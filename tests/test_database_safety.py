import asyncio
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import Database, SCHEMA_VERSION


class DatabaseSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="fog-db-safety-")
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(str(self.db_path))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def wait_worker(self, event):
        self.assertTrue(await asyncio.to_thread(event.wait, 3), "SQLite worker did not reach checkpoint")

    def assert_connection_settled(self):
        self.assertFalse(self.db._conn.in_transaction)
        self.assertEqual(self.db._transaction_depth, 0)
        self.assertFalse(self.db._connection_lock.locked())

    async def test_cancel_while_begin_waits_for_another_writer(self):
        resource_id = await self.db.add_resource("original", "")
        writer = sqlite3.connect(self.db_path)
        started = threading.Event()
        await self.db._conn.set_trace_callback(
            lambda sql: started.set() if sql == "BEGIN IMMEDIATE" else None
        )
        task = None
        try:
            writer.execute("BEGIN IMMEDIATE")
            task = asyncio.create_task(self.db.update_resource(resource_id, name="cancelled"))
            await self.wait_worker(started)
            task.cancel()
            await asyncio.sleep(0)
            writer.rollback()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            writer.rollback()
            writer.close()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_connection_settled()
        self.assertEqual((await self.db.get_resource_by_id(resource_id))["name"], "original")
        await self.db.update_resource(resource_id, name="next edit")
        self.assertEqual((await self.db.get_resource_by_id(resource_id))["name"], "next edit")

    async def test_repeated_cancel_during_insert_rolls_back_before_next_writer(self):
        started, release = threading.Event(), threading.Event()

        def slow(value):
            started.set()
            release.wait(3)
            return value

        await self.db._conn.create_function("slow", 1, slow)
        task = asyncio.create_task(self.db.execute_insert(
            "INSERT INTO resources (name) VALUES (slow(?))", ("cancelled",)
        ))
        try:
            await self.wait_worker(started)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assert_connection_settled()
        await self.db.add_resource("unrelated", "")
        with closing(sqlite3.connect(self.db_path)) as reader:
            self.assertEqual(reader.execute("SELECT name FROM resources").fetchall(), [("unrelated",)])

    async def test_cancel_execute_query_rolls_back(self):
        started, release = threading.Event(), threading.Event()

        def slow(value):
            started.set()
            release.wait(3)
            return value

        await self.db._conn.create_function("slow", 1, slow)
        task = asyncio.create_task(self.db.execute_query(
            "INSERT INTO resources (name) VALUES (slow(?))", ("cancelled",)
        ))
        try:
            await self.wait_worker(started)
            task.cancel()
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        self.assert_connection_settled()
        self.assertEqual(await self.db.execute_query("SELECT name FROM resources"), [])

    async def test_cancel_transaction_body_rolls_back(self):
        ready = asyncio.Event()

        async def write():
            async with self.db.transaction():
                await self.db.add_resource("rolled back", "")
                ready.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(write())
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_connection_settled()
        self.assertEqual(await self.db.execute_query("SELECT name FROM resources"), [])

    async def test_cancel_nested_transaction_preserves_outer_transaction(self):
        ready = asyncio.Event()

        async def write():
            async with self.db.transaction():
                await self.db.add_resource("first", "")
                try:
                    async with self.db.transaction():
                        await self.db.add_resource("cancelled nested", "")
                        ready.set()
                        await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass
                self.assertEqual(self.db._transaction_depth, 1)
                await self.db.add_resource("last", "")

        task = asyncio.create_task(write())
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        await asyncio.wait_for(task, 3)
        self.assert_connection_settled()
        self.assertEqual(
            await self.db.execute_query("SELECT name FROM resources ORDER BY id"),
            [{"name": "first"}, {"name": "last"}],
        )

    async def test_nested_rollback_failure_cannot_commit_aborted_inner_work(self):
        connection = self.db._conn
        original_execute = connection.execute

        def fail_rollback(sql, *args):
            if sql.startswith("ROLLBACK TO SAVEPOINT"):
                raise sqlite3.OperationalError("injected rollback failure")
            return original_execute(sql, *args)

        with self.assertRaisesRegex(RuntimeError, "not connected"):
            async with self.db.transaction():
                await self.db.add_resource("outer before", "")
                with patch.object(connection, "execute", side_effect=fail_rollback):
                    try:
                        async with self.db.transaction():
                            await self.db.add_resource("must never commit", "")
                            raise ValueError("abort inner")
                    except sqlite3.OperationalError:
                        pass
                with self.assertRaisesRegex(RuntimeError, "unfinished transaction"):
                    await self.db.connect()
                # Even if the caller swallows the rollback failure, outer exit
                # must fail and cannot commit the inner changes.
        self.assertIsNone(self.db._conn)
        self.assertEqual(self.db._transaction_depth, 0)
        await self.db.connect()
        self.assertEqual(await self.db.execute_query("SELECT name FROM resources"), [])
        await self.db.add_resource("fresh connection", "")

    async def test_query_queued_after_close_reports_disconnected_connection(self):
        async with self.db._connection_guard():
            close_task = asyncio.create_task(self.db.close())
            query_task = asyncio.create_task(self.db.execute_query("SELECT 1"))
            await asyncio.sleep(0)
        await close_task
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            await query_task
        await self.db.connect()
        self.assert_connection_settled()

    async def test_cancel_commit_drains_sqlite_before_connection_reuse(self):
        # DELETE journal mode lets a real reader hold COMMIT pending without mocks.
        await self.db.execute_query("PRAGMA journal_mode = DELETE")
        reader = sqlite3.connect(self.db_path)
        started = threading.Event()
        await self.db._conn.set_trace_callback(
            lambda sql: started.set() if sql == "COMMIT" else None
        )

        async def write():
            async with self.db.transaction():
                await self.db.add_resource("committed at boundary", "")

        task = None
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM resources").fetchall()
            task = asyncio.create_task(write())
            await self.wait_worker(started)
            task.cancel()
            await asyncio.sleep(0)
            reader.rollback()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        finally:
            reader.rollback()
            reader.close()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assert_connection_settled()
        await self.db.update_resource(1, name="next edit")
        self.assertEqual((await self.db.get_resource_by_id(1))["name"], "next edit")

    async def test_failed_commit_rolls_back_and_does_not_poison_connection(self):
        with self.assertRaises(sqlite3.IntegrityError):
            async with self.db.transaction():
                await self.db.execute_query("PRAGMA defer_foreign_keys = ON")
                await self.db.execute_query(
                    "INSERT INTO recipe_ingredients (recipe_id, resource_id, quantity) VALUES (999, 999, 1)"
                )
        self.assert_connection_settled()
        self.assertEqual(await self.db.execute_query("SELECT * FROM recipe_ingredients"), [])
        await self.db.add_resource("still works", "")

    async def test_recipe_creation_rejects_deleted_result_and_duplicate_race(self):
        gear_id = await self.db.add_gear("item", "epic", "helmet", "")
        await self.db.delete_gear(gear_id)
        with self.assertRaisesRegex(ValueError, "удалён"):
            await self.db.create_recipe("gear", gear_id)
        with self.assertRaises(ValueError):
            await self.db.create_recipe("invalid", gear_id)
        self.assertEqual(await self.db.execute_query("SELECT * FROM recipes"), [])
        gear_id = await self.db.add_gear("valid", "epic", "helmet", "")
        results = await asyncio.gather(
            self.db.create_recipe("gear", gear_id),
            self.db.create_recipe("gear", gear_id),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, int) for result in results), 1)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)

    async def test_concurrent_ingredient_add_preserves_original_quantity(self):
        gear_id = await self.db.add_gear("item", "epic", "helmet", "")
        recipe_id = await self.db.create_recipe("gear", gear_id)
        resource_id = await self.db.add_resource("ore", "")
        results = await asyncio.gather(
            self.db.add_ingredient(recipe_id, resource_id, 2),
            self.db.add_ingredient(recipe_id, resource_id, 3),
            return_exceptions=True,
        )
        self.assertEqual(sum(result is None for result in results), 1)
        errors = [result for result in results if isinstance(result, ValueError)]
        self.assertEqual(len(errors), 1)
        self.assertIn("уже есть", str(errors[0]))
        rows = await self.db.execute_query("SELECT quantity FROM recipe_ingredients")
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["quantity"], (2, 3))
        for invalid in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                await self.db.add_ingredient(recipe_id, resource_id, invalid)

    async def test_dust_updates_validate_current_range_under_transaction(self):
        location_id = await self.db.execute_insert("INSERT INTO locations(name) VALUES ('forest')")
        mob_id = await self.db.execute_insert(
            "INSERT INTO mobs(name, location_id, dust_min, dust_max) VALUES ('mob', ?, 1, 10)",
            (location_id,),
        )
        results = await asyncio.gather(
            self.db.update_mob_field(mob_id, "dust_min", 9),
            self.db.update_mob_field(mob_id, "dust_max", 5),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        rows = await self.db.execute_query("SELECT dust_min,dust_max FROM mobs WHERE id = ?", (mob_id,))
        self.assertLessEqual(rows[0]["dust_min"], rows[0]["dust_max"])
        with self.assertRaises(ValueError):
            await self.db.update_mob_field(mob_id, "dust_min", -1)

    async def test_search_treats_wildcards_and_escape_as_literal_in_every_table(self):
        location_id = await self.db.execute_insert("INSERT INTO locations(name) VALUES ('forest')")
        literal_name = r"Mixed_%\name"
        for name in (literal_name, "Mixed ordinary name"):
            await self.db.add_resource(name, "")
            await self.db.add_gear(name, "epic", "helmet", "")
            await self.db.add_card(name, "", "helmet")
            await self.db.execute_insert(
                "INSERT INTO mobs(name,location_id) VALUES (?, ?)", (name, location_id)
            )
        for query in ("%", "_", "\\", "mixed_%"):
            result = await self.db.search(query)
            for table in ("resources", "gear", "cards", "mobs"):
                self.assertEqual([row["name"] for row in result[table]], [literal_name], (query, table))

    async def test_linked_owners_survive_rename_and_do_not_absorb_manual_names(self):
        gear_id = await self.db.add_gear("item", "epic", "helmet", "")
        recipe_id = await self.db.create_recipe("gear", gear_id)
        await self.db.add_recipe_owner(recipe_id, "OldName")
        await self.db.add_recipe_owner(recipe_id, "oldname")
        await self.db.claim_recipe_owner(recipe_id, 101, "OldName", expected_gear_id=gear_id)
        entries = await self.db.get_recipe_owner_entries(recipe_id)
        self.assertEqual(len(entries), 2)
        self.assertEqual({entry["user_id"] for entry in entries}, {None, 101})
        await self.db.register_user_if_not_exists(101, "NewName")
        gear = await self.db.get_gear_card(gear_id)
        self.assertEqual(gear["owner_user_ids"], [101])
        self.assertEqual(set(gear["owners"]), {"OldName", "NewName"})
        await self.db.register_user_if_not_exists(101, None)
        entries = await self.db.get_recipe_owner_entries(recipe_id)
        self.assertIsNone(next(entry["player_username"] for entry in entries if entry["user_id"] == 101))
        await self.db.claim_recipe_owner(recipe_id, 202, "NewName", expected_gear_id=gear_id)
        await self.db.relinquish_recipe_owner(recipe_id, 202)
        self.assertIn(101, (await self.db.get_gear_card(gear_id))["owner_user_ids"])
        await self.db.relinquish_recipe_owner(recipe_id, 101)
        self.assertEqual(await self.db.get_recipe_owners(recipe_id), ["OldName"])
        await self.db.remove_recipe_owner(recipe_id, "OLDNAME")
        self.assertEqual(await self.db.get_recipe_owner_entries(recipe_id), [])

    async def test_claim_is_idempotent_and_rechecks_result_gear_and_rarity(self):
        gear_id = await self.db.add_gear("item", "epic", "helmet", "")
        recipe_id = await self.db.create_recipe("gear", gear_id)
        await asyncio.gather(*(
            self.db.claim_recipe_owner(recipe_id, 101, "Owner", expected_gear_id=gear_id)
            for _ in range(3)
        ))
        self.assertEqual(len(await self.db.get_recipe_owner_entries(recipe_id)), 1)
        with self.assertRaises(ValueError):
            await self.db.claim_recipe_owner(recipe_id, 202, "Other", expected_gear_id=gear_id + 1)
        await self.db.update_gear(gear_id, rarity="rare")
        with self.assertRaises(ValueError):
            await self.db.claim_recipe_owner(recipe_id, 202, "Other")
        entry = (await self.db.get_recipe_owner_entries(recipe_id))[0]
        await self.db.remove_recipe_owner_entry(recipe_id + 1, entry["owner_id"])
        self.assertEqual(len(await self.db.get_recipe_owner_entries(recipe_id)), 1)
        await self.db.remove_recipe_owner_entry(recipe_id, entry["owner_id"])
        self.assertEqual(await self.db.get_recipe_owner_entries(recipe_id), [])


class OwnerMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="fog-owner-migration-")
        self.path = Path(self.temp_dir.name) / "legacy.db"

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_legacy(self, *, orphan=False, version=0):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE recipes (
                    id INTEGER PRIMARY KEY, result_type TEXT NOT NULL,
                    result_id INTEGER NOT NULL, quantity INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(result_type, result_id)
                );
                INSERT INTO recipes VALUES (7, 'gear', 9, 1);
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY, username TEXT,
                    first_seen TEXT DEFAULT CURRENT_TIMESTAMP, last_activity TEXT DEFAULT CURRENT_TIMESTAMP,
                    first_name TEXT, last_name TEXT
                );
                INSERT INTO users(user_id,username) VALUES (101, 'LegacyOwner');
                CREATE TABLE recipe_owners (
                    recipe_id INTEGER NOT NULL, player_username TEXT NOT NULL,
                    PRIMARY KEY(recipe_id,player_username),
                    FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
                );
                INSERT INTO recipe_owners VALUES (7,'LegacyOwner'), (7,'legacyowner');
                CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO schema_metadata VALUES ('unrelated_metadata','keep');
            """)
            if orphan:
                connection.execute("INSERT INTO recipe_owners VALUES (999,'Orphan')")
            connection.execute(f"PRAGMA user_version = {version}")

    async def test_legacy_migration_deduplicates_manual_names_without_claiming_identity(self):
        self.create_legacy()
        db = Database(str(self.path))
        try:
            await db.connect()
            owners = await db.get_recipe_owner_entries(7)
            self.assertEqual(len(owners), 1)
            self.assertIsNone(owners[0]["user_id"])
            self.assertEqual(owners[0]["player_username"], "LegacyOwner")
            self.assertEqual((await db.execute_query("PRAGMA user_version"))[0]["user_version"], SCHEMA_VERSION)
            self.assertEqual(await db.execute_query("SELECT value FROM schema_metadata"), [{"value": "keep"}])
            await db.close()
            await db.connect()
            self.assertEqual(await db.get_recipe_owner_entries(7), owners)
            self.assertEqual(await db.execute_query("PRAGMA foreign_key_check"), [])
        finally:
            await db.close()

    async def test_failed_migration_keeps_legacy_table_data_and_version(self):
        self.create_legacy(orphan=True)
        db = Database(str(self.path))
        with self.assertRaises(sqlite3.IntegrityError):
            await db.connect()
        self.assertIsNone(db._conn)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(recipe_owners)")}
            self.assertEqual(columns, {"recipe_id", "player_username"})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recipe_owners").fetchone()[0], 3)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='recipe_owners_v1'").fetchone())

    async def test_newer_schema_is_rejected_before_any_schema_changes(self):
        self.create_legacy(version=SCHEMA_VERSION + 1)
        db = Database(str(self.path))
        with self.assertRaisesRegex(RuntimeError, "newer"):
            await db.connect()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='gear'").fetchone())
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recipe_owners").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "delete")
