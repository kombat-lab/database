"""Back up and migrate SQLite data into the current application schema."""

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

from database import CURRENT_SCHEMA_VERSION, Database  # noqa: E402


TABLE_ORDER = (
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
)
EXPECTED_TABLES = set(TABLE_ORDER)


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
        row_counts = {
            table: connection.execute(
                f"SELECT COUNT(*) FROM {quote_identifier(table)}"
            ).fetchone()[0]
            for table in sorted(tables & EXPECTED_TABLES)
        }
        return {"integrity": integrity, "tables": sorted(tables), "row_counts": row_counts}
    finally:
        connection.close()


def get_schema_version(path: Path) -> int | None:
    if not path.is_file() or path.stat().st_size == 0:
        return None
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not tables:
            return None
        if "schema_metadata" not in tables:
            return 0
        row = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            return 0
        try:
            return int(row[0])
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"Invalid database schema version: {row[0]!r}") from error
    finally:
        connection.close()


def count_legacy_resource_types(path: Path) -> int:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "resources" not in tables:
            return 0
        return connection.execute(
            "SELECT COUNT(*) FROM resources WHERE type = 'scroll'"
        ).fetchone()[0]
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
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
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
                    "Database WAL is busy. Stop all processes using it before migration."
                )
        finally:
            connection.close()
    except sqlite3.OperationalError as error:
        raise RuntimeError(
            "Database is busy. Stop all processes using it before migration."
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
        stale.with_name(f"{stale.stem}.migration.json").unlink(missing_ok=True)


def quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def get_columns(connection: sqlite3.Connection, schema: str, table: str) -> dict:
    return {
        row[1]: {"notnull": bool(row[3]), "default": row[4]}
        for row in connection.execute(
            f"PRAGMA {schema}.table_info({quote_identifier(table)})"
        )
    }


def get_source_expression(table: str, column: str, target: dict, exists: bool) -> str:
    if exists:
        expression = f"source.{quote_identifier(column)}"
        if target["notnull"] and target["default"] is not None:
            expression = f"COALESCE({expression}, {target['default']})"
    elif target["default"] is not None:
        expression = target["default"]
    elif not target["notnull"]:
        expression = "NULL"
    else:
        raise RuntimeError(f"Required source column is missing: {table}.{column}")

    if table == "resources" and column == "type":
        expression = (
            f"CASE WHEN {expression} = 'scroll' "
            f"THEN 'scroll_recipe' ELSE {expression} END"
        )
    return expression


def migrate_database(source: Path, target: Path) -> dict:
    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source database not found: {source}")
    if not target.is_file():
        raise FileNotFoundError(f"Target database not found: {target}")
    if source == target:
        raise ValueError("Source backup and target database must be different files")

    source_snapshot = inspect_database(source)
    if source_snapshot["integrity"] != "ok":
        raise RuntimeError(f"Source integrity check failed: {source_snapshot['integrity']}")
    missing_tables = EXPECTED_TABLES - set(source_snapshot["tables"])
    if missing_tables:
        raise RuntimeError(f"Source tables are missing: {', '.join(sorted(missing_tables))}")

    ensure_database_is_idle(target)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".migrated", dir=target.parent
    )
    os.close(handle)
    temporary_path = Path(temporary_name)

    try:
        asyncio.run(create_clean_database(temporary_path))
        connection = sqlite3.connect(
            f"file:{temporary_path.as_posix()}",
            uri=True,
        )
        normalized_scroll_rows = 0
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "ATTACH DATABASE ? AS legacy",
                (f"file:{source.as_posix()}?mode=ro",),
            )
            connection.execute("BEGIN IMMEDIATE")

            normalized_scroll_rows = connection.execute(
                "SELECT COUNT(*) FROM legacy.resources WHERE type = 'scroll'"
            ).fetchone()[0]

            for table in TABLE_ORDER:
                target_columns = get_columns(connection, "main", table)
                source_columns = get_columns(connection, "legacy", table)
                if not source_columns:
                    raise RuntimeError(f"Source table is missing: {table}")

                expressions = [
                    get_source_expression(table, column, info, column in source_columns)
                    for column, info in target_columns.items()
                ]
                columns_sql = ", ".join(quote_identifier(column) for column in target_columns)
                expressions_sql = ", ".join(expressions)
                connection.execute(
                    f"INSERT INTO main.{quote_identifier(table)} ({columns_sql}) "
                    f"SELECT {expressions_sql} "
                    f"FROM legacy.{quote_identifier(table)} AS source"
                )

                source_only = connection.execute(
                    f"SELECT {expressions_sql} "
                    f"FROM legacy.{quote_identifier(table)} AS source "
                    f"EXCEPT SELECT {columns_sql} FROM main.{quote_identifier(table)} LIMIT 1"
                ).fetchone()
                target_only = connection.execute(
                    f"SELECT {columns_sql} FROM main.{quote_identifier(table)} "
                    f"EXCEPT SELECT {expressions_sql} "
                    f"FROM legacy.{quote_identifier(table)} AS source LIMIT 1"
                ).fetchone()
                if source_only is not None or target_only is not None:
                    raise RuntimeError(f"Row verification failed: {table}")

            connection.commit()
            connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Foreign key violations: {violations[:5]}")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise RuntimeError(f"Migrated database integrity check failed: {integrity}")

            migrated_counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM main.{quote_identifier(table)}"
                ).fetchone()[0]
                for table in TABLE_ORDER
            }
            if migrated_counts != source_snapshot["row_counts"]:
                raise RuntimeError("Row counts changed during migration")
            if connection.execute(
                "SELECT COUNT(*) FROM main.resources WHERE type = 'scroll'"
            ).fetchone()[0]:
                raise RuntimeError("Legacy resource type remains after migration")
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            remove_sqlite_sidecars(source)

        ensure_database_is_idle(temporary_path)
        remove_sqlite_sidecars(target)
        os.replace(temporary_path, target)
    finally:
        remove_sqlite_files(temporary_path)

    return {
        "migrated_at": datetime.now().astimezone().isoformat(),
        "source": str(source),
        "source_sha256": sha256(source),
        "target": str(target),
        "integrity": "ok",
        "foreign_key_violations": 0,
        "row_counts": source_snapshot["row_counts"],
        "normalized_resource_types": {"scroll_to_scroll_recipe": normalized_scroll_rows},
        "schema_version": CURRENT_SCHEMA_VERSION,
    }


