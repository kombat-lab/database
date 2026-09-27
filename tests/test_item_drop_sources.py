import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database
from recipe_domain import DomainError


class ItemDropSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fog-item-sources-")
        self.path = Path(self.temp.name) / "catalog.db"
        self.db = Database(str(self.path))
        await self.db.connect()
        self.forest = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Ясный лес','🌲')")
        self.cave = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Пещера','🪨')")
        self.arena = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Арена','🏟️')")
        self.wolf = await self.mob("Волк", self.forest)
        self.guard = await self.mob("Страж", self.cave)
        self.literal = await self.mob("Босс 100%_силы", self.arena)
        self.guard_copy = await self.mob("Страж", self.cave)
        self.resource = await self.db.add_resource("Железо", "⚙️")
        self.card = await self.db.add_card("Карта волка", "🃏", "шлем")
        self.gear = await self.db.add_gear("Меч", "rare", "основная рука", "🗡️")

    async def asyncTearDown(self):
        await self.db.close()
        self.temp.cleanup()

    async def mob(self, name, location):
        return await self.db.execute_insert(
            "INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES (?, ?, 10, 1, 2, 3, ?)",
            (name, "🐺", location),
        )

    async def counts(self):
        return {
            table: (await self.db.execute_query(f"SELECT COUNT(*) AS n FROM {table}"))[0]["n"]
            for table in ("resources", "gear", "cards", "drops")
        }

    async def fail_drop_writes(self):
        await self.db.execute_query(
            "CREATE TRIGGER fail_source BEFORE INSERT ON drops BEGIN SELECT RAISE(ABORT, 'source failure'); END"
        )

    async def test_search_matches_unicode_mob_or_location_and_returns_context(self):
        by_name = await self.db.get_drop_source_mobs("стРАЖ")
        self.assertEqual([row["id"] for row in by_name], [self.guard, self.guard_copy])
        self.assertEqual(
            by_name[0],
            {
                "id": self.guard,
                "name": "Страж",
                "emoji": "🐺",
                "location_id": self.cave,
                "location_name": "Пещера",
                "location_emoji": "🪨",
            },
        )
        by_location = await self.db.get_drop_source_mobs("яСНЫЙ")
        self.assertEqual([row["id"] for row in by_location], [self.wolf])
        self.assertEqual(await self.db.get_drop_source_mobs("Несуществующее"), [])

    async def test_search_metacharacters_are_literal(self):
        for query in ("%", "_", "%_"):
            with self.subTest(query=query):
                rows = await self.db.get_drop_source_mobs(query)
                self.assertEqual([row["id"] for row in rows], [self.literal])
        self.assertEqual(await self.db.get_drop_source_mobs("%' OR 1=1 --"), [])

    async def test_page_order_uses_location_name_mob_name_and_id(self):
        expected = [self.literal, self.guard, self.guard_copy, self.wolf]
        pages = [await self.db.get_drop_source_mobs(offset=offset, limit=2) for offset in (0, 2, 4)]
        self.assertEqual([row["id"] for page in pages for row in page], expected)
        self.assertEqual(pages[-1], [])

    async def test_selected_filter_intersects_search_and_pagination(self):
        rows = await self.db.get_drop_source_mobs(
            "пещ", limit=1, offset=1, mob_ids=[self.wolf, self.guard, self.guard_copy]
        )
        self.assertEqual([row["id"] for row in rows], [self.guard_copy])
        self.assertEqual(await self.db.get_drop_source_mobs(mob_ids=[]), [])
        self.assertEqual(await self.db.get_drop_source_mobs(mob_ids=[999999]), [])

    async def test_pagination_is_bounded_and_rejects_invalid_arguments(self):
        async with self.db.transaction():
            for index in range(105):
                await self.mob(f"Арена {index}", self.arena)
        self.assertEqual(len(await self.db.get_drop_source_mobs(limit=10000)), 100)
        for kwargs in (
            {"offset": -1},
            {"offset": True},
            {"offset": 2**63},
            {"offset": "0"},
            {"limit": 0},
            {"limit": -1},
            {"limit": True},
            {"limit": "9"},
            {"query": None},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(DomainError):
                await self.db.get_drop_source_mobs(**kwargs)

    async def test_all_item_types_support_sorted_replace_and_clear(self):
        for item_type, item_id in (("resource", self.resource), ("gear", self.gear), ("card", self.card)):
            with self.subTest(item_type=item_type):
                self.assertEqual(await self.db.get_item_drop_mob_ids(item_type, item_id), [])
                await self.db.set_item_drop_sources(item_type, item_id, [self.guard, self.wolf], expected_mob_ids=[])
                self.assertEqual(await self.db.get_item_drop_mob_ids(item_type, item_id), [self.wolf, self.guard])
                await self.db.set_item_drop_sources(
                    item_type, item_id, [self.literal], expected_mob_ids=[self.guard, self.wolf]
                )
                self.assertEqual(await self.db.get_item_drop_mob_ids(item_type, item_id), [self.literal])
                await self.db.set_item_drop_sources(item_type, item_id, [], expected_mob_ids=[self.literal])
                self.assertEqual(await self.db.get_item_drop_mob_ids(item_type, item_id), [])

    async def test_gear_alias_reads_and_writes_canonical_sources(self):
        alias = await self.db.add_gear("Меч", "rare", "основная рука", "⚔️", allow_duplicate=True)
        await self.db.merge_gear(alias, self.gear)
        await self.db.set_item_drop_sources("gear", alias, [self.wolf], expected_mob_ids=[])
        self.assertEqual(await self.db.get_item_drop_mob_ids("gear", alias), [self.wolf])
        self.assertEqual(await self.db.get_item_drop_mob_ids("gear", self.gear), [self.wolf])
        rows = await self.db.execute_query("SELECT item_id FROM drops WHERE item_type='gear'")
        self.assertEqual(rows, [{"item_id": self.gear}])

    async def test_item_ids_and_unknown_types_are_validated_without_creating_orphans(self):
        before = await self.counts()
        for item_type, item_id in (
            ("unknown", self.resource),
            ("resource", 999999),
            ("card", 999999),
            ("gear", 999999),
            ("gear", 0),
            ("card", True),
            ("resource", 2**63),
        ):
            with self.subTest(item_type=item_type, item_id=item_id):
                with self.assertRaises(DomainError):
                    await self.db.get_item_drop_mob_ids(item_type, item_id)
                with self.assertRaises(DomainError):
                    await self.db.set_item_drop_sources(item_type, item_id, [self.wolf])
        self.assertEqual(await self.counts(), before)

    async def test_source_ids_and_baselines_are_validated_before_writes(self):
        await self.db.set_item_drop_sources("resource", self.resource, [self.wolf])
        for values in ([self.wolf, self.wolf], [0], [-1], [True], ["1"], [2**63], [1.5], [1] * 1001, (self.wolf,)):
            with self.subTest(values=values):
                with self.assertRaises(DomainError):
                    await self.db.set_item_drop_sources("resource", self.resource, values)
                with self.assertRaises(DomainError):
                    await self.db.set_item_drop_sources("resource", self.resource, [], expected_mob_ids=values)
                with self.assertRaises(DomainError):
                    await self.db.get_drop_source_mobs(mob_ids=values)
        self.assertEqual(await self.db.get_item_drop_mob_ids("resource", self.resource), [self.wolf])

    async def test_missing_mob_rejects_replace_without_removing_existing_sources(self):
        await self.db.set_item_drop_sources("resource", self.resource, [self.wolf])
        with self.assertRaises(DomainError):
            await self.db.set_item_drop_sources(
                "resource", self.resource, [self.guard, 999999], expected_mob_ids=[self.wolf]
            )
        self.assertEqual(await self.db.get_item_drop_mob_ids("resource", self.resource), [self.wolf])

    async def test_write_failure_rolls_back_removed_sources(self):
        await self.db.set_item_drop_sources("resource", self.resource, [self.wolf])
        await self.fail_drop_writes()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "source failure"):
            await self.db.set_item_drop_sources("resource", self.resource, [self.guard], expected_mob_ids=[self.wolf])
        self.assertEqual(await self.db.get_item_drop_mob_ids("resource", self.resource), [self.wolf])

    async def test_create_wrappers_commit_complete_records_and_sources(self):
        resource = await self.db.create_resource_with_sources(
            "Зелье", "🧪", "consumable", "Лечит", mob_ids=[self.guard, self.wolf]
        )
        card = await self.db.create_card_with_sources(
            "Карта стража", "🃏", "шлем", "+1", "+2", "+3", "+4", "Описание", mob_ids=[self.guard]
        )
        self.assertEqual((await self.db.get_resource_by_id(resource))["note"], "Лечит")
        self.assertEqual((await self.db.get_resource_by_id(resource))["type"], "consumable")
        value = await self.db.get_card_by_id(card)
        self.assertEqual(
            [value[key] for key in ("bonus1", "bonus2", "bonus3", "bonus4", "note")],
            ["+1", "+2", "+3", "+4", "Описание"],
        )
        self.assertEqual(await self.db.get_item_drop_mob_ids("resource", resource), [self.wolf, self.guard])
        self.assertEqual(await self.db.get_item_drop_mob_ids("card", card), [self.guard])
        without_sources = await self.db.create_resource_with_sources("Без дропа", "", mob_ids=[])
        self.assertEqual(await self.db.get_item_drop_mob_ids("resource", without_sources), [])

    async def test_create_wrappers_roll_back_records_when_source_missing(self):
        before = await self.counts()
        with self.assertRaises(DomainError):
            await self.db.create_resource_with_sources("Не сохранится", "🪨", mob_ids=[self.wolf, 999999])
        self.assertEqual(await self.counts(), before)
        with self.assertRaises(DomainError):
            await self.db.create_card_with_sources("Не сохранится", "🃏", "шлем", mob_ids=[999999])
        self.assertEqual(await self.counts(), before)

    async def test_create_wrappers_roll_back_records_after_write_failure(self):
        before = await self.counts()
        await self.fail_drop_writes()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "source failure"):
            await self.db.create_resource_with_sources("Не сохранится", "🪨", mob_ids=[self.wolf])
        self.assertEqual(await self.counts(), before)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "source failure"):
            await self.db.create_card_with_sources("Не сохранится", "🃏", "шлем", mob_ids=[self.wolf])
        self.assertEqual(await self.counts(), before)

    async def test_stale_baseline_preserves_external_additions_and_removals(self):
        await self.db.set_item_drop_sources("resource", self.resource, [self.wolf])
        other = Database(str(self.path))
        await other.connect()
        try:
            await other.add_drop(self.guard, "resource", self.resource)
            with self.assertRaises(DomainError):
                await self.db.set_item_drop_sources("resource", self.resource, [], expected_mob_ids=[self.wolf])
            self.assertEqual(await self.db.get_item_drop_mob_ids("resource", self.resource), [self.wolf, self.guard])
            await other.remove_drop(self.guard, "resource", self.resource)
            with self.assertRaises(DomainError):
                await self.db.set_item_drop_sources(
                    "resource", self.resource, [self.literal], expected_mob_ids=[self.wolf, self.guard]
                )
            self.assertEqual(await self.db.get_item_drop_mob_ids("resource", self.resource), [self.wolf])
        finally:
            await other.close()

    async def test_simultaneous_editors_cannot_overwrite_each_others_sources(self):
        other = Database(str(self.path))
        await other.connect()
        try:
            results = await asyncio.gather(
                self.db.set_item_drop_sources("resource", self.resource, [self.wolf], expected_mob_ids=[]),
                other.set_item_drop_sources("resource", self.resource, [self.guard], expected_mob_ids=[]),
                return_exceptions=True,
            )
            self.assertEqual(sum(result is None for result in results), 1)
            self.assertEqual(sum(isinstance(result, DomainError) for result in results), 1)
            sources = await self.db.get_item_drop_mob_ids("resource", self.resource)
            self.assertIn(sources, ([self.wolf], [self.guard]))
        finally:
            await other.close()
