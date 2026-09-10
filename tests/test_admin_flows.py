import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import admin_handlers as admin
import admin_utils


class AdminFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=123456789, chat_id=1, user_id=1))
        self.user = types.User(id=1, is_bot=False, first_name="Admin")
        self.message = types.Message(
            message_id=10, date=1, chat=types.Chat(id=1, type="private"), from_user=self.user,
            text="input",
        )
        self.patches = [
            patch.object(admin, "ADMIN_IDS", [1]),
            patch.object(types.Message, "answer", new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, "edit_text", new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, "edit_reply_markup", new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, "delete", new=AsyncMock()),
            patch.object(types.CallbackQuery, "answer", new=AsyncMock()),
            patch.object(admin_utils, "edit_admin_rich", new=AsyncMock()),
            patch.object(admin, "edit_admin_rich", new=AsyncMock()),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.storage.close()
        await self.bot.session.close()

    async def route(self, event):
        kind = "callback_query" if isinstance(event, types.CallbackQuery) else "message"
        return await admin.admin_router.propagate_event(
            kind, event, bot=self.bot, state=self.state,
            raw_state=await self.state.get_state(), event_from_user=self.user,
        )

    def callback(self, data, message_id=10):
        return types.CallbackQuery(
            id="audit", from_user=self.user, chat_instance="test", data=data,
            message=self.message.model_copy(update={"message_id": message_id}),
        )

    async def set_editing_resource(self):
        await self.state.set_state(admin_utils.GenericEditStates.new_value)
        await self.state.set_data({"editing_entity": "resource", "entity_id": 1, "edit_field": "name"})

    async def test_kombat_cancels_input_instead_of_saving_command(self):
        await self.set_editing_resource()
        config = dict(admin.ENTITY_CONFIGS["resource"], update_func=AsyncMock())
        with patch.dict(admin.ENTITY_CONFIGS, {"resource": config}):
            await self.route(self.message.model_copy(update={"text": "/kombat"}))
        config["update_func"].assert_not_awaited()
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_other_commands_are_not_saved_as_input(self):
        await self.set_editing_resource()
        config = dict(admin.ENTITY_CONFIGS["resource"], update_func=AsyncMock())
        with patch.dict(admin.ENTITY_CONFIGS, {"resource": config}):
            await self.route(self.message.model_copy(update={"text": "/unknown"}))
        config["update_func"].assert_not_awaited()

    async def test_close_clears_state_before_next_text(self):
        await self.set_editing_resource()
        config = dict(admin.ENTITY_CONFIGS["resource"], update_func=AsyncMock())
        with patch.dict(admin.ENTITY_CONFIGS, {"resource": config}):
            await self.route(self.callback("admin_close"))
            await self.route(self.message.model_copy(update={"text": "search after close"}))
        config["update_func"].assert_not_awaited()
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_old_mob_confirmation_cannot_delete_current_mob(self):
        await self.state.set_state(admin.MobStates.edit_field)
        await self.state.set_data({"mob_id": 1})
        with patch.object(admin.db, "execute_query", new=AsyncMock(return_value=[{"name": "Mob A"}])):
            await self.route(self.callback("mob_delete"))
        confirmation = (await self.state.get_data())["admin_delete_confirmation"]
        old_data = "confirm_mob_delete_" + confirmation["token"]
        await self.route(self.message.model_copy(update={"text": "/kombat"}))
        await self.state.set_state(admin.MobStates.edit_field)
        await self.state.update_data(mob_id=2)
        with patch.object(admin.db, "delete_mob", new=AsyncMock()) as delete:
            await self.route(self.callback(old_data))
            await self.route(self.callback("confirm_mob_delete"))
        delete.assert_not_awaited()

    async def test_mob_confirmation_deletes_once(self):
        await self.state.set_state(admin.MobStates.edit_field)
        await self.state.set_data({"mob_id": 1})
        with patch.object(admin.db, "execute_query", new=AsyncMock(return_value=[{"name": "Mob A"}])):
            await self.route(self.callback("mob_delete"))
        confirmation = (await self.state.get_data())["admin_delete_confirmation"]
        callback = self.callback("confirm_mob_delete_" + confirmation["token"])
        with patch.object(admin.db, "delete_mob", new=AsyncMock()) as delete, patch.object(
            admin, "get_mob_locations_keyboard", new=AsyncMock(return_value=types.InlineKeyboardMarkup(inline_keyboard=[])),
        ):
            await self.route(callback)
            await self.route(callback)
        delete.assert_awaited_once_with(1)

    async def test_generic_confirmation_rejects_changed_object_message_and_token(self):
        config = dict(
            admin.ENTITY_CONFIGS["resource"],
            get_by_id_func=AsyncMock(return_value={"id": 1, "name": "A"}),
            delete_func=AsyncMock(),
        )
        with patch.dict(admin.ENTITY_CONFIGS, {"resource": config}):
            for change in ("object", "message", "token"):
                with self.subTest(change=change):
                    await self.state.set_state(admin_utils.GenericEditStates.select_field)
                    await self.state.set_data({"editing_entity": "resource", "entity_id": 1})
                    await self.route(self.callback("delete_entity"))
                    confirmation = (await self.state.get_data())["admin_delete_confirmation"]
                    old_callback = self.callback("confirm_delete_yes_" + confirmation["token"])
                    if change == "object":
                        await self.state.update_data(entity_id=2)
                    elif change == "message":
                        old_callback = self.callback(old_callback.data, message_id=99)
                    else:
                        await self.state.set_state(admin_utils.GenericEditStates.select_field)
                        await self.route(self.callback("delete_entity"))
                    await self.route(old_callback)
        config["delete_func"].assert_not_awaited()

    async def test_generic_confirmation_deletes_correct_resource_once(self):
        config = dict(
            admin.ENTITY_CONFIGS["resource"],
            get_by_id_func=AsyncMock(return_value={"id": 1, "name": "A"}),
            delete_func=AsyncMock(), get_page_func=AsyncMock(return_value=[]),
        )
        await self.state.set_state(admin_utils.GenericEditStates.select_field)
        await self.state.set_data({"editing_entity": "resource", "entity_id": 1})
        with patch.dict(admin.ENTITY_CONFIGS, {"resource": config}):
            await self.route(self.callback("delete_entity"))
            confirmation = (await self.state.get_data())["admin_delete_confirmation"]
            callback = self.callback("confirm_delete_yes_" + confirmation["token"])
            await self.route(callback)
            await self.route(callback)
        config["delete_func"].assert_awaited_once_with(1)

    async def test_recipe_confirmation_is_bound_to_recipe_and_message(self):
        await self.state.set_state(admin.RecipeStates.view_recipe)
        await self.state.set_data({"recipe_id": 1})
        await self.route(self.callback("recipe_delete"))
        confirmation = (await self.state.get_data())["admin_delete_confirmation"]
        callback = self.callback("recipe_delete_yes_" + confirmation["token"])
        with patch.object(admin.db, "delete_recipe", new=AsyncMock()) as delete, patch.object(
            admin, "get_recipe_list_keyboard", new=AsyncMock(return_value=types.InlineKeyboardMarkup(inline_keyboard=[])),
        ):
            await self.route(self.callback(callback.data, message_id=99))
            delete.assert_not_awaited()
            await self.state.update_data(recipe_id=2)
            await self.route(callback)
            delete.assert_not_awaited()
            await self.state.update_data(recipe_id=1)
            await self.route(callback)
            await self.route(callback)
        delete.assert_awaited_once_with(1)

    async def test_owner_selection_uses_stable_id_and_confirms_recipe(self):
        await self.state.set_state(admin.RecipeStates.manage_owners)
        await self.state.set_data({"recipe_id": 7})
        owners = [{"owner_id": 42, "user_id": 99, "player_username": "tester"}]
        with patch.object(admin.db, "get_recipe_owner_entries", new=AsyncMock(return_value=owners)), patch.object(
            admin.db, "remove_recipe_owner_entry", new=AsyncMock(),
        ) as remove:
            await self.route(self.callback("recipe_owner_select_42"))
            confirmation = (await self.state.get_data())["admin_delete_confirmation"]
            callback = self.callback("recipe_owner_delete_yes_" + confirmation["token"])
            await self.state.update_data(recipe_id=8)
            await self.route(callback)
            remove.assert_not_awaited()
            await self.state.update_data(recipe_id=7)
            await self.route(callback)
            await self.route(callback)
        remove.assert_awaited_once_with(7, 42)

    async def test_owner_without_username_is_visible_by_id(self):
        recipe = {
            "id": 7, "result_type": "gear", "result_id": 2, "quantity": 1,
            "ingredients": [], "owners": [],
            "owner_entries": [{"owner_id": 42, "user_id": 99, "player_username": None}],
        }
        with patch.object(admin.db, "get_gear_by_id", new=AsyncMock(return_value={"name": "Gear", "emoji": ""})):
            await admin.show_recipe(self.message, recipe, self.state)
        text = types.Message.answer.await_args.args[0]
        self.assertIn("Игрок 99", text)
        self.assertNotIn("Нет владельцев", text)
        await self.state.update_data(recipe_id=7)
        with patch.object(admin.db, "get_recipe_owner_entries", new=AsyncMock(return_value=recipe["owner_entries"])):
            await admin.show_recipe_owners(self.callback("recipe_manage_owners"), self.state)
        keyboard = types.Message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].text, "❌ Игрок 99")

    async def test_back_from_drop_returns_to_same_mob(self):
        await self.state.set_state(admin.MobStates.drop_category)
        await self.state.set_data({"mob_id": 2})
        mob = {"id": 2, "name": "A", "emoji": "", "hp": 1, "dust_min": 1, "dust_max": 2, "exp": 1}
        with patch.object(admin, "get_mob_edit_data", new=AsyncMock(return_value=mob)):
            await self.route(self.callback("back_to_mob_edit"))
        self.assertEqual(await self.state.get_state(), admin.MobStates.edit_field.state)
        self.assertEqual((await self.state.get_data())["mob_id"], 2)

    async def test_duplicate_ingredient_recovers_to_filtered_selection(self):
        await self.state.set_state(admin.RecipeStates.edit_ingredient_quantity)
        await self.state.set_data({"recipe_id": 7, "edit_action": "add", "temp_resource_id": 1})
        with patch.object(admin.db, "add_ingredient", new=AsyncMock(side_effect=ValueError("Already exists"))), patch.object(
            admin.db, "get_recipe_details", new=AsyncMock(return_value={"ingredients": [{"resource_id": 1}]}),
        ), patch.object(admin.db, "get_all_resources_simple", new=AsyncMock(return_value=[
            {"id": 1, "name": "First", "emoji": ""}, {"id": 2, "name": "Second", "emoji": ""},
        ])):
            await self.route(self.message.model_copy(update={"text": "2"}))
        self.assertEqual(await self.state.get_state(), admin.RecipeStates.add_ingredient.state)
        self.assertEqual([row["id"] for row in (await self.state.get_data())["ingredient_resources"]], [2])

    async def test_ingredient_delete_rejects_old_message_and_wrong_recipe(self):
        await self.state.set_state(admin.RecipeStates.edit_ingredient)
        await self.state.set_data({"recipe_id": 7, "edit_action": "add", "temp_resource_id": 99})
        recipe = {"ingredients": [{"resource_id": 3, "name": "Ingredient"}]}
        with patch.object(admin.db, "get_recipe_details", new=AsyncMock(return_value=recipe)), patch.object(
            admin.db, "remove_ingredient", new=AsyncMock(),
        ) as remove, patch.object(admin, "show_recipe", new=AsyncMock()):
            await self.route(self.callback("recipe_edit_ing_3"))
            self.assertIsNone((await self.state.get_data())["edit_action"])
            confirmation = (await self.state.get_data())["admin_delete_confirmation"]
            callback = self.callback("recipe_ing_delete_" + confirmation["token"])
            await self.route(self.callback(callback.data, message_id=99))
            remove.assert_not_awaited()
            await self.state.update_data(recipe_id=8)
            await self.route(callback)
            remove.assert_not_awaited()
            await self.state.update_data(recipe_id=7)
            await self.route(callback)
            await self.route(callback)
        remove.assert_awaited_once_with(7, 3)

    async def test_mob_dust_edit_rejects_inverted_range(self):
        for field, value in (("dust_min", "30"), ("dust_max", "5")):
            with self.subTest(field=field):
                await self.state.set_state(admin.MobStates.edit_new_value)
                await self.state.set_data({"mob_id": 1, "edit_field": field})
                with patch.object(admin, "get_mob_edit_data", new=AsyncMock(return_value={"dust_min": 10, "dust_max": 20})), patch.object(
                    admin.db, "update_mob_field", new=AsyncMock(),
                ) as update:
                    await self.route(self.message.model_copy(update={"text": value}))
                update.assert_not_awaited()
                self.assertEqual(await self.state.get_state(), admin.MobStates.edit_new_value.state)


if __name__ == "__main__":
    unittest.main()
