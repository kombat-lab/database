import asyncio
import tempfile
import unittest
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey

from database import Database
from fsm_storage import SQLiteFSMStorage, ScopedEventIsolation, storage_key
from runtime_settings import AppSettings


class DurableConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "conversation.db")
        self.db = Database(self.path)
        await self.db.connect()
        self.now = 1000.0
        self.storage = SQLiteFSMStorage(self.db, ttl_seconds=100, clock=lambda: self.now)
        self.key = StorageKey(bot_id=1, chat_id=10, user_id=20)
        self.state = FSMContext(self.storage, self.key)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def test_unfinished_form_survives_database_and_storage_restart(self):
        await self.state.set_state("ResourceAddStates:note")
        await self.state.update_data(
            res_name="Сталь", catalog_source_mob_ids=[1, 2], nested={"screen": "token", "optional": None}
        )
        await self.db.close()
        self.db = Database(self.path)
        await self.db.connect()
        restored = FSMContext(SQLiteFSMStorage(self.db, ttl_seconds=100, clock=lambda: self.now), self.key)
        self.assertEqual(await restored.get_state(), "ResourceAddStates:note")
        self.assertEqual((await restored.get_data())["catalog_source_mob_ids"], [1, 2])
        self.assertEqual((await restored.get_data())["nested"], {"screen": "token", "optional": None})
        await restored.clear()
        self.assertEqual(await self.db.execute_query("SELECT key FROM fsm_sessions"), [])

    async def test_reading_and_clearing_idle_users_does_not_accumulate_sessions(self):
        for i in range(100):
            context = FSMContext(self.storage, StorageKey(bot_id=1, chat_id=i, user_id=i))
            self.assertIsNone(await context.get_state())
            self.assertEqual(await context.get_data(), {})
            await context.clear()
        self.assertEqual(await self.db.execute_query("SELECT key FROM fsm_sessions"), [])

    async def test_expiry_cannot_restore_stale_fields_and_pruning_is_bounded(self):
        await self.state.set_state("Editing")
        await self.state.update_data(old="do not restore")
        self.now += 101
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        await self.state.update_data(new="valid")
        self.assertEqual(await self.state.get_data(), {"new": "valid"})
        self.now += 101
        self.assertEqual(await self.storage.prune(limit=1), 1)
        self.assertEqual(await self.storage.prune(limit=1), 0)

    async def test_parallel_partial_updates_do_not_lose_fields(self):
        await asyncio.gather(*(self.state.update_data({str(index): index}) for index in range(20)))
        self.assertEqual(await self.state.get_data(), {str(index): index for index in range(20)})

    async def test_failed_transaction_rolls_back_session_with_catalog_change(self):
        await self.state.set_state("Editing")
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            async with self.db.transaction():
                await self.state.clear()
                raise RuntimeError("rollback")
        self.assertEqual(await self.state.get_state(), "Editing")

    async def test_invalid_json_rejected_without_overwriting_draft(self):
        await self.state.update_data(valid="preserved")
        for invalid in ({"object": object()}, {"float": float("nan")}, {"big": "x" * 600000}, {1: "bad key"}):
            with self.subTest(invalid_type=type(invalid)), self.assertRaises(ValueError):
                await self.state.set_data(invalid)
            self.assertEqual(await self.state.get_data(), {"valid": "preserved"})

    async def test_full_storage_identity_is_preserved(self):
        keys = [
            self.key,
            StorageKey(bot_id=2, chat_id=10, user_id=20),
            StorageKey(bot_id=1, chat_id=10, user_id=20, thread_id=30),
            StorageKey(bot_id=1, chat_id=10, user_id=20, destiny="a:b"),
            StorageKey(bot_id=1, chat_id=10, user_id=20, business_connection_id="a:b"),
        ]
        self.assertEqual(len({storage_key(key) for key in keys}), len(keys))
        for index, key in enumerate(keys):
            await self.storage.set_data(key, {"index": index})
        for index, key in enumerate(keys):
            self.assertEqual(await self.storage.get_data(key), {"index": index})


class EventIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_locks_are_released_immediately(self):
        isolation = ScopedEventIsolation()
        for index in range(1000):
            async with isolation.lock(StorageKey(bot_id=1, chat_id=index, user_id=index)):
                self.assertEqual(isolation.active_keys, 1)
        self.assertEqual(isolation.active_keys, 0)
        await isolation.close()

    async def test_cancelled_waiter_does_not_split_lock_for_remaining_users(self):
        isolation = ScopedEventIsolation()
        key = StorageKey(bot_id=1, chat_id=1, user_id=1)
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []

        async def first():
            async with isolation.lock(key):
                entered.set()
                await release.wait()
                seen.append(1)

        async def waiter(number):
            async with isolation.lock(key):
                seen.append(number)

        task = asyncio.create_task(first())
        await entered.wait()
        cancelled = asyncio.create_task(waiter(2))
        third = asyncio.create_task(waiter(3))
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        fourth = asyncio.create_task(waiter(4))
        await asyncio.sleep(0)
        self.assertEqual(seen, [])
        self.assertEqual(isolation.active_keys, 1)
        release.set()
        await asyncio.gather(task, third, fourth)
        self.assertEqual(seen, [1, 3, 4])
        self.assertEqual(isolation.active_keys, 0)

    async def test_close_keeps_held_entry_until_holder_finishes(self):
        isolation = ScopedEventIsolation()
        key = StorageKey(bot_id=1, chat_id=1, user_id=1)
        async with isolation.lock(key):
            await isolation.close()
            self.assertEqual(isolation.active_keys, 1)
            with self.assertRaises(RuntimeError):
                async with isolation.lock(key):
                    pass
        self.assertEqual(isolation.active_keys, 0)


class RuntimeSettingsTests(unittest.TestCase):
    def test_strict_ids_limits_and_secret_repr(self):
        settings = AppSettings.from_env({"BOT_TOKEN": "secret", "ADMIN_ID": "1, 2,1", "FSM_TTL_DAYS": "7"})
        self.assertEqual(settings.admin_ids, frozenset({1, 2}))
        self.assertEqual(settings.fsm_ttl_seconds, 7 * 86400)
        self.assertNotIn("secret", repr(settings))
        self.assertTrue(Path(settings.database_path).is_absolute())
        for key, value in [
            ("ADMIN_ID", "abc"),
            ("ADMIN_ID", "0"),
            ("ADMIN_ID", "١"),
            ("UPDATE_CONCURRENCY", "0"),
            ("FSM_TTL_DAYS", "-1"),
            ("ALLOW_EMPTY_DATABASE", "maybe"),
        ]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                AppSettings.from_env({"BOT_TOKEN": "secret", key: value})
