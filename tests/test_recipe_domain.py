import asyncio
import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from database import Database, SCHEMA_VERSION
from recipe_domain import DomainError, DraftConflictError, validate_draft_payload


class RecipeDomainValidationTests(unittest.TestCase):
    def test_partial_draft_and_complete_validation(self):
        self.assertEqual(validate_draft_payload({}), {})
        with self.assertRaises(DomainError):
            validate_draft_payload({}, complete=True)
        for value in (True, 0, -1, 1.5, "2", 2**63):
            with self.subTest(value=value), self.assertRaises(DomainError):
                validate_draft_payload({"materials": [{"resource_id": 1, "quantity": value}]})
        for payload in (
            {"name": "x" * 129},
            {"note": "x" * 2001},
            {"emoji": "<b>"},
            {"classes": "unknown"},
            {"slot": "unknown"},
            {"rarity": "unknown"},
            {"materials": [{"resource_id": 1, "name": "mixed", "quantity": 1}]},
            {"materials": [{"name": " Ore ", "quantity": 1}, {"name": "ore", "quantity": 1}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(DomainError):
                validate_draft_payload(payload)


class RecipeDomainDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fog-domain-tests-")
        self.path = Path(self.temp.name) / "catalog.db"
        self.db = Database(str(self.path))
        await self.db.connect()
        self.material_id = await self.db.add_resource("Железо", "⚙️")
        self.location_id = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Пещера','🪨')")
        self.mob_id = await self.db.execute_insert(
            "INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES ('Страж','🐺',10,1,2,3,?)",
            (self.location_id,),
        )
        self.context = dict(owner_user_id=101, chat_id=-1000001, message_id=55)
        self.payload = dict(
            name="Меч",
            rarity="rare",
            slot="основная рука",
            emoji="🗡️",
            level=10,
            craftable=True,
            quantity=2,
            materials=[{"resource_id": self.material_id, "quantity": 3}],
            learning_scroll={},
            gear_mob_ids=[self.mob_id],
            scroll_mob_ids=[self.mob_id],
        )

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def draft(self, **changes):
        value = copy.deepcopy(self.payload)
        value.update(changes)
        return await self.db.create_gear_draft(payload=value, **self.context)

    async def save(self, draft, db=None):
        return await (db or self.db).save_gear_draft(
            draft["draft_id"], expected_revision=draft["revision"], **self.context
        )

    async def counts(self):
        return {
            table: (await self.db.execute_query(f"SELECT COUNT(*) AS n FROM {table}"))[0]["n"]
            for table in ("gear", "resources", "recipes", "recipe_ingredients", "recipe_learning_requirements", "drops")
        }

    async def test_draft_is_private_durable_and_save_creates_one_complete_aggregate(self):
        before = await self.counts()
        draft = await self.draft()
        self.assertEqual(await self.counts(), before)
        self.assertEqual(len(draft["draft_id"]), 16)
        await self.db.close()
        await self.db.connect()
        loaded = await self.db.get_gear_draft(draft["draft_id"], owner_user_id=101, chat_id=-1000001)
        self.assertEqual(loaded, draft)
        self.assertEqual(len(await self.db.list_gear_drafts(owner_user_id=101, chat_id=-1000001)), 1)
        self.assertIsNone(await self.db.get_gear_draft(draft["draft_id"], owner_user_id=102, chat_id=-1000001))
        result = await self.save(draft)
        card = await self.db.get_gear_card(result["gear_id"])
        self.assertEqual([item["id"] for item in card["ingredients"]], [self.material_id])
        self.assertEqual(card["learning_scroll"]["id"], result["scroll_resource_id"])
        self.assertTrue(card["can_learn"])
        self.assertEqual([item["id"] for item in card["scroll_mobs"]], [self.mob_id])
        self.assertEqual((await self.db.get_recipe_details(result["recipe_id"]))["quantity"], 2)
        scroll = await self.db.get_resource_card(result["scroll_resource_id"])
        self.assertEqual(scroll["used_in"], [])
        self.assertEqual(scroll["learning_recipes"][0]["result_id"], result["gear_id"])
        self.assertEqual(await self.db.list_gear_drafts(owner_user_id=101, chat_id=-1000001), [])

    async def test_identical_save_retries_across_connections_return_original_ids(self):
        draft = await self.draft(materials=[{"name": "Новый материал", "emoji": "🪨", "quantity": 4}])
        other = Database(str(self.path))
        await other.connect()
        try:
            results = await asyncio.gather(self.save(draft), self.save(draft, other), self.save(draft))
            self.assertEqual(results[0], results[1])
            self.assertEqual(results[0], results[2])
            self.assertEqual((await self.counts())["gear"], 1)
            self.assertEqual((await self.counts())["resources"], 3)
        finally:
            await other.close()

    async def test_context_revision_and_rebinding_invalidate_old_buttons(self):
        draft = await self.draft()
        for wrong_context in (
            {**self.context, "owner_user_id": 102},
            {**self.context, "chat_id": 9},
            {**self.context, "message_id": 56},
        ):
            with self.assertRaises(DraftConflictError):
                await self.db.save_gear_draft(draft["draft_id"], expected_revision=0, **wrong_context)
        changed = await self.db.update_gear_draft(
            draft["draft_id"], expected_revision=0, payload={**draft["payload"], "name": "Клинок"}, **self.context
        )
        with self.assertRaises(DraftConflictError):
            await self.save(draft)
        rebound = await self.db.bind_gear_draft_message(
            draft["draft_id"],
            old_message_id=55,
            new_message_id=99,
            expected_revision=changed["revision"],
            owner_user_id=101,
            chat_id=-1000001,
        )
        with self.assertRaises(DraftConflictError):
            await self.save(rebound)
        self.context["message_id"] = 99
        saved = await self.save(rebound)
        self.assertEqual((await self.db.get_gear_by_id(saved["gear_id"]))["name"], "Клинок")

    async def test_cancelled_draft_cannot_publish(self):
        draft = await self.draft()
        await self.db.cancel_gear_draft(draft["draft_id"], expected_revision=0, **self.context)
        with self.assertRaises(DraftConflictError):
            await self.save(draft)
        self.assertEqual((await self.counts())["gear"], 0)

    async def test_failed_final_validation_rolls_back_all_new_objects(self):
        await self.db.add_resource("Занятый свиток", "📜", "scroll_recipe")
        draft = await self.draft(
            materials=[{"name": "Создан внутри транзакции", "quantity": 1}], learning_scroll={"name": "Занятый свиток"}
        )
        before = await self.counts()
        with self.assertRaises(DomainError):
            await self.save(draft)
        self.assertEqual(await self.counts(), before)
        self.assertEqual(
            (await self.db.get_gear_draft(draft["draft_id"], owner_user_id=101, chat_id=-1000001))["status"], "editing"
        )

    async def test_cancellation_after_catalog_writes_rolls_back_then_retry_succeeds(self):
        draft = await self.draft(materials=[{"name": "Новый материал", "quantity": 2}])
        reached = asyncio.Event()
        before = await self.counts()
        original = self.db._replace_draft_drops

        async def delayed(item_type, item_id, mob_ids):
            reached.set()
            await asyncio.Event().wait()

        with patch.object(self.db, "_replace_draft_drops", delayed):
            task = asyncio.create_task(self.save(draft))
            await asyncio.wait_for(reached.wait(), 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await self.counts(), before)
        self.assertEqual(self.db._replace_draft_drops, original)
        result = await self.save(draft)
        self.assertIsNotNone(await self.db.get_gear_card(result["gear_id"]))

    async def test_two_editors_cannot_overwrite_each_other_and_owners_survive(self):
        initial = await self.save(await self.draft())
        first = await self.db.create_gear_draft(gear_id=initial["gear_id"], **self.context)
        second = await self.db.create_gear_draft(gear_id=initial["gear_id"], **self.context)
        await self.db.add_recipe_owner(initial["recipe_id"], "ManualOwner")
        first = await self.db.update_gear_draft(
            first["draft_id"],
            expected_revision=0,
            payload={**first["payload"], "note": "Первый редактор"},
            **self.context,
        )
        await self.save(first)
        with self.assertRaises(DraftConflictError):
            await self.save(second)
        self.assertEqual((await self.db.get_gear_by_id(initial["gear_id"]))["note"], "Первый редактор")
        self.assertEqual(await self.db.get_recipe_owners(initial["recipe_id"]), ["ManualOwner"])

    async def test_existing_recipe_cannot_be_removed_implicitly(self):
        initial = await self.save(await self.draft())
        draft = await self.db.create_gear_draft(gear_id=initial["gear_id"], **self.context)
        draft = await self.db.update_gear_draft(
            draft["draft_id"],
            expected_revision=0,
            payload={
                **draft["payload"],
                "craftable": False,
                "materials": [],
                "learning_scroll": None,
                "scroll_mob_ids": [],
            },
            **self.context,
        )
        with self.assertRaises(DomainError):
            await self.save(draft)
        self.assertIsNotNone(await self.db.get_recipe_details(initial["recipe_id"]))

    async def test_learning_is_not_rarity_and_owner_history_blocks_reassignment(self):
        result = await self.save(await self.draft(rarity="common"))
        await self.db.claim_recipe_owner(result["recipe_id"], 202, None, expected_gear_id=result["gear_id"])
        self.assertEqual((await self.db.get_gear_card(result["gear_id"]))["owner_user_ids"], [202])
        with self.assertRaises(DomainError):
            await self.db.set_recipe_learning_scroll(result["recipe_id"], None)
        second = await self.db.add_gear("Другой", "legendary", "шлем", "🪖")
        second_recipe = await self.db.create_recipe("gear", second)
        with self.assertRaises(DomainError):
            await self.db.set_recipe_learning_scroll(second_recipe, result["scroll_resource_id"])
        with self.assertRaises(DomainError):
            await self.db.add_ingredient(second_recipe, result["scroll_resource_id"], 1)
        with self.assertRaises(DomainError):
            await self.db.create_recipe("resource", result["scroll_resource_id"])
        with self.assertRaises(DomainError):
            await self.db.update_resource(result["scroll_resource_id"], resource_type="craft")

    async def test_last_material_cannot_be_removed_even_by_concurrent_requests(self):
        result = await self.db.add_resource("Зелье для удаления", "🧪", "alchemy")
        second = await self.db.add_resource("Второй материал", "")
        recipe = await self.db.save_resource_recipe(
            result,
            1,
            [
                {"resource_id": self.material_id, "quantity": 1},
                {"resource_id": second, "quantity": 1},
            ],
        )
        results = await asyncio.gather(
            self.db.remove_ingredient(recipe, self.material_id),
            self.db.remove_ingredient(recipe, second),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(item, DomainError) for item in results), 1)
        self.assertEqual(len((await self.db.get_recipe_details(recipe))["ingredients"]), 1)

    async def test_self_and_indirect_cycles_are_rejected_without_changing_recipe(self):
        a = await self.db.add_resource("A", "")
        b = await self.db.add_resource("B", "")
        c = await self.db.add_resource("C", "")
        ra = await self.db.create_recipe("resource", a)
        rb = await self.db.create_recipe("resource", b)
        rc = await self.db.create_recipe("resource", c)
        with self.assertRaises(DomainError):
            await self.db.add_ingredient(ra, a, 1)
        await self.db.add_ingredient(ra, b, 1)
        await self.db.add_ingredient(rb, c, 1)
        with self.assertRaises(DomainError):
            await self.db.add_ingredient(rc, a, 1)
        self.assertEqual((await self.db.get_recipe_details(rc))["ingredients"], [])
        for quantity in (0, -3, True, 2**63):
            with self.assertRaises(DomainError):
                await self.db.update_ingredient(ra, b, quantity)
        self.assertEqual((await self.db.get_recipe_details(ra))["ingredients"][0]["quantity"], 1)

    async def test_delete_requires_explicit_dependencies_and_bundle_preserves_or_removes_scroll(self):
        result = await self.save(await self.draft())
        before = await self.counts()
        for resource_id in (self.material_id, result["scroll_resource_id"]):
            with self.assertRaises(DomainError):
                await self.db.delete_resource(resource_id)
        self.assertEqual(await self.counts(), before)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute_query("DELETE FROM resources WHERE id=?", (self.material_id,))
        await self.db.delete_recipe_bundle(result["recipe_id"])
        self.assertIsNotNone(await self.db.get_resource_by_id(result["scroll_resource_id"]))
        self.assertTrue(await self.db.get_drop_status(self.mob_id, "resource", result["scroll_resource_id"]))
        recipe_id = await self.db.create_recipe("gear", result["gear_id"])
        await self.db.add_ingredient(recipe_id, self.material_id, 1)
        await self.db.set_recipe_learning_scroll(recipe_id, result["scroll_resource_id"])
        await self.db.delete_recipe_bundle(recipe_id, delete_scroll=True)
        self.assertIsNone(await self.db.get_resource_by_id(result["scroll_resource_id"]))
        self.assertIsNotNone(await self.db.get_resource_by_id(self.material_id))

    async def test_atomic_resource_recipe_and_retry(self):
        result_id = await self.db.add_resource("Зелье", "🧪", "alchemy")
        ingredients = [{"resource_id": self.material_id, "quantity": 2}]
        recipe_id = await self.db.save_resource_recipe(result_id, 3, ingredients)
        self.assertEqual(await self.db.save_resource_recipe(result_id, 3, ingredients), recipe_id)
        with self.assertRaises(DraftConflictError):
            await self.db.save_resource_recipe(result_id, 4, ingredients)
        with self.assertRaises(DomainError):
            await self.db.save_resource_recipe(result_id + 99, 1, [{"name": "rollback", "quantity": 1}])
        self.assertFalse(await self.db.execute_query("SELECT id FROM resources WHERE name='rollback'"))
        await self.db.update_recipe_quantity(recipe_id, 5)
        self.assertEqual((await self.db.get_recipe_details(recipe_id))["quantity"], 5)

    async def test_merge_preserves_formula_drops_and_old_public_id(self):
        source = await self.db.add_gear("Дубль", "rare", "тело", "🦺", 10)
        target = await self.db.add_gear("Дубль", "rare", "тело", "🛡️", 10, allow_duplicate=True)
        recipe_id = await self.db.create_recipe("gear", target)
        await self.db.add_ingredient(recipe_id, self.material_id, 1)
        await self.db.add_drop(self.mob_id, "gear", source)
        self.assertEqual(await self.db.merge_gear(source, target), target)
        self.assertEqual((await self.db.get_gear_card(source))["id"], target)
        self.assertEqual((await self.db.get_gear_by_id(source))["id"], target)
        self.assertTrue(await self.db.get_drop_status(self.mob_id, "gear", target))
        self.assertEqual((await self.db.get_recipe_details(recipe_id))["result_id"], target)
        self.assertEqual(await self.db.merge_gear(source, target), target)
        other = await self.db.add_gear("Дубль", "rare", "тело", "🦺", 1)
        with self.assertRaises(DomainError):
            await self.db.merge_gear(other, target)

    async def test_existing_scroll_sources_are_loaded_and_concurrent_changes_refuse_save(self):
        scroll = await self.db.add_resource("Найденный свиток", "📜", "scroll_recipe")
        await self.db.add_drop(self.mob_id, "resource", scroll)
        draft = await self.draft(learning_scroll={"resource_id": scroll}, scroll_mob_ids=[])
        self.assertEqual(draft["payload"]["scroll_mob_ids"], [self.mob_id])
        await self.db.remove_drop(self.mob_id, "resource", scroll)
        before = await self.counts()
        with self.assertRaises(DraftConflictError):
            await self.save(draft)
        self.assertEqual(await self.counts(), before)
        self.assertFalse(await self.db.get_drop_status(self.mob_id, "resource", scroll))

    async def test_switching_selected_scroll_copies_sources_then_allows_explicit_edit(self):
        scroll = await self.db.add_resource("Выбранный свиток", "📜", "scroll_recipe")
        await self.db.add_drop(self.mob_id, "resource", scroll)
        draft = await self.draft()
        draft = await self.db.update_gear_draft(
            draft["draft_id"],
            expected_revision=draft["revision"],
            payload={**draft["payload"], "learning_scroll": {"resource_id": scroll}, "scroll_mob_ids": []},
            **self.context,
        )
        self.assertEqual(draft["payload"]["scroll_mob_ids"], [self.mob_id])
        draft = await self.db.update_gear_draft(
            draft["draft_id"],
            expected_revision=draft["revision"],
            payload={**draft["payload"], "scroll_mob_ids": []},
            **self.context,
        )
        await self.save(draft)
        self.assertFalse(await self.db.get_drop_status(self.mob_id, "resource", scroll))

    async def test_maximum_gear_name_still_allows_automatic_scroll_name(self):
        result = await self.save(await self.draft(name="Я" * 128))
        scroll = await self.db.get_resource_by_id(result["scroll_resource_id"])
        self.assertEqual(scroll["name"], "Рецепт (" + "Я" * 128 + ")")

    async def test_contextual_delete_detects_new_owner_and_preserves_everything(self):
        result = await self.save(await self.draft())
        draft = await self.db.create_gear_draft(gear_id=result["gear_id"], **self.context)
        await self.db.claim_recipe_owner(result["recipe_id"], 202, "NewOwner")
        with self.assertRaises(DraftConflictError):
            await self.db.delete_gear_draft_target(
                draft["draft_id"], expected_revision=0, delete_gear=True, delete_scroll=True, **self.context
            )
        self.assertIsNotNone(await self.db.get_gear_card(result["gear_id"]))
        refreshed = await self.db.create_gear_draft(gear_id=result["gear_id"], **self.context)
        deleted = await self.db.delete_gear_draft_target(
            refreshed["draft_id"], expected_revision=0, delete_gear=True, delete_scroll=True, **self.context
        )
        self.assertEqual(deleted["status"], "cancelled")
        self.assertIsNone(await self.db.get_gear_card(result["gear_id"]))
        self.assertIsNone(await self.db.get_resource_by_id(result["scroll_resource_id"]))
        self.assertEqual(
            await self.db.delete_gear_draft_target(
                refreshed["draft_id"], expected_revision=0, delete_gear=True, delete_scroll=True, **self.context
            ),
            deleted,
        )

    async def test_craft_location_is_explicit_and_survives_result_rename(self):
        result = await self.db.add_resource("Новое зелье", "🧪", "alchemy")
        recipe = await self.db.save_resource_recipe(result, 1, [{"resource_id": self.material_id, "quantity": 2}])
        self.assertEqual((await self.db.get_resource_card(result))["craft_location"], "")
        await self.db.update_recipe_craft_location(recipe, "Мастерская <алхимика>")
        await self.db.update_resource(result, name="Переименованное зелье")
        self.assertEqual((await self.db.get_resource_card(result))["craft_location"], "Мастерская <алхимика>")
        self.assertEqual((await self.db.get_recipe_for_resource(result))["craft_location"], "Мастерская <алхимика>")
        with self.assertRaises(DomainError):
            await self.db.update_recipe_craft_location(recipe, "x" * 501)


class LearningMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fog-learning-migration-")
        self.path = Path(self.temp.name) / "legacy.db"

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def fixture(self, *, ambiguous=False, quantity=1, version=1):
        db = Database(str(self.path))
        await db.connect()
        material = await db.add_resource("Ore", "")
        scroll = await db.add_resource("Scroll", "📜", "scroll_recipe")
        gear = await db.add_gear("Gear", "epic", "шлем", "")
        recipe = await db.create_recipe("gear", gear)
        await db.add_ingredient(recipe, material, 3)
        # Deliberately construct a pre-v2 legacy owner before its scroll migration.
        await db.execute_query(
            "INSERT INTO recipe_owners(recipe_id,player_username) VALUES (?,?)", (recipe, "ManualOwner")
        )
        await db.close()
        with closing(sqlite3.connect(self.path)) as conn, conn:
            for (trigger,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'audit_%'"
            ).fetchall():
                conn.execute(f"DROP TRIGGER {trigger}")
            conn.execute("DROP TABLE recipe_learning_requirements")
            conn.execute("DROP TABLE gear_drafts")
            conn.execute("DROP TABLE gear_aliases")
            conn.execute("INSERT INTO recipe_ingredients VALUES (?,?,?)", (recipe, scroll, quantity))
            if ambiguous:
                conn.execute("INSERT INTO gear(name,rarity,slot,emoji) VALUES ('Other','epic','шлем','')")
                other = conn.execute("SELECT MAX(id) FROM gear").fetchone()[0]
                conn.execute("INSERT INTO recipes(result_type,result_id) VALUES ('gear',?)", (other,))
                other_recipe = conn.execute("SELECT MAX(id) FROM recipes").fetchone()[0]
                conn.execute("INSERT INTO recipe_ingredients VALUES (?,?,1)", (other_recipe, scroll))
            if version == 0:
                conn.execute("ALTER TABLE recipe_owners RENAME TO owners_v1")
                conn.execute(
                    "CREATE TABLE recipe_owners(recipe_id INTEGER NOT NULL,player_username TEXT NOT NULL,PRIMARY KEY(recipe_id,player_username),FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE)"
                )
                conn.execute("INSERT INTO recipe_owners SELECT recipe_id,player_username FROM owners_v1")
                conn.execute("DROP TABLE owners_v1")
            conn.execute(f"PRAGMA user_version={version}")
        return material, scroll, gear, recipe

    async def test_migration_preserves_ids_materials_and_manual_owners(self):
        material, scroll, gear, recipe = await self.fixture(version=0)
        db = Database(str(self.path))
        try:
            await db.connect()
            card = await db.get_gear_card(gear)
            self.assertEqual(card["learning_scroll"]["id"], scroll)
            self.assertEqual([item["id"] for item in card["ingredients"]], [material])
            self.assertEqual(card["owners"], ["ManualOwner"])
            self.assertEqual(card["owner_user_ids"], [])
            self.assertEqual((await db.execute_query("PRAGMA user_version"))[0]["user_version"], SCHEMA_VERSION)
            self.assertEqual(await db.execute_query("PRAGMA foreign_key_check"), [])
            self.assertEqual((await db.execute_query("PRAGMA integrity_check"))[0]["integrity_check"], "ok")
            await db.close()
            await db.connect()
            self.assertEqual(len(await db.execute_query("SELECT * FROM recipe_learning_requirements")), 1)
        finally:
            await db.close()

    async def test_ambiguous_or_nonunit_legacy_scroll_rolls_back_schema_and_data(self):
        for ambiguous, quantity in ((True, 1), (False, 2)):
            with self.subTest(ambiguous=ambiguous, quantity=quantity):
                if self.path.exists():
                    self.path.unlink()
                await self.fixture(ambiguous=ambiguous, quantity=quantity)
                with closing(sqlite3.connect(self.path)) as conn:
                    before = conn.execute("SELECT * FROM recipe_ingredients ORDER BY recipe_id,resource_id").fetchall()
                db = Database(str(self.path))
                try:
                    with self.assertRaises(DomainError):
                        await db.connect()
                finally:
                    await db.close()
                with closing(sqlite3.connect(self.path)) as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                    self.assertEqual(
                        conn.execute("SELECT * FROM recipe_ingredients ORDER BY recipe_id,resource_id").fetchall(),
                        before,
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT name FROM sqlite_master WHERE name='recipe_learning_requirements'"
                        ).fetchall(),
                        [],
                    )
