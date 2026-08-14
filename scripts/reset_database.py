"""Back up an existing SQLite database and replace it with the current clean schema."""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from database import Database  # noqa: E402


EXPECTED_TABLES = {
    "locations",
    "mobs",
    "resources",
    "gear",
    "cards",
    "drops",
    "recipes",
    "recipe_ingredients",
    "recipe_owners",
    "users",
    "analytics_events",
}


def remove_sqlite_sidecars(path: Path):
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def remove_sqlite_files(path: Path):
    path.unlink(missing_ok=True)
    remove_sqlite_sidecars(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path) -> dict:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        row_counts = {}
        for table in sorted(tables & EXPECTED_TABLES):
            row_counts[table] = connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
        return {"integrity": integrity, "tables": sorted(tables), "row_counts": row_counts}
    finally:
        connection.close()


def create_backup(source: Path, backup_dir: Path) -> tuple[Path, dict]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f%z")
    backup_path = backup_dir / f"{source.stem}-{timestamp}.db"

    source_connection = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    backup_connection = sqlite3.connect(backup_path)
    try:
        source_connection.backup(backup_connection)
    finally:
        backup_connection.close()
        source_connection.close()

    try:
        snapshot = inspect_database(backup_path)
    finally:
        remove_sqlite_sidecars(backup_path)
    if snapshot["integrity"] != "ok":
        remove_sqlite_files(backup_path)
        raise RuntimeError(f"Backup integrity check failed: {snapshot['integrity']}")

    metadata = {
        "created_at": datetime.now().astimezone().isoformat(),
        "source": str(source),
        "backup": str(backup_path),
        "sha256": sha256(backup_path),
        **snapshot,
    }
    backup_path.with_suffix(".json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return backup_path, metadata


async def create_clean_database(path: Path):
    database = Database(str(path))
    try:
        await database.connect()
    finally:
        await database.close()


def ensure_database_is_idle(path: Path):
    try:
        connection = sqlite3.connect(path, timeout=1)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise RuntimeError(
                    "Database WAL is busy. Stop all processes using the database before reset."
                )
        finally:
            connection.close()
    except sqlite3.OperationalError as error:
        raise RuntimeError(
            "Database is busy. Stop the bot and all processes using the database before reset."
        ) from error


def prune_backups(backup_dir: Path, source_stem: str, keep: int):
    backups = sorted(
        backup_dir.glob(f"{source_stem}-*.db"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[keep:]:
        stale.unlink()
        stale.with_suffix(".json").unlink(missing_ok=True)


def reset_database(source: Path, backup_dir: Path, keep: int = 10) -> tuple[Path, dict]:
    source = source.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Database file not found: {source}")
    if keep < 1:
        raise ValueError("At least one backup must be retained")

    ensure_database_is_idle(source)
    backup_path, metadata = create_backup(source, backup_dir)

    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{source.name}.", suffix=".new", dir=source.parent
    )
    os.close(handle)
    temporary_path = Path(temporary_name)
    try:
        asyncio.run(create_clean_database(temporary_path))
        clean_snapshot = inspect_database(temporary_path)
        if clean_snapshot["integrity"] != "ok":
            raise RuntimeError(f"New database integrity check failed: {clean_snapshot['integrity']}")
        missing = EXPECTED_TABLES - set(clean_snapshot["tables"])
        if missing:
            raise RuntimeError(f"New database is missing tables: {', '.join(sorted(missing))}")
        if any(clean_snapshot["row_counts"].values()):
            raise RuntimeError("New database unexpectedly contains application data")

        remove_sqlite_sidecars(source)
        os.replace(temporary_path, source)
    finally:
        remove_sqlite_files(temporary_path)

    prune_backups(backup_dir, source.stem, keep)
    return backup_path, metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up a SQLite database and atomically reset it to the current empty schema."
    )
    parser.add_argument("database", type=Path, help="Path to the existing SQLite database")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=PROJECT_ROOT / "backups",
        help="Backup directory (default: project backups/)",
    )
    parser.add_argument("--keep", type=int, default=10, help="Number of backups to retain")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm destructive replacement of the working database",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.yes:
        print("Refusing to reset without --yes.", file=sys.stderr)
        return 2
    try:
        backup_path, metadata = reset_database(args.database, args.backup_dir, args.keep)
    except Exception as error:
        print(f"Reset failed: {error}", file=sys.stderr)
        return 1

    print(f"Backup: {backup_path}")
    print(f"SHA-256: {metadata['sha256']}")
    print(f"Database reset: {args.database.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
