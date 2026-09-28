import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendRichMessage

from database import Database
from public_catalog import PublicCatalogHandlers
from public_presentation import PublicContext
from ui.rich import CardView


class PublicArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        self.app = PublicCatalogHandlers(PublicContext(self.db, bot_username="catalog_bot"))
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.user = types.User(id=101, is_bot=False, first_name="Player")
        self.message = types.Message(
            message_id=1,
            date=1,
            chat=types.Chat(id=-1001, type="supergroup"),
            message_thread_id=72,
            from_user=self.user,
            text="🐾 Мобы",
        ).as_(self.bot)

    async def asyncTearDown(self):
        await self.app.background_tasks.close()
        await self.db.close()
        await self.bot.session.close()

    def private_message(self, text: str) -> types.Message:
        return types.Message(
            message_id=2,
            date=1,
            chat=types.Chat(id=self.user.id, type="private"),
            from_user=self.user,
            text=text,
        ).as_(self.bot)

    def callback(self, data: str) -> types.CallbackQuery:
        return types.CallbackQuery(
            id="test",
            data=data,
            chat_instance="test",
            message=self.private_message("card"),
            from_user=self.user,
        ).as_(self.bot)

    async def test_menu_clears_abandoned_input_before_plain_text_search(self):
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=self.bot.id, chat_id=self.user.id, user_id=self.user.id))
        await state.set_state("GenericEditStates:new_value")
        await state.set_data({"editing_entity": "resource", "entity_id": 1, "edit_field": "name"})
        try:
            with (
                patch.object(types.Message, "answer", new=AsyncMock()),
                patch.object(self.app.analytics, "log_start", new=AsyncMock()),
                patch.object(self.app.analytics, "log_search", new=AsyncMock()),
                patch.object(self.db, "search", new=AsyncMock(return_value={})) as search,
            ):
                await self.app.send_menu(self.private_message("/menu"), state)
                self.assertIsNone(await state.get_state())
                self.assertEqual(await state.get_data(), {})
                await self.app.handle_search(self.private_message("Bronze sword"), state)
            search.assert_awaited_once_with("Bronze sword")
        finally:
            await storage.close()

    async def test_deep_links_record_views_for_every_catalog_type(self):
        card = CardView("<b>Found</b>", "Found")
        builders = {
            "mob": ("build_mob_card", "log_view_mob", "get_mob_full_card"),
            "resource": ("build_resource_card", "log_view_resource", "get_resource_card"),
            "gear": ("build_gear_card", "log_view_gear", "get_gear_card"),
            "card": ("build_card_card", "log_view_card", "get_card_by_id"),
        }
        target = {"id": 7, "type": "craft", "location_id": 1, "rarity": "epic", "slot": "шлем"}
        for kind, (builder_name, logger_name, fetch_name) in builders.items():
            with (
                self.subTest(kind=kind),
                patch.object(self.db, fetch_name, new=AsyncMock(return_value=target)),
                patch.object(
                    self.db, "get_prev_next_gear", new=AsyncMock(return_value={"prev_id": None, "next_id": None})
                ),
                patch.object(self.app, builder_name, new=AsyncMock(return_value=card)) as builder,
                patch.object(self.app.analytics, logger_name, new=AsyncMock()) as logger,
                patch.object(self.app, "upsert_rich_card", new=AsyncMock(return_value=self.private_message("card"))),
                patch.object(types.Message, "delete", new=AsyncMock()),
            ):
                await self.app.send_menu(self.private_message(f"/start {kind}_7"), AsyncMock())
            logger.assert_awaited_once_with(self.user.id, 7)
            builder.assert_awaited_once()

    async def test_resource_deep_link_preserves_gear_return_context(self):
        with (
            patch.object(self.db, "get_resource_card", new=AsyncMock(return_value={"id": 7, "type": "craft"})),
            patch.object(self.app, "build_resource_card", new=AsyncMock(return_value=CardView("Found", "Found"))),
            patch.object(self.app.analytics, "log_view_resource", new=AsyncMock()),
            patch.object(
                self.app, "upsert_rich_card", new=AsyncMock(return_value=self.private_message("card"))
            ) as render,
            patch.object(types.Message, "delete", new=AsyncMock()),
        ):
            await self.app.send_menu(self.private_message("/start resource_7-r-gear_21_epic_4_3"), AsyncMock())
        buttons = render.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(buttons[0][0].callback_data, "view_gear_21_epic_4_3")

    async def test_gear_slot_change_updates_links_and_keyboard_together(self):
        data = {
            "id": 21,
            "name": "Gear",
            "emoji": "🛡",
            "rarity": "epic",
            "slot": "плечи",
            "craftable": True,
            "recipe_id": 1,
            "owner_user_ids": [],
            "owner_entries": [],
            "ingredients": [{"id": 7, "name": "Ore", "emoji": "", "quantity": 1}],
            "scroll_mobs": [],
            "mobs": [],
        }
        with (
            patch.object(self.db, "get_gear_card", new=AsyncMock(return_value=data)),
            patch.object(self.db, "get_prev_next_gear", new=AsyncMock(return_value={"prev_id": None, "next_id": None})),
            patch.object(self.app, "upsert_rich_card", new=AsyncMock()) as render,
        ):
            await self.app.render_gear_card(self.callback("view_gear_21_epic_0_3"), 21, "epic", 3, 0)
        kwargs = render.await_args.kwargs
        self.assertIn("resource_7-r-gear_21_epic_1_3", kwargs["plain_text"])
        self.assertIn('data="entity:resource:7:gear:21"', kwargs["rich_message"].html)
        self.assertEqual(kwargs["reply_markup"].inline_keyboard[-1][0].callback_data, "page_gear_epic_1_3")

    async def test_world_map_preserves_thread_without_retry_after_ambiguous_delivery(self):
        failure = TelegramNetworkError(
            method=SendRichMessage(chat_id=-1001, rich_message={"html": "map"}), message="timeout"
        )
        with (
            patch("public_catalog.os.path.isfile", return_value=True),
            patch.object(self.bot, "send_rich_message", new=AsyncMock(side_effect=failure)) as rich,
            patch.object(self.bot, "send_message", new=AsyncMock()) as plain,
        ):
            with self.assertRaises(TelegramNetworkError):
                await self.app.mobs_button(self.message, AsyncMock())
        self.assertEqual(rich.await_args.kwargs["message_thread_id"], 72)
        rich.assert_awaited_once()
        plain.assert_not_awaited()

    async def test_world_map_format_rejection_falls_back_in_same_thread(self):
        failure = TelegramBadRequest(
            method=SendRichMessage(chat_id=-1001, rich_message={"html": "map"}), message="unsupported format"
        )
        with (
            patch("public_catalog.os.path.isfile", return_value=True),
            patch.object(self.bot, "send_rich_message", new=AsyncMock(side_effect=failure)),
            patch.object(self.bot, "send_message", new=AsyncMock(return_value=self.message)) as plain,
        ):
            await self.app.mobs_button(self.message, AsyncMock())
        self.assertEqual(plain.await_args.kwargs["message_thread_id"], 72)
        plain.assert_awaited_once()

    async def test_group_keyboard_exposes_explicit_actions_for_every_reader(self):
        gear = await self.db.add_gear("Gear", "rare", "шлем", "🛡")
        recipe = await self.db.create_recipe("gear", gear)
        scroll = await self.db.add_resource("Scroll", "📜", "scroll_recipe")
        await self.db.set_recipe_learning_scroll(recipe, scroll)
        await self.db.claim_recipe_owner(recipe, 101, None, expected_gear_id=gear)
        data = await self.db.get_gear_card(gear)
        first = await self.app.build_gear_card_keyboard(data, 101, 1, None, personal=False)
        second = await self.app.build_gear_card_keyboard(data, 202, 1, None, personal=False)
        self.assertEqual(first, second)
        actions = [
            button.callback_data
            for row in first.inline_keyboard
            for button in row
            if (button.callback_data or "").startswith("recipe_")
        ]
        self.assertEqual(len(actions), 2)
        for user_id in (202, 303):
            callback = types.CallbackQuery(
                id=str(user_id),
                data=actions[0],
                chat_instance="test",
                message=self.message,
                from_user=types.User(id=user_id, is_bot=False, first_name="Reader"),
            ).as_(self.bot)
            with (
                patch.object(types.CallbackQuery, "answer", new=AsyncMock()),
                patch.object(self.app, "render_gear_card", new=AsyncMock()),
            ):
                await self.app.update_recipe_owner(callback)
        self.assertEqual((await self.db.get_gear_card(gear))["owner_user_ids"], [101, 202, 303])

    async def test_locations_follow_metadata_for_arbitrary_ids(self):
        parent = await self.db.execute_insert("INSERT INTO locations(id,name,emoji) VALUES (500,'Forest','🌲')")
        child = await self.db.execute_insert(
            "INSERT INTO locations(id,name,emoji,parent_id) VALUES (600,'Cave','🪨',?)", (parent,)
        )
        top = await self.app.get_locations_keyboard("mobs")
        callbacks = [b.callback_data for row in top.inline_keyboard for b in row]
        self.assertIn(f"mobs_location_group_{parent}", callbacks)
        self.assertNotIn(f"list_mobs_{child}_1", callbacks)
        nested = await self.app.get_location_group_keyboard(parent)
        callbacks = [b.callback_data for row in nested.inline_keyboard for b in row]
        self.assertIn(f"list_mobs_{child}_1", callbacks)
        self.assertIn(f"list_mobs_{parent}_1", callbacks)

    async def test_location_groups_allow_multiple_nesting_levels(self):
        first = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Root','🌲')")
        second = await self.db.execute_insert(
            "INSERT INTO locations(name,emoji,parent_id) VALUES ('Child','🪨',?)", (first,)
        )
        third = await self.db.execute_insert(
            "INSERT INTO locations(name,emoji,parent_id) VALUES ('Grandchild','🦇',?)", (second,)
        )
        keyboard = await self.app.get_location_group_keyboard(first)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"mobs_location_group_{second}", callbacks)
        keyboard = await self.app.get_location_group_keyboard(second)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"list_mobs_{third}_1", callbacks)
        self.assertIn(f"mobs_location_group_{first}", callbacks)
        keyboard = await self.app.get_items_keyboard("mobs", second, 1)
        self.assertEqual(keyboard.inline_keyboard[-1][0].callback_data, f"mobs_location_group_{second}")

    async def test_invalid_categories_do_not_reach_catalog_queries(self):
        for payload in ("gear_slots_unknown", "back_to_locations_invalid"):
            callback = types.CallbackQuery(
                id="invalid", data=payload, chat_instance="test", message=self.message, from_user=self.user
            ).as_(self.bot)
            with (
                patch.object(self.db, "execute_query", side_effect=AssertionError("invalid input reached SQL")),
                patch.object(types.CallbackQuery, "answer", new=AsyncMock()) as answer,
            ):
                await self.app.router.propagate_event("callback_query", callback, bot=self.bot)
            answer.assert_awaited_once()
            self.assertTrue(answer.await_args.kwargs["show_alert"])
