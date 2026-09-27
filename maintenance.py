"""Read-only retention planning for historical records.

Existing catalog history is never deleted by a background timer. Cleanup can
be considered separately after reviewing counts and taking a database backup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from storage.types import sql_int

if TYPE_CHECKING:
    from database import Database


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    completed_drafts: int
    analytics_events: int
    draft_days: int
    analytics_days: int


async def preview_retention(database: Database, *, draft_days: int = 90, analytics_days: int = 365) -> RetentionPreview:
    if not 1 <= draft_days <= 36500 or not 1 <= analytics_days <= 36500:
        raise ValueError("Retention periods must be between 1 and 36500 days")
    drafts = await database.execute_query(
        "SELECT COUNT(*) AS n FROM gear_drafts WHERE status IN ('saved','cancelled') "
        "AND updated_at < datetime('now',?)",
        (f"-{draft_days} days",),
    )
    events = await database.execute_query(
        "SELECT COUNT(*) AS n FROM analytics_events WHERE timestamp < datetime('now',?)",
        (f"-{analytics_days} days",),
    )
    return RetentionPreview(sql_int(drafts[0]["n"]), sql_int(events[0]["n"]), draft_days, analytics_days)


def main() -> None:
    """Print a retention preview using SQLite read-only mode, without migrations."""
    import argparse
    import json
    import sqlite3
    from dataclasses import asdict
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Preview historical data retention; never deletes records.")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--draft-days", type=int, default=90)
    parser.add_argument("--analytics-days", type=int, default=365)
    args = parser.parse_args()
    if not 1 <= args.draft_days <= 36500 or not 1 <= args.analytics_days <= 36500:
        parser.error("retention periods must be between 1 and 36500 days")
    path = args.database.resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        drafts = connection.execute(
            "SELECT COUNT(*) FROM gear_drafts WHERE status IN ('saved','cancelled') AND updated_at < datetime('now',?)",
            (f"-{args.draft_days} days",),
        ).fetchone()[0]
        events = connection.execute(
            "SELECT COUNT(*) FROM analytics_events WHERE timestamp < datetime('now',?)",
            (f"-{args.analytics_days} days",),
        ).fetchone()[0]
        result = RetentionPreview(drafts, events, args.draft_days, args.analytics_days)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    finally:
        connection.close()


if __name__ == "__main__":
    main()
