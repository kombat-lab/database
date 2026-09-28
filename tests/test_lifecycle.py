import asyncio
import unittest
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Chat, Message, Update, User

from lifecycle import BackgroundTaskRegistry, UpdateTaskTracker, install_update_tracker


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_updates_waiting_for_fsm_lock(self):
        tracker = UpdateTaskTracker()
        dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
        install_update_tracker(dispatcher, tracker)
        bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        entered = asyncio.Event()
        release = asyncio.Event()
        completed = []

        @dispatcher.message()
        async def handler(message: Message):
            if message.message_id == 1:
                entered.set()
                await release.wait()
            completed.append(message.message_id)

        def update(number):
            return Update(
                update_id=number,
                message=Message(
                    message_id=number,
                    date=datetime.now(timezone.utc),
                    chat=Chat(id=901, type="private"),
                    from_user=User(id=901, is_bot=False, first_name="Synthetic"),
                    text="test",
                ),
            )

        try:
            first = asyncio.create_task(dispatcher.feed_update(bot, update(1)))
            await entered.wait()
            second = asyncio.create_task(dispatcher.feed_update(bot, update(2)))
            async with asyncio.timeout(2):
                while tracker.active_count < 2:
                    await asyncio.sleep(0)
            closing = asyncio.create_task(tracker.close(timeout=2))
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            self.assertEqual(completed, [])
            release.set()
            await closing
            await asyncio.gather(first, second)
            self.assertEqual(completed, [1, 2])
            await dispatcher.feed_update(bot, update(3))
            self.assertEqual(completed, [1, 2])
        finally:
            release.set()
            await bot.session.close()
            await dispatcher.fsm.close()

    async def test_timeout_awaits_cleanup_despite_repeated_caller_cancellation(self):
        registry = BackgroundTaskRegistry()
        entered = asyncio.Event()
        cleanup_started = asyncio.Event()
        cleanup_release = asyncio.Event()
        cleaned = asyncio.Event()

        async def worker():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()

        task = registry.create_task(worker())
        await entered.wait()
        closing = asyncio.create_task(registry.close(timeout=0))
        await cleanup_started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        closing.cancel()
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        cleanup_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertTrue(cleaned.is_set())
        self.assertTrue(task.cancelled())
        await registry.close()

    async def test_closed_registry_rejects_and_closes_new_coroutine(self):
        registry = BackgroundTaskRegistry()
        await registry.close()

        async def work():
            return 1

        coroutine = work()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            registry.create_task(coroutine)
        self.assertIsNone(coroutine.cr_frame)

    async def test_background_failures_are_observed_and_logged(self):
        registry = BackgroundTaskRegistry()

        async def broken():
            raise ValueError("synthetic failure")

        with self.assertLogs("lifecycle", level="ERROR") as logs:
            registry.create_task(broken())
            await registry.close()
        self.assertIn("synthetic failure", "\n".join(logs.output))

    async def test_tracker_can_be_installed_with_fsm_disabled(self):
        dispatcher = Dispatcher(disable_fsm=True)
        tracker = UpdateTaskTracker()
        install_update_tracker(dispatcher, tracker)
        await tracker.close()
        await dispatcher.fsm.close()
