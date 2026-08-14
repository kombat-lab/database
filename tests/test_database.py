import asyncio
import tempfile
import unittest
from pathlib import Path

from database import Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.db = Database(str(self.db_path))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_fresh_database_contains_complete_schema(self):
        expected = {
            "locations", "mobs", "resources", "gear", "cards", "drops",
            "recipes", "recipe_ingredients", "recipe_owners", "users",
            "analytics_events",
        }
        rows = await self.db.execute_query(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
        actual = {row["name"] for row in rows}
        self.assertTrue(expected.issubset(actual))

    async def test_concurrent_inserts_return_their_own_ids(self):
        ids = await asyncio.gather(*(
            self.db.add_resource(f"resource-{index}", "📦")
            for index in range(25)
        ))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 25)

        rows = await self.db.execute_query("SELECT COUNT(*) AS count FROM resources")
        self.assertEqual(rows[0]["count"], 25)

    async def test_admin_resources_are_sorted_alphabetically(self):
        await self.db.add_resource("яблоко", "🍎")
        await self.db.add_resource("Арбуз", "🍉")
        await self.db.add_resource("банан", "🍌")

        rows = await self.db.get_resources_page(0, 10)

        self.assertEqual(
            [row["name"] for row in rows],
            ["Арбуз", "банан", "яблоко"],
        )

    async def test_drop_search_matches_all_item_types_and_reports_status(self):
        location_id = await self.db.execute_insert(
            "INSERT INTO locations (name, emoji) VALUES (?, ?)",
            ("Лес", "🌲"),
        )
        mob_id = await self.db.execute_insert(
            """
            INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("Волк", "🐺", 10, 1, 2, 3, location_id),
        )
        resource_id = await self.db.add_resource("Сияющий кристалл", "💎")
        await self.db.add_resource("Древесина", "🪵")
        gear_id = await self.db.add_gear(
            "Кристальный меч", "rare", "основная рука", "⚔️"
        )
        card_id = await self.db.add_card("Карта кристалла", "🃏", "основная рука")
        await self.db.add_drop(mob_id, "resource", resource_id)

        rows = await self.db.search_drop_items(mob_id, "КРИСТ", limit=20)

        self.assertEqual(
            [(row["item_type"], row["id"]) for row in rows],
            [("card", card_id), ("gear", gear_id), ("resource", resource_id)],
        )
        self.assertEqual(
            {row["item_type"]: bool(row["enabled"]) for row in rows},
            {"card": False, "gear": False, "resource": True},
        )

    async def test_nested_transaction_rollback_isolated_by_savepoint(self):
        async with self.db.transaction():
            await self.db.execute_query(
                "INSERT INTO locations (name, emoji) VALUES (?, ?)", ("first", "1")
            )
            with self.assertRaises(RuntimeError):
                async with self.db.transaction():
                    await self.db.execute_query(
                        "INSERT INTO locations (name, emoji) VALUES (?, ?)", ("rolled-back", "2")
                    )
                    raise RuntimeError("rollback nested transaction")
            await self.db.execute_query(
                "INSERT INTO locations (name, emoji) VALUES (?, ?)", ("last", "3")
            )

        rows = await self.db.execute_query("SELECT name FROM locations ORDER BY id")
        self.assertEqual([row["name"] for row in rows], ["first", "last"])

    async def test_window_navigation_returns_adjacent_items(self):
        first = await self.db.add_resource("alpha", "1", "scroll_recipe")
        middle = await self.db.add_resource("bravo", "2", "scroll_recipe")
        last = await self.db.add_resource("charlie", "3", "scroll_recipe")

        self.assertEqual(
            await self.db.get_prev_next_resource_by_type(middle, "scroll_recipe"),
            {"prev_id": first, "next_id": last},
        )
        self.assertEqual(
            await self.db.get_prev_next_resource_by_type(first, "scroll_recipe"),
            {"prev_id": None, "next_id": middle},
        )

    async def test_card_aggregates_preserve_commas_and_pipes(self):
        await self.db.execute_query(
            "INSERT INTO locations (name, emoji) VALUES (?, ?)", ("forest, cave | level", "📍")
        )
        await self.db.execute_query(
            """INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("mob, elite | boss", "🐾", 10, 1, 2, 3, 1),
        )
        resource_id = await self.db.add_resource("ore, shard | rare", "📦")
        gear_id = await self.db.add_gear("blade, two | handed", "rare", "slot", "⚔️")
        card_id = await self.db.add_card("card, special | foil", "🃏", "slot")
        await self.db.add_drop(1, "resource", resource_id)
        await self.db.add_drop(1, "gear", gear_id)
        await self.db.add_drop(1, "card", card_id)
        recipe_id = await self.db.create_recipe("gear", gear_id)
        await self.db.add_ingredient(recipe_id, resource_id, 2)
        await self.db.add_recipe_owner(recipe_id, "owner, with | separators")

        mob = await self.db.get_mob_full_card(1)
        self.assertEqual(mob["resource_drops"][0]["name"], "ore, shard | rare")
        self.assertEqual(mob["gear_drops"][0]["name"], "blade, two | handed")
        self.assertEqual(mob["card_drops"][0]["name"], "card, special | foil")

        resource = await self.db.get_resource_card(resource_id)
        self.assertEqual(resource["mobs"][0]["name"], "mob, elite | boss")
        self.assertEqual(resource["mobs"][0]["location_name"], "forest, cave | level")

        gear = await self.db.get_gear_card(gear_id)
        self.assertEqual(gear["ingredients"][0]["name"], "ore, shard | rare")
        self.assertEqual(gear["owners"], ["owner, with | separators"])

    async def test_resource_delete_cleans_related_rows_atomically(self):
        await self.db.execute_query(
            "INSERT INTO locations (name, emoji) VALUES (?, ?)", ("location", "📍")
        )
        await self.db.execute_query(
            """INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("mob", "🐾", 10, 1, 2, 3, 1),
        )
        result_id = await self.db.add_resource("result", "📦")
        ingredient_id = await self.db.add_resource("ingredient", "✨")
        recipe_id = await self.db.create_recipe("resource", result_id)
        await self.db.add_ingredient(recipe_id, ingredient_id, 2)
        await self.db.add_recipe_owner(recipe_id, "tester")
        await self.db.add_drop(1, "resource", result_id)

        await self.db.delete_resource(result_id)

        self.assertEqual(await self.db.execute_query(
            "SELECT 1 FROM drops WHERE item_type = 'resource' AND item_id = ?", (result_id,)
        ), [])
        self.assertEqual(await self.db.execute_query(
            "SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)
        ), [])
        self.assertEqual(await self.db.execute_query(
            "SELECT 1 FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
        ), [])
        self.assertEqual(await self.db.execute_query(
            "SELECT 1 FROM recipe_owners WHERE recipe_id = ?", (recipe_id,)
        ), [])
        self.assertIsNotNone(await self.db.get_resource_by_id(ingredient_id))