def migrate_working_database(
    database: Path,
    backup_dir: Path,
    keep: int = 10,
    source_backup: Path | None = None,
) -> tuple[Path, dict]:
    database = database.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    if keep < 1:
        raise ValueError("At least one backup must be retained")

    ensure_database_is_idle(database)
    if source_backup is None:
        source, _ = create_backup(database, backup_dir)
    else:
        source = source_backup.expanduser().resolve()

    report = migrate_database(source, database)
    prune_backups(backup_dir, database.stem, keep)
    return source, report


def auto_migrate_database(
    database: Path,
    backup_dir: Path | None = None,
    keep: int = 10,
) -> dict | None:
    database = database.expanduser().resolve()
    if not database.exists() or database.stat().st_size == 0:
        return None

    ensure_database_is_idle(database)
    installed_version = get_schema_version(database)
    if installed_version is None:
        return None
    if installed_version > CURRENT_SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {installed_version} is newer than "
            f"application version {CURRENT_SCHEMA_VERSION}"
        )
    legacy_resource_rows = count_legacy_resource_types(database)
    if installed_version == CURRENT_SCHEMA_VERSION and legacy_resource_rows == 0:
        return None

    resolved_backup_dir = (
        backup_dir.expanduser().resolve()
        if backup_dir is not None
        else database.parent / "backups"
    )
    source, report = migrate_working_database(database, resolved_backup_dir, keep)
    report["previous_schema_version"] = installed_version
    report["detected_legacy_resource_types"] = legacy_resource_rows
    report_path = source.with_name(f"{source.stem}.migration.json")
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up and migrate SQLite data into the current application schema."
    )
    parser.add_argument("database", type=Path, help="Working SQLite database")
    parser.add_argument("--source-backup", type=Path, help="Existing backup to restore")
    parser.add_argument(
        "--backup-dir", type=Path, default=PROJECT_ROOT / "backups", help="Backup directory"
    )
    parser.add_argument("--keep", type=int, default=10, help="Backups to retain")
    parser.add_argument("--yes", action="store_true", help="Confirm atomic replacement")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.yes:
        print("Refusing to migrate without --yes.", file=sys.stderr)
        return 2
    try:
        source, report = migrate_working_database(
            args.database, args.backup_dir, args.keep, args.source_backup
        )
    except Exception as error:
        print(f"Migration failed: {error}", file=sys.stderr)
        return 1

    report_path = source.with_name(f"{source.stem}.migration.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Source backup: {source}")
    print(f"Source SHA-256: {report['source_sha256']}")
    print(f"Migrated rows: {sum(report['row_counts'].values())}")
    print(
        "Normalized resource types: "
        f"{report['normalized_resource_types']['scroll_to_scroll_recipe']}"
    )
    print(f"Database migrated: {args.database.expanduser().resolve()}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
