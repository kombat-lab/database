import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import CURRENT_SCHEMA_VERSION, Database
from scripts.migrate_database import TABLE_ORDER, auto_migrate_database, migrate_database


class DatabaseMigrationTests(unittest.TestCase):
    def test_current_schema_still_normalizes_legacy_resource_type(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "game.db"

            async def create_database():
                database = Database(str(database_path))
                await database.connect()
                try:
                    await database.add_resource("Legacy scroll", "📜", "scroll")
                finally:
                    await database.close()

            asyncio.run(create_database())
            report = auto_migrate_database(database_path, root / "backups", keep=3)

            self.assertIsNotNone(report)
            self.assertEqual(
                report["previous_schema_version"], CURRENT_SCHEMA_VERSION
            )
            self.assertEqual(report["detected_legacy_resource_types"], 1)
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT type FROM resources").fetchone()[0],
                    "scroll_recipe",
                )
            finally:
                connection.close()

    def test_automatic_migration_runs_once_for_unversioned_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database_path = root / "game.db"
            backup_dir = root / "backups"

            async def create_legacy_database():
                database = Database(str(database_path))
                await database.connect()
                try:
                    await database.add_resource("Legacy scroll", "📜", "scroll")
                    await database.execute_query("DROP TABLE schema_metadata")
                finally:
                    await database.close()

            asyncio.run(create_legacy_database())

            report = auto_migrate_database(database_path, backup_dir, keep=3)
            self.assertIsNotNone(report)
            self.assertEqual(report["previous_schema_version"], 0)
            self.assertEqual(report["schema_version"], CURRENT_SCHEMA_VERSION)
            self.assertEqual(len(list(backup_dir.glob("*.db"))), 1)

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
                    ).fetchone()[0],
                    str(CURRENT_SCHEMA_VERSION),
                )
                self.assertEqual(
                    connection.execute("SELECT type FROM resources").fetchone()[0],
                    "scroll_recipe",
                )
            finally:
                connection.close()

            self.assertIsNone(auto_migrate_database(database_path, backup_dir, keep=3))
            self.assertEqual(len(list(backup_dir.glob("*.db"))), 1)

    def test_migration_preserves_rows_and_normalizes_legacy_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "legacy.db"
            target = root / "game.db"

            async def create_database(path: Path):
                database = Database(str(path))
                await database.connect()
                await database.close()

            asyncio.run(create_database(source))
            asyncio.run(create_database(target))

            connection = sqlite3.connect(source)
            try:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute(
                    "INSERT INTO locations (id, name, emoji) VALUES (1, 'Forest', '🌲')"
                )
                connection.execute(
                    """
                    INSERT INTO mobs
                        (id, name, emoji, hp, dust_min, dust_max, exp, location_id)
                    VALUES (1, 'Wolf', '🐺', 10, 1, 2, 3, 1)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO resources (id, name, emoji, type, note)
                    VALUES (1, 'Legacy scroll', '📜', 'scroll', 'keep me')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO gear
                        (id, name, rarity, slot, emoji, level, classes, note)
                    VALUES (1, 'Sword', 'common', 'hand', '⚔️', 1, '', '')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO cards
                        (id, name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
                    VALUES (1, 'Wolf card', '🃏', 'weapon', '', '', '', '', '')
                    """
                )
                connection.execute(
                    "INSERT INTO drops (mob_id, item_type, item_id) VALUES (1, 'resource', 1)"
                )
                connection.execute(
                    """
                    INSERT INTO recipes (id, result_type, result_id, quantity)
                    VALUES (1, 'gear', 1, 1)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO recipe_ingredients (recipe_id, resource_id, quantity)
                    VALUES (1, 1, 2)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO recipe_owners (recipe_id, player_username)
                    VALUES (1, 'tester')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO users
                        (user_id, first_seen, last_activity, username, first_name, last_name)
                    VALUES (1, '2026-01-01', '2026-01-02', 'tester', 'Test', 'User')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO analytics_events
                        (id, user_id, event_type, target_id, target_type, metadata, timestamp)
                    VALUES (1, 1, 'view', 1, 'mob', '{}', '2026-01-02')
                    """
                )
                connection.commit()
            finally:
                connection.close()

            report = migrate_database(source, target)

            self.assertEqual(sum(report["row_counts"].values()), len(TABLE_ORDER))
            self.assertEqual(
                report["normalized_resource_types"]["scroll_to_scroll_recipe"], 1
            )
            self.assertEqual(list(root.glob(".game.db.*.migrated*")), [])

            migrated = sqlite3.connect(target)
            try:
                self.assertEqual(migrated.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(migrated.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    migrated.execute(
                        "SELECT name, type, note FROM resources WHERE id = 1"
                    ).fetchone(),
                    ("Legacy scroll", "scroll_recipe", "keep me"),
                )
                for table in TABLE_ORDER:
                    self.assertEqual(
                        migrated.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 1
                    )
            finally:
                migrated.close()

            original = sqlite3.connect(source)
            try:
                self.assertEqual(
                    original.execute("SELECT type FROM resources WHERE id = 1").fetchone()[0],
                    "scroll",
                )
            finally:
                original.close()
