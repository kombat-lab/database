"""Back up and migrate SQLite data into the current application schema."""

import argparse
import asyncio
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

from scripts.reset_database import (  # noqa: E402
    EXPECTED_TABLES,
    create_backup,
    create_clean_database,
    ensure_database_is_idle,
    inspect_database,
    prune_backups,
    remove_sqlite_files,
    remove_sqlite_sidecars,
    sha256,
)


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
