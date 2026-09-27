import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.filters import Command

from app import create_application
from database import Database
from runtime_settings import AppSettings

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot(TOKEN)
        self.settings = AppSettings(bot_token=TOKEN, database_path=":memory:", admin_ids=frozenset({101}))

    async def asyncTearDown(self):
        await self.bot.session.close()

    def update(self, number, text="/start", user_id=101):
        return types.Update(
            update_id=number,
            message=types.Message(
                message_id=number,
                date=1,
                chat=types.Chat(id=user_id, type="private"),
                from_user=types.User(id=user_id, is_bot=False, first_name="Synthetic"),
                text=text,
            ),
        )

    async def test_shutdown_drains_actual_dispatcher_updates_before_database_close(self):
        db = Database(":memory:")
        application = create_application(self.settings, db)
        entered, release = asyncio.Event(), asyncio.Event()
        completed = []
        observed = []

        async def handler(message):
            entered.set()
            await release.wait()
            await db.execute_query("SELECT 1")
            completed.append("update")

        application.dispatcher.message(Command("work"))(handler)

        async def background():
            await release.wait()
            await db.execute_query("SELECT 1")
            completed.append("background")

        async def poll(*args, **kwargs):
            observed.append(asyncio.create_task(application.dispatcher.feed_update(self.bot, self.update(1, "/work"))))
            application.background_tasks.create_task(background())
            await entered.wait()
            asyncio.get_running_loop().call_soon(release.set)
            await application.dispatcher.emit_shutdown(bot=self.bot)
            self.assertIn("update", completed)

        with (
            patch.object(self.bot, "me", AsyncMock(return_value=SimpleNamespace(username="test_bot"))),
            patch.object(self.bot, "delete_webhook", AsyncMock()) as delete,
            patch.object(application.dispatcher, "start_polling", AsyncMock(side_effect=poll)),
        ):
            await application.run(self.bot)
        await asyncio.gather(*observed)
        self.assertEqual(set(completed), {"update", "background"})
        delete.assert_awaited_once_with(drop_pending_updates=False)
        self.assertEqual(application.update_tasks.active_count, 0)
        self.assertEqual(application.dispatcher.fsm.events_isolation.active_keys, 0)
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            await db.execute_query("SELECT 1")

    async def test_startup_failure_closes_database_and_bot_without_polling(self):
        db = Database(":memory:")
        application = create_application(self.settings, db)
        with (
            patch.object(db, "connect", AsyncMock(side_effect=RuntimeError("cannot open"))),
            patch.object(db, "close", AsyncMock()) as close,
            patch.object(self.bot.session, "close", AsyncMock()) as session_close,
            patch.object(application.dispatcher, "start_polling", AsyncMock()) as poll,
        ):
            with self.assertRaisesRegex(RuntimeError, "cannot open"):
                await application.run(self.bot)
        close.assert_awaited_once()
        session_close.assert_awaited_once()
        poll.assert_not_awaited()

    async def test_fresh_applications_keep_public_access_permissions_and_data_independent(self):
        first_db, second_db = Database(":memory:"), Database(":memory:")
        first = create_application(self.settings, first_db)
        second_settings = AppSettings(bot_token=TOKEN, database_path=":memory:", admin_ids=frozenset({202}))
        second = create_application(second_settings, second_db)
        other_bot = Bot(TOKEN)
        answers = []

        async def answer(text, **kwargs):
            answers.append(text)
            return types.Message(message_id=50, date=1, chat=types.Chat(id=101, type="private"), text=text).as_(
                self.bot
            )

        async def first_poll(*args, **kwargs):
            await first.dispatcher.feed_update(self.bot, self.update(1, "/start", 303))
            self.assertTrue(any("меню" in item.lower() or "справочник" in item.lower() for item in answers))
            await first.dispatcher.feed_update(self.bot, self.update(2, "/kombat", 101))
            self.assertIn("Админ-панель", answers[-1])
            self.assertEqual(
                [row["user_id"] for row in await first_db.execute_query("SELECT user_id FROM users ORDER BY user_id")],
                [101, 303],
            )

        async def second_poll(*args, **kwargs):
            await second.dispatcher.feed_update(other_bot, self.update(3, "/kombat", 101))
            self.assertIn("Нет доступа", answers[-1])
            self.assertEqual(
                [row["user_id"] for row in await second_db.execute_query("SELECT user_id FROM users")], [101]
            )

        try:
            with (
                patch.object(types.Message, "answer", AsyncMock(side_effect=answer)),
                patch.object(self.bot, "me", AsyncMock(return_value=SimpleNamespace(username="first_bot"))),
                patch.object(other_bot, "me", AsyncMock(return_value=SimpleNamespace(username="second_bot"))),
                patch.object(self.bot, "delete_webhook", AsyncMock()),
                patch.object(other_bot, "delete_webhook", AsyncMock()),
                patch.object(first.dispatcher, "start_polling", AsyncMock(side_effect=first_poll)),
                patch.object(second.dispatcher, "start_polling", AsyncMock(side_effect=second_poll)),
            ):
                await first.run(self.bot)
                await second.run(other_bot)
        finally:
            await other_bot.session.close()
        self.assertIsNot(first.dispatcher.sub_routers[0], second.dispatcher.sub_routers[0])
        self.assertIsNot(first.dispatcher.sub_routers[1], second.dispatcher.sub_routers[1])

    async def test_catalog_changes_receive_actor_and_update_identity(self):
        db = Database(":memory:")
        application = create_application(self.settings, db)

        async def write(message):
            await db.add_resource("Сталь", "🪨")

        application.dispatcher.message(Command("write"))(write)

        async def poll(*args, **kwargs):
            await application.dispatcher.feed_update(self.bot, self.update(71, "/write"))
            rows = await db.execute_query(
                "SELECT actor_user_id,operation_id,source FROM catalog_changes WHERE table_name='resources'"
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["actor_user_id"], 101)
            self.assertEqual(rows[0]["operation_id"], f"telegram:{self.bot.id}:71")
            self.assertEqual(rows[0]["source"], "telegram")

        with (
            patch.object(self.bot, "me", AsyncMock(return_value=SimpleNamespace(username="test_bot"))),
            patch.object(self.bot, "delete_webhook", AsyncMock()),
            patch.object(application.dispatcher, "start_polling", AsyncMock(side_effect=poll)),
        ):
            await application.run(self.bot)

    async def test_typo_in_database_path_does_not_create_empty_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.db"
            settings = AppSettings(bot_token=TOKEN, database_path=str(path))
            with self.assertRaisesRegex(ValueError, "does not exist"):
                create_application(settings)
            self.assertFalse(path.exists())
