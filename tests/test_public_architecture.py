import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError, TelegramBadRequest
from aiogram.methods import SendRichMessage

from database import Database
from public_catalog import PublicCatalogHandlers
from public_presentation import PublicContext


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
        for payload in ('gear_slots_unknown','back_to_locations_invalid'):
            callback = types.CallbackQuery(id='invalid',data=payload,chat_instance='test',message=self.message,from_user=self.user).as_(self.bot)
            with patch.object(self.db,'execute_query',side_effect=AssertionError('invalid input reached SQL')), patch.object(types.CallbackQuery,'answer',new=AsyncMock()) as answer:
                await self.app.router.propagate_event('callback_query',callback,bot=self.bot)
            answer.assert_awaited_once()
            self.assertTrue(answer.await_args.kwargs['show_alert'])
