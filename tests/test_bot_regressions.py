import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendRichMessage

import admin_handlers as admin
import admin_utils
from tests.public_fixture import app
from database import Database
from messaging import replace_rich_card
from search_rendering import build_search_content
from telegram_text import split_formatted_text, utf16_length
from ui.rich import CardView


class BotFlowRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=self.bot.id, chat_id=101, user_id=101))
        self.user = types.User(id=101, is_bot=False, first_name="Player", username="OldName")
        self.message = types.Message(
            message_id=1, date=1, chat=types.Chat(id=101, type="private"),
            from_user=self.user, text="input",
        ).as_(self.bot)
        self.patches = [
            patch.object(types.Message, "answer", new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, "delete", new=AsyncMock()),
            patch.object(types.CallbackQuery, "answer", new=AsyncMock()),
            patch.object(app, "BOT_USERNAME", "audit_bot"),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.storage.close()
        await self.bot.session.close()

    async def route_message(self, text):
        message = self.message.model_copy(update={"text": text})
        return await app.dp.propagate_event(
            "message", message, bot=self.bot, state=self.state,
            raw_state=await self.state.get_state(), event_from_user=self.user,
        )

    def callback(self, data, user=None):
        return types.CallbackQuery(
            id="test", data=data, chat_instance="test", from_user=user or self.user,
            message=self.message,
        ).as_(self.bot)

    async def test_menu_then_search_does_not_resume_admin_edit(self):
        await self.state.set_state(admin_utils.GenericEditStates.new_value)
        await self.state.set_data({"editing_entity": "resource", "entity_id": 1, "edit_field": "name"})
        config = dict(admin.ENTITY_CONFIGS["resource"], update_func=AsyncMock())
        with patch.object(admin, "ADMIN_IDS", [101]), patch.dict(admin.ENTITY_CONFIGS, {"resource": config}), patch.object(
            app.dp, "sub_routers", [admin.admin_router],
        ), patch.object(app.analytics, "log_start", new=AsyncMock()), patch.object(app.analytics, "log_search", new=AsyncMock()), patch.object(
            app.db, "search", new=AsyncMock(return_value={}),
        ) as search:
            await self.route_message("/menu")
            await self.route_message("Bronze sword")
        config["update_func"].assert_not_awaited()
        search.assert_awaited_once_with("Bronze sword")
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})

    async def test_successful_deep_links_record_view_for_each_catalog_type(self):
        card = CardView("<b>Found</b>", "Found")
        names = {
            "mob": ("build_mob_card", "log_view_mob"),
            "resource": ("build_resource_card", "log_view_resource"),
            "gear": ("build_gear_card", "log_view_gear"),
            "card": ("build_card_card", "log_view_card"),
        }
        fetch_names = {
            "mob": "get_mob_full_card", "resource": "get_resource_card",
            "gear": "get_gear_card", "card": "get_card_by_id",
        }
        target = {"id": 7, "type": "craft", "location_id": 1, "rarity": "epic", "slot": "шлем"}
        for kind, (builder_name, log_name) in names.items():
            with self.subTest(kind=kind), patch.object(app.db, "get_prev_next_gear", new=AsyncMock(return_value={"prev_id": None, "next_id": None})), patch.object(app.db, fetch_names[kind], new=AsyncMock(return_value=target)), patch.object(app, builder_name, new=AsyncMock(return_value=card)) as builder, patch.object(app.analytics, log_name, new=AsyncMock()) as log, patch.object(
                app, "upsert_rich_card", new=AsyncMock(return_value=self.message),
            ):
                await self.route_message(f"/start {kind}_7")
            log.assert_awaited_once_with(101, 7)
            builder.assert_awaited_once()

    async def test_resource_deep_link_keeps_return_gear_slot_and_page(self):
        with patch.object(app.db, "get_resource_card", new=AsyncMock(return_value={"id": 7, "type": "craft"})), patch.object(app, "build_resource_card", new=AsyncMock(return_value=CardView("Found", "Found"))), patch.object(app.analytics, "log_view_resource", new=AsyncMock()), patch.object(
            app, "upsert_rich_card", new=AsyncMock(return_value=self.message),
        ) as render:
            await self.route_message("/start resource_7-r-gear_21_epic_4_3")
        buttons = render.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "view_gear_21_epic_4_3")

    async def test_gear_slot_change_updates_deep_links_and_keyboard_together(self):
        data = {
            "id": 21, "name": "Gear", "emoji": "🛡", "rarity": "epic", "slot": "плечи",
            "craftable": True, "recipe_id": 1, "owner_user_ids": [], "owner_entries": [],
            "ingredients": [{"id": 7, "name": "Ore", "emoji": "", "quantity": 1}],
            "scroll_mobs": [], "mobs": [],
        }
        with patch.object(app.db, "get_gear_card", new=AsyncMock(return_value=data)), patch.object(
            app.db, "get_prev_next_gear", new=AsyncMock(return_value={"prev_id": None, "next_id": None}),
        ), patch.object(app, "upsert_rich_card", new=AsyncMock()) as render:
            await app.render_gear_card(self.callback("view_gear_21_epic_0_3"), 21, "epic", 3, 0)
        kwargs = render.await_args.kwargs
        self.assertIn("resource_7-r-gear_21_epic_1_3", kwargs["plain_text"])
        self.assertIn('data="entity:resource:7:gear:21"', kwargs["rich_message"].html)
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[-1][0].callback_data, "page_gear_epic_1_3")

    async def test_owner_actions_use_user_id_after_rename_or_without_username(self):
        isolated = Database(":memory:")
        await isolated.connect()
        try:
            gear_id = await isolated.add_gear("Gear", "epic", "шлем", "🛡")
            recipe_id = await isolated.create_recipe("gear", gear_id)
            scroll_id = await isolated.add_resource("Learning scroll", "📜", "scroll_recipe")
            await isolated.set_recipe_learning_scroll(recipe_id, scroll_id)
            claim = f"recipe_claim_{recipe_id}_{gear_id}_epic_0_1"
            relinquish = f"recipe_relinquish_{recipe_id}_{gear_id}_epic_0_1"
            with patch.object(app, "db", isolated), patch.object(app, "render_gear_card", new=AsyncMock(return_value=True)):
                await app.update_recipe_owner(self.callback(claim))
                stranger = types.User(id=202, is_bot=False, first_name="Other", username="OldName")
                await app.update_recipe_owner(self.callback(relinquish, stranger))
                self.assertEqual((await isolated.get_gear_card(gear_id))["owner_user_ids"], [101])
                renamed = self.user.model_copy(update={"username": "NewName"})
                await app.update_recipe_owner(self.callback(relinquish, renamed))
                self.assertEqual((await isolated.get_gear_card(gear_id))["owner_user_ids"], [])
                anonymous = self.user.model_copy(update={"username": None})
                await app.update_recipe_owner(self.callback(claim, anonymous))
                data = await isolated.get_gear_card(gear_id)
                self.assertEqual(data["owner_user_ids"], [101])
                keyboard = await app.build_gear_card_keyboard(data, anonymous.id, 1, 0)
                callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
                self.assertIn(relinquish, callbacks)
                plain = await app.format_gear_card_plain(gear_id, "epic", data=data)
                self.assertIn("tg://user?id=101", plain)
                self.assertNotIn("@None", plain)
        finally:
            await isolated.close()


class TextAndMessagingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_rich_rejection_splits_safe_fallback_and_moves_keyboard_to_last_chunk(self):
        bot = AsyncMock()
        current = AsyncMock()
        bot.id = 703
        current.message_id = 10
        current.message_thread_id = 8
        rich = types.InputRichMessage(html="<b>rich</b>")
        method = SendRichMessage(chat_id=1, rich_message=rich)
        bot.send_rich_message.side_effect = TelegramBadRequest(method=method, message="Unsupported format")
        plain = "<b>" + "🛡&amp;" * 3000 + "</b>"
        events = []
        async def send(**kwargs):
            events.append(("send", kwargs))
            return types.Message(
                message_id=10 + len(events), date=1,
                chat=types.Chat(id=1, type="private"), text=kwargs["text"], message_thread_id=8,
            )
        async def delete():
            events.append(("delete", None))
        bot.send_message.side_effect = send
        current.delete.side_effect = delete
        markup = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text="Back", callback_data="back")]])
        await replace_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text=plain, reply_markup=markup, current_message=current)
        messages = [value for event, value in events if event == "send"]
        self.assertGreater(len(messages), 1)
        self.assertEqual("".join(item["text"] for item in messages), "🛡&" * 3000)
        self.assertTrue(all(utf16_length(item["text"]) <= 4096 for item in messages))
        self.assertTrue(all(item["parse_mode"] is None for item in messages))
        self.assertTrue(all(item["message_thread_id"] == 8 for item in messages))
        self.assertTrue(all(item["reply_markup"] is None for item in messages[:-1]))
        self.assertIs(messages[-1]["reply_markup"], markup)
        self.assertEqual(events[-1][0], "delete")

    async def test_ambiguous_network_failure_keeps_old_card_without_retry(self):
        bot = AsyncMock()
        current = AsyncMock()
        rich = types.InputRichMessage(html="Card")
        bot.send_rich_message.side_effect = TelegramNetworkError(
            method=SendRichMessage(chat_id=1, rich_message=rich), message="Connection lost",
        )
        with self.assertRaises(TelegramNetworkError):
            await replace_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text="Card", reply_markup=None, current_message=current)
        bot.send_message.assert_not_awaited()
        current.delete.assert_not_awaited()

    async def test_failure_during_fallback_preserves_previous_navigation(self):
        bot = AsyncMock()
        current = AsyncMock()
        rich = types.InputRichMessage(html="Card")
        method = SendRichMessage(chat_id=1, rich_message=rich)
        bot.send_rich_message.side_effect = TelegramBadRequest(method=method, message="Unsupported")
        bot.send_message.side_effect = [
            types.Message(message_id=11, date=1, chat=types.Chat(id=1, type="private"), text="part"),
            TelegramNetworkError(method=method, message="Connection lost"),
        ]
        with self.assertRaises(TelegramNetworkError):
            await replace_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text="x" * 5000, reply_markup=None, current_message=current)
        current.delete.assert_not_awaited()

    async def test_search_treats_catalog_markup_as_literal_text(self):
        malicious = '<a href="https://wrong.example">name</a>'
        content = build_search_content({"gear": [{"id": 1, "name": malicious, "emoji": "<b>emoji</b>", "rarity": "epic"}]}, "audit_bot")
        chunks = split_formatted_text(content, limit=40)
        self.assertIn(malicious, "".join(chunk.text for chunk in chunks))
        links = [entity for chunk in chunks for entity in chunk.entities if entity.type == "text_link"]
        self.assertTrue(links)
        self.assertEqual({entity.url for entity in links}, {"https://t.me/audit_bot?start=gear_1"})
        for chunk in chunks:
            size = utf16_length(chunk.text)
            self.assertLessEqual(size, 40)
            self.assertTrue(all(0 <= entity.offset < entity.offset + entity.length <= size for entity in chunk.entities))


if __name__ == "__main__":
    unittest.main()
