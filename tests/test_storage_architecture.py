import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database
from storage.types import SCHEMA_VERSION
from recipe_domain import DomainError, DraftConflictError, DuplicateIdentityError
from storage.context import catalog_operation
from storage.types import sql_int, sql_text, sql_row


class StorageArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fog-storage-")
        self.path = str(Path(self.temp.name) / "catalog.db")
        self.db = Database(self.path)
        await self.db.connect()
        self.location = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Лес','🌲')")
        self.mob = await self.db.add_mob("Страж", "🐺", 10, 1, 2, 3, self.location)
        self.context = dict(owner_user_id=10, chat_id=-100, message_id=20)

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def equipment(self):
        ore = await self.db.add_resource("Руда", "🪨")
        scroll = await self.db.add_resource("Свиток", "📜", "scroll_recipe")
        gear = await self.db.add_gear("Клинок", "rare", "основная рука", "🗡️")
        recipe = await self.db.create_recipe("gear", gear)
        await self.db.add_ingredient(recipe, ore, 2)
        await self.db.set_recipe_learning_scroll(recipe, scroll)
        return gear, recipe, scroll

    async def test_delete_checks_published_scroll_after_selecting_another_scroll(self):
        gear, recipe, scroll = await self.equipment()
        other = await self.db.add_resource("Другой свиток", "📜", "scroll_recipe")
        draft = await self.db.create_gear_draft(gear_id=gear, **self.context)
        value = draft["payload"]
        value["learning_scroll"] = {"resource_id": other}
        draft = await self.db.update_gear_draft(draft["draft_id"], expected_revision=0, payload=value, **self.context)
        await self.db.update_resource(scroll, note="Правка другого администратора")
        with self.assertRaises(DraftConflictError):
            await self.db.delete_gear_draft_target(
                draft["draft_id"],
                expected_revision=draft["revision"],
                delete_gear=True,
                delete_scroll=True,
                **self.context,
            )
        self.assertIsNotNone(await self.db.get_gear_by_id(gear))
        self.assertIsNotNone(await self.db.get_recipe_details(recipe))
        self.assertEqual((await self.db.get_resource_by_id(scroll))["note"], "Правка другого администратора")

    async def test_old_draft_without_original_reference_cannot_delete_scroll(self):
        gear, _, scroll = await self.equipment()
        draft = await self.db.create_gear_draft(gear_id=gear, **self.context)
        await self.db.execute_query(
            "UPDATE gear_drafts SET published_reference_fingerprints_json='{}' WHERE draft_id=?", (draft["draft_id"],)
        )
        with self.assertRaises(DraftConflictError):
            await self.db.delete_gear_draft_target(
                draft["draft_id"], expected_revision=0, delete_gear=True, delete_scroll=True, **self.context
            )
        self.assertIsNotNone(await self.db.get_resource_by_id(scroll))

    async def test_owner_requires_learning_and_legacy_rows_cannot_attach_to_new_scroll(self):
        gear = await self.db.add_gear("Шлем", "common", "шлем", "🪖")
        recipe = await self.db.create_recipe("gear", gear)
        with self.assertRaises(DomainError):
            await self.db.add_recipe_owner(recipe, "Player")
        await self.db.execute_query(
            "INSERT INTO recipe_owners(recipe_id,player_username) VALUES (?,?)", (recipe, "LegacyPlayer")
        )
        scroll = await self.db.add_resource("Изучение", "📜", "scroll_recipe")
        with self.assertRaises(DomainError):
            await self.db.set_recipe_learning_scroll(recipe, scroll)
        self.assertEqual(await self.db.get_recipe_owners(recipe), ["LegacyPlayer"])
        self.assertIsNone(await self.db.get_recipe_learning_scroll(recipe))

    async def test_search_ranks_exact_before_limit_and_pages_are_consistent(self):
        for number in range(60):
            await self.db.add_resource(f"Старая сталь {number:02}", "")
        prefix = await self.db.add_resource("Сталь высшая", "")
        exact = await self.db.add_resource("Сталь", "")
        first = (await self.db.search("  СТАЛЬ  ", limit=2))["resources"]
        second = (await self.db.search("сталь", offset=2, limit=2))["resources"]
        whole = (await self.db.search("сталь", limit=4))["resources"]
        self.assertEqual([item["id"] for item in first], [exact, prefix])
        self.assertEqual(first + second, whole)

    async def test_identity_is_unicode_normalized_and_variants_are_explicit(self):
        first = await self.db.add_resource("Ore  A", "")
        with self.assertRaises(DuplicateIdentityError):
            await self.db.add_resource("  ＯＲＥ A  ", "")
        second = await self.db.add_resource("ore a", "", allow_duplicate=True)
        self.assertNotEqual(first, second)
        self.assertEqual(len(await self.db.get_resource_name_matches("ORE A")), 2)
        await self.db.add_card("Карта", "🃏", "шлем")
        with self.assertRaises(DuplicateIdentityError):
            await self.db.add_card(" карта ", "🃏", "шлем")

    async def test_create_command_is_replayed_after_restart_and_rejects_changed_request(self):
        first = await self.db.create_resource_with_sources("Предмет", "", mob_ids=[self.mob], operation_id="form-1")
        await self.db.close()
        await self.db.connect()
        second = await self.db.create_resource_with_sources("Предмет", "", mob_ids=[self.mob], operation_id="form-1")
        self.assertEqual(first, second)
        self.assertEqual(len(await self.db.get_resource_name_matches("Предмет")), 1)
        with self.assertRaises(DraftConflictError):
            await self.db.create_resource_with_sources("Другой", "", mob_ids=[self.mob], operation_id="form-1")

    async def test_failed_creation_rolls_back_ledger_and_audit(self):
        before = await self.db.get_catalog_revision()
        with self.assertRaises(DomainError):
            await self.db.create_resource_with_sources("Не сохранён", "", mob_ids=[99999], operation_id="rollback")
        self.assertEqual(await self.db.get_catalog_revision(), before)
        self.assertEqual(
            await self.db.execute_query("SELECT * FROM command_results WHERE operation_key='resource:rollback'"), []
        )
        result = await self.db.create_resource_with_sources(
            "Не сохранён", "", mob_ids=[self.mob], operation_id="rollback"
        )
        self.assertIsNotNone(await self.db.get_resource_by_id(result))

    async def test_audit_has_atomic_before_after_actor_and_one_operation(self):
        with catalog_operation(actor_user_id=123, operation_id="one-operation", source="test"):
            resource = await self.db.create_resource_with_sources("Original", "", mob_ids=[self.mob])
            await self.db.update_resource(resource, name="Updated")
        rows = await self.db.execute_query(
            "SELECT * FROM catalog_changes WHERE operation_id='one-operation' ORDER BY id"
        )
        self.assertEqual([row["table_name"] for row in rows], ["resources", "drops", "resources"])
        self.assertTrue(all(row["actor_user_id"] == 123 and row["source"] == "test" for row in rows))
        self.assertEqual(json.loads(rows[-1]["before_json"])["name"], "Original")
        self.assertEqual(json.loads(rows[-1]["after_json"])["name"], "Updated")
        revision = await self.db.get_catalog_revision()
        with self.assertRaises(RuntimeError):
            async with self.db.transaction():
                await self.db.update_resource(resource, name="Rolled back")
                raise RuntimeError("abort")
        self.assertEqual(await self.db.get_catalog_revision(), revision)
        self.assertEqual((await self.db.get_resource_by_id(resource))["name"], "Updated")

    async def test_operation_attribution_does_not_leak_across_tasks(self):
        async def create(number):
            with catalog_operation(actor_user_id=number, operation_id=f"task-{number}"):
                await asyncio.sleep(0)
                await self.db.add_resource(f"Task {number}", "")

        await asyncio.gather(create(101), create(102))
        rows = await self.db.execute_query(
            "SELECT actor_user_id,operation_id,after_json FROM catalog_changes WHERE operation_id LIKE 'task-%'"
        )
        self.assertEqual(
            {(row["actor_user_id"], json.loads(row["after_json"])["name"]) for row in rows},
            {(101, "Task 101"), (102, "Task 102")},
        )

    async def test_drop_setting_is_idempotent_and_noop_does_not_bump_revision(self):
        resource = await self.db.add_resource("Дроп", "")
        await self.db.set_drop_enabled(self.mob, "resource", resource, True)
        revision = await self.db.get_catalog_revision()
        await self.db.set_drop_enabled(self.mob, "resource", resource, True)
        self.assertEqual(await self.db.get_catalog_revision(), revision)
        await self.db.set_drop_enabled(self.mob, "resource", resource, False)
        revision = await self.db.get_catalog_revision()
        await self.db.set_drop_enabled(self.mob, "resource", resource, False)
        self.assertEqual(await self.db.get_catalog_revision(), revision)

    async def test_read_snapshot_does_not_reserve_writer_lock(self):
        other = Database(self.path)
        await other.connect()
        try:
            async with self.db.read_transaction():
                before = await self.db.get_catalog_revision()
                resource = await asyncio.wait_for(other.add_resource("Concurrent", ""), 2)
                self.assertIsNone(await self.db.get_resource_by_id(resource))
                self.assertEqual(await self.db.get_catalog_revision(), before)
            self.assertIsNotNone(await self.db.get_resource_by_id(resource))
            self.assertGreater(self.db.metrics.counter, 0)
            self.assertGreater(self.db.metrics.total_seconds, 0)
        finally:
            await other.close()

    async def test_boolean_ids_cannot_mutate_record_one(self):
        resource = await self.db.add_resource("Свободный ресурс", "")
        card = await self.db.add_card("Карта", "🃏", "шлем")
        gear, recipe, _ = await self.equipment()
        before = await self.db.get_catalog_revision()
        for action in (
            lambda: self.db.delete_resource(True),
            lambda: self.db.delete_card(True),
            lambda: self.db.delete_gear(True),
            lambda: self.db.delete_mob(True),
            lambda: self.db.delete_recipe(True),
            lambda: self.db.claim_recipe_owner(recipe, True, None),
        ):
            with self.assertRaises(DomainError):
                await action()
        self.assertEqual(await self.db.get_catalog_revision(), before)
        self.assertIsNotNone(await self.db.get_resource_by_id(resource))
        self.assertIsNotNone(await self.db.get_card_by_id(card))
        self.assertIsNotNone(await self.db.get_gear_by_id(gear))
        self.assertIsNotNone(await self.db.get_mob_by_id(self.mob))
        self.assertIsNotNone(await self.db.get_recipe_details(recipe))

    async def test_mutating_api_rejects_invalid_values_without_writes(self):
        revision = await self.db.get_catalog_revision()
        for action in (
            lambda: self.db.add_resource("", ""),
            lambda: self.db.add_resource("bad", "text"),
            lambda: self.db.add_resource("bad", "", "missing-type"),
            lambda: self.db.add_card("bad", "", "missing-slot"),
            lambda: self.db.add_gear("bad", "rare", "шлем", "", True),
            lambda: self.db.update_card(1, unknown="field"),
            lambda: self.db.update_mob_field(self.mob, "name", 42),
            lambda: self.db.update_mob_field(self.mob, "location_id", 0),
            lambda: self.db.update_mob_field(self.mob, "exp", 2**63),
            lambda: self.db.set_drop_enabled(self.mob, "resource", 1, 1),
        ):
            with self.assertRaises(DomainError):
                await action()
        self.assertEqual(await self.db.get_catalog_revision(), revision)


class MetadataMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_metadata_is_backfilled_only_for_verified_names(self):
        for matching in (True, False):
            with self.subTest(matching=matching), tempfile.TemporaryDirectory(prefix="fog-metadata-") as temp:
                path = Path(temp) / "legacy.db"
                connection = sqlite3.connect(path)
                connection.executescript(
                    "CREATE TABLE resources(id INTEGER PRIMARY KEY,name TEXT NOT NULL,emoji TEXT NOT NULL DEFAULT '',type TEXT NOT NULL,note TEXT NOT NULL DEFAULT '');CREATE TABLE locations(id INTEGER PRIMARY KEY,name TEXT NOT NULL,emoji TEXT NOT NULL DEFAULT '');"
                )
                connection.execute(
                    "INSERT INTO resources(id,name,type) VALUES (71,?,'currency')", ("Пыль" if matching else "Other",)
                )
                connection.executemany(
                    "INSERT INTO locations(id,name) VALUES (?,?)",
                    [
                        (4, "Мертвый лес" if matching else "Other"),
                        (8, "Пещера"),
                        (9, "Подземная пещера"),
                        (10, "Темный грот"),
                    ],
                )
                connection.commit()
                connection.close()
                db = Database(str(path))
                try:
                    await db.connect()
                    self.assertEqual((await db.execute_query("PRAGMA user_version"))[0]["user_version"], SCHEMA_VERSION)
                    self.assertEqual((await db.get_resource_by_id(71))["code"], "dust" if matching else None)
                    self.assertEqual(
                        [row["id"] for row in await db.get_location_children(4)], [8, 9, 10] if matching else []
                    )
                    self.assertEqual(await db.execute_query("PRAGMA foreign_key_check"), [])
                    await db.close()
                    await db.connect()
                    self.assertEqual((await db.get_resource_by_id(71))["code"], "dust" if matching else None)
                finally:
                    await db.close()


class SqlBoundaryTests(unittest.TestCase):
    def test_sql_values_are_checked_instead_of_coerced(self):
        for value in (None, "1", 1.5, True):
            with self.assertRaises(ValueError):
                sql_int(value)
        for value in (None, 1, b"text"):
            with self.assertRaises(ValueError):
                sql_text(value)
        with self.assertRaises(ValueError):
            sql_row({"id": {"nested": "unexpected"}})
        self.assertEqual(sql_row({"id": 1, "note": None}), {"id": 1, "note": None})
