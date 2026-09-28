import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.types import CallbackQuery, Chat, Message, User
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from analytics import AnalyticsService
from database import Database
from stats_handlers import (
    PrivateStatsMiddleware,
    show_stats_command,
    show_stats_callback,
    stats_users_page,
    stats_user_details,
)
from utils import escape_html, is_valid_emoji


class StatisticsPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.middleware = PrivateStatsMiddleware()

    async def asyncTearDown(self):
        await self.bot.session.close()

    def message(self, chat_type):
        return Message(
            message_id=1,
            date=datetime.now(timezone.utc),
            chat=Chat(id=901 if chat_type == "private" else -100901, type=chat_type),
            from_user=User(id=901, is_bot=False, first_name="Synthetic"),
            text="/stats",
        ).as_(self.bot)

    async def test_group_command_and_callback_never_enter_statistics_handler(self):
        handler = AsyncMock()
        for chat_type in ("group", "supergroup", "channel"):
            with self.subTest(chat_type=chat_type):
                message = self.message(chat_type)
                callback = CallbackQuery(
                    id="test",
                    from_user=message.from_user,
                    chat_instance="test",
                    message=message,
                    data="stats_user_902_1",
                ).as_(self.bot)
                with patch.object(Bot, "__call__", AsyncMock(return_value=True)) as send:
                    await self.middleware(handler, message, {})
                    await self.middleware(handler, callback, {})
                handler.assert_not_awaited()
                self.assertEqual(send.await_count, 2)
                for call in send.call_args_list:
                    self.assertNotIn("902", str(call))

    async def test_private_command_and_callback_reach_handler(self):
        message = self.message("private")
        callback = CallbackQuery(
            id="test", from_user=message.from_user, chat_instance="test", message=message, data="stats_users"
        ).as_(self.bot)
        handler = AsyncMock(return_value="ok")
        self.assertEqual(await self.middleware(handler, message, {}), "ok")
        self.assertEqual(await self.middleware(handler, callback, {}), "ok")
        self.assertEqual(handler.await_count, 2)

    async def test_callback_without_accessible_chat_is_denied(self):
        callback = CallbackQuery(
            id="test",
            from_user=User(id=901, is_bot=False, first_name="Synthetic"),
            chat_instance="test",
            inline_message_id="inline",
            data="stats_users",
        ).as_(self.bot)
        handler = AsyncMock()
        with patch.object(Bot, "__call__", AsyncMock(return_value=True)):
            await self.middleware(handler, callback, {})
        handler.assert_not_awaited()

    async def test_statistics_navigation_clears_abandoned_edit_state(self):
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=self.bot.id, chat_id=901, user_id=901))
        message = self.message("private")
        callback = CallbackQuery(
            id="test", from_user=message.from_user, chat_instance="test", message=message, data="admin_stats"
        ).as_(self.bot)
        try:
            with (
                patch("stats_handlers.show_stats_menu", AsyncMock()),
                patch.object(Bot, "__call__", AsyncMock(return_value=True)),
            ):
                for entry, event in ((show_stats_command, message), (show_stats_callback, callback)):
                    await state.set_state("GenericEditStates:new_value")
                    await state.set_data({"item_id": 12})
                    await entry(event, state)
                    self.assertIsNone(await state.get_state())
                    self.assertEqual(await state.get_data(), {})
        finally:
            await storage.close()

    async def test_invalid_statistics_payload_does_not_query_users(self):
        message = self.message("private")
        analytics = AnalyticsService(Database(":memory:"))
        with (
            patch("stats_handlers.show_users", AsyncMock()) as show_users,
            patch("stats_handlers.show_user_details", AsyncMock()) as show_details,
            patch.object(Bot, "__call__", AsyncMock(return_value=True)),
        ):
            for payload in (
                "stats_users_page_bad",
                "stats_users_page_-1",
                "stats_user_1_extra_1",
                "stats_user_9999999999999999999_1",
            ):
                callback = CallbackQuery(
                    id="test", from_user=message.from_user, chat_instance="test", message=message, data=payload
                ).as_(self.bot)
                handler = stats_users_page if payload.startswith("stats_users_page_") else stats_user_details
                await handler(callback, analytics=analytics)
            show_users.assert_not_awaited()
            show_details.assert_not_awaited()


class AnalyticsQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_typed_statistics_project_nullable_users_and_deleted_items(self):
        database = Database(":memory:")
        await database.connect()
        try:
            await database.register_user_if_not_exists(42, first_name="Example")
            await database.execute_query(
                "INSERT INTO analytics_events(user_id, event_type, target_id) VALUES (42, 'view_mob', 7)"
            )
            await database.execute_query(
                "INSERT INTO analytics_events(user_id, event_type, metadata) VALUES (42, 'search', ?)",
                ('{"query":"synthetic"}',),
            )
            service = AnalyticsService(database)
            users = await service.get_users_page()
            self.assertEqual(users[0]["user_id"], 42)
            self.assertIsNone(users[0]["username"])
            self.assertEqual(users[0]["event_count"], 2)
            top = await service.get_top_items_with_names("mob")
            self.assertEqual(top, [{"target_id": 7, "name": "[Удалён ID 7]", "emoji": "❓", "views": 1}])
            self.assertEqual(await service.get_top_search_queries(), [{"query": "synthetic", "count": 1}])
            self.assertEqual(
                await service.get_top_search_queries(search_type="text"),
                [{"query": "synthetic", "count": 1}],
            )
            details = await service.get_user_activity(42)
            self.assertIsNotNone(details)
            assert details is not None
            self.assertEqual(details["totals"]["total_events"], 2)
            self.assertEqual(details["recent_searches"][0]["query"], "synthetic")
            self.assertIsNone(await service.get_user_activity(43))
            counts = await service.get_db_stats()
            self.assertEqual((counts["users"], counts["events"]), (1, 2))
        finally:
            await database.close()

    async def test_retention_counts_users_once_on_the_exact_return_day(self):
        database = Database(":memory:")
        await database.connect()
        try:
            for user_id in (1, 2, 3):
                await database.execute_query(
                    "INSERT INTO users(user_id, first_seen) VALUES (?, datetime('now', '-7 days'))",
                    (user_id,),
                )
            for user_id, offset in ((1, "0 days"), (1, "0 days"), (2, "-1 day")):
                await database.execute_query(
                    "INSERT INTO analytics_events(user_id, event_type, timestamp) "
                    "VALUES (?, 'view_mob', datetime('now', ?))",
                    (user_id, offset),
                )
            service = AnalyticsService(database)
            self.assertAlmostEqual(await service.get_retention(7, 7), 100 / 3)
            self.assertEqual(await service.get_retention(30, 30), 0)
            with self.assertRaises(ValueError):
                await service.get_retention(1, 7)
        finally:
            await database.close()


class EmojiValidationTests(unittest.TestCase):
    def test_unicode_sequences_and_existing_combinations(self):
        for value in ("🔥🪖", "⚗️🧶", "🧏‍♀️", "🇷🇺", "👩🏽‍💻", "1️⃣", "⛓", "❤️"):
            with self.subTest(value=value):
                self.assertTrue(is_valid_emoji(value))

    def test_text_html_fragments_and_excessive_emoji_are_rejected(self):
        for value in ("", " ", "word", "12", "<b>", "🔥text", "🔥 🪖", "🔥\u200d", "🔥" * 9):
            with self.subTest(value=value):
                self.assertFalse(is_valid_emoji(value))

    def test_html_escaping_keeps_zero(self):
        self.assertEqual(escape_html(0), "0")
        self.assertEqual(escape_html(None), "")
        self.assertEqual(escape_html("<b>"), "&lt;b&gt;")
