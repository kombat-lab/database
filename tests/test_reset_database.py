import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from database import Database
from scripts.reset_database import EXPECTED_TABLES, reset_database


class DatabaseResetTests(unittest.TestCase):
    def test_reset_creates_verified_backup_and_empty_current_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "game.db"
            backup_dir = root / "backups"

            async def create_source():
                database = Database(str(source))
                await database.connect()
                try:
                    await database.add_resource("old resource", "📦")
                finally:
                    await database.close()

            asyncio.run(create_source())
            backup_path, metadata = reset_database(source, backup_dir, keep=3)

            self.assertEqual(list(root.glob(".game.db.*.new*")), [])
            self.assertTrue(backup_path.is_file())
            self.assertFalse(Path(f"{backup_path}-wal").exists())
            self.assertFalse(Path(f"{backup_path}-shm").exists())
            self.assertEqual(metadata["integrity"], "ok")
            self.assertEqual(metadata["row_counts"]["resources"], 1)
            manifest = json.loads(backup_path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["sha256"], metadata["sha256"])

            backup_connection = sqlite3.connect(backup_path)
            try:
                self.assertEqual(
                    backup_connection.execute("SELECT name FROM resources").fetchall(),
                    [("old resource",)],
                )
            finally:
                backup_connection.close()

            clean_connection = sqlite3.connect(source)
            try:
                tables = {
                    row[0]
                    for row in clean_connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                }
                self.assertEqual(tables, EXPECTED_TABLES)
                self.assertEqual(
                    clean_connection.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
                    0,
                )
            finally:
                clean_connection.close()
