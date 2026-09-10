
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot as app
from lifecycle import BackgroundTaskRegistry, UpdateTaskTracker


class ApplicationLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_keeps_updates_and_shutdown_finishes_accepted_work(self):
        tracker = UpdateTaskTracker()
        background = BackgroundTaskRegistry()
        db = AsyncMock()
        fake_bot = AsyncMock()
        fake_bot.me.return_value = SimpleNamespace(username="test_bot")
        closed = False
        completed = []
        entered = asyncio.Event()
        resume = asyncio.Event()
        observed_tasks = []

        async def close_db():
            nonlocal closed
            self.assertEqual(set(completed), {"update", "background"})
            closed = True

        async def handler(event, data):
            entered.set()
            await resume.wait()
            self.assertFalse(closed)
            completed.append("update")

        async def bg():
            await resume.wait()
            self.assertFalse(closed)
            completed.append("background")

        async def poll(*args, **kwargs):
            observed_tasks.append(asyncio.create_task(tracker(handler, SimpleNamespace(), {})))
            background.create_task(bg())
            await entered.wait()
            asyncio.get_running_loop().call_soon(resume.set)
            # Model polling ending while an accepted update is still in flight.

        db.close.side_effect = close_db
        with (
            patch.object(app.logging, "basicConfig"),
            patch.dict(os.environ, {"BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"}),
            patch.object(app, "db", db),
            patch.object(app, "Bot", return_value=fake_bot),
            patch.object(app, "update_tasks", tracker),
            patch.object(app, "background_tasks", background),
            patch.object(app.dp, "include_router"),
            patch.object(app.dp.update, "middleware"),
            patch.object(app.dp, "start_polling", new=AsyncMock(side_effect=poll)),
        ):
            await app.main()
        fake_bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        self.assertTrue(closed)
        self.assertTrue(all(task.done() for task in observed_tasks))
        fake_bot.session.close.assert_awaited_once()

    async def test_startup_failure_still_closes_database_and_session(self):
        db = AsyncMock()
        db.connect.side_effect = RuntimeError("database cannot open")
        fake_bot = AsyncMock()
        with (
            patch.object(app.logging, "basicConfig"),
            patch.dict(os.environ, {"BOT_TOKEN": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"}),
            patch.object(app, "db", db),
            patch.object(app, "Bot", return_value=fake_bot),
            patch.object(app, "update_tasks", UpdateTaskTracker()),
            patch.object(app, "background_tasks", BackgroundTaskRegistry()),
        ):
            with self.assertRaisesRegex(RuntimeError, "database cannot open"):
                await app.main()
        db.close.assert_awaited_once()
        fake_bot.session.close.assert_awaited_once()
        fake_bot.delete_webhook.assert_not_awaited()
