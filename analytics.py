import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from database import Database, DbRow
from storage.types import sql_int, sql_optional_int, sql_optional_text, sql_text

logger = logging.getLogger(__name__)


class UserIdentity(TypedDict):
    user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    first_seen: str | None
    last_activity: str | None


class UserSummary(UserIdentity):
    event_count: int
    events_7d: int
    last_event: str | None


class ActivityTotals(TypedDict):
    total_events: int
    events_1d: int
    events_7d: int
    events_30d: int
    first_event: str | None
    last_event: str | None


class EventCount(TypedDict):
    event_type: str
    count: int


class RecentSearch(TypedDict):
    query: str | None
    timestamp: str | None


class SearchCount(TypedDict):
    query: str | None
    count: int


class TopItem(TypedDict):
    target_id: int | None
    name: str
    emoji: str
    views: int


class DatabaseStats(TypedDict):
    users: int
    events: int
    db_size_bytes: int


class UserActivity(TypedDict):
    user: UserIdentity
    totals: ActivityTotals
    event_types: list[EventCount]
    recent_searches: list[RecentSearch]


def _optional_text(value: object) -> str | None:
    return sql_optional_text(value)


def _user_identity(row: DbRow) -> UserIdentity:
    return UserIdentity(
        user_id=sql_int(row["user_id"]),
        username=_optional_text(row["username"]),
        first_name=_optional_text(row["first_name"]),
        last_name=_optional_text(row["last_name"]),
        first_seen=_optional_text(row["first_seen"]),
        last_activity=_optional_text(row["last_activity"]),
    )


def _search_counts(rows: list[DbRow]) -> list[SearchCount]:
    return [SearchCount(query=_optional_text(row["query"]), count=sql_int(row["count"])) for row in rows]


class AnalyticsMiddleware(BaseMiddleware):
    def __init__(self, database: Database) -> None:
        self.database = database

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if isinstance(user, User) and (not user.is_bot):
            try:
                await self.database.register_user_if_not_exists(
                    user_id=user.id, username=user.username, first_name=user.first_name, last_name=user.last_name
                )
            except Exception as e:
                logger.warning(f"register_user failed: {e}")
        return await handler(event, data)


class AnalyticsService:
    """Database-bound analytics; each application owns its dependencies."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def _log_event(
        self,
        user_id: int,
        event_type: str,
        target_id: int | None = None,
        target_type: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        try:
            await self.database.execute_query(
                """
            INSERT INTO analytics_events
                (user_id, event_type, target_id, target_type, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
                (
                    user_id,
                    event_type,
                    target_id,
                    target_type,
                    json.dumps(metadata, ensure_ascii=False) if metadata else None,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to log event: {e}")

    async def log_start(self, user_id: int) -> None:
        await self._log_event(user_id, "start")

    async def log_view_mob(self, user_id: int, mob_id: int) -> None:
        await self._log_event(user_id, "view_mob", target_id=mob_id, target_type="mob")

    async def log_view_resource(self, user_id: int, resource_id: int) -> None:
        await self._log_event(user_id, "view_resource", target_id=resource_id, target_type="resource")

    async def log_view_gear(self, user_id: int, gear_id: int) -> None:
        await self._log_event(user_id, "view_gear", target_id=gear_id, target_type="gear")

    async def log_view_card(self, user_id: int, card_id: int) -> None:
        await self._log_event(user_id, "view_card", target_id=card_id, target_type="card")

    async def log_search(self, user_id: int, query: str) -> None:
        await self._log_event(user_id, "search", metadata={"query": query})

    async def log_inline_search(self, user_id: int, query: str) -> None:
        await self._log_event(user_id, "inline_search", metadata={"query": query})

    async def log_inline_result_chosen(self, user_id: int, result_id: str, query: str) -> None:
        await self._log_event(user_id, "inline_choice", metadata={"result_id": result_id, "query": query})

    async def get_active_users_count(self, days: int = 1) -> int:
        res = await self.database.execute_query(
            "SELECT COUNT(DISTINCT user_id) as cnt FROM analytics_events WHERE timestamp >= datetime('now', ?)",
            (f"-{days} days",),
        )
        return sql_int(res[0]["cnt"]) if res else 0

    async def get_retention(self, cohort_days_ago: int, after_days: int) -> float:
        if cohort_days_ago < 0 or after_days < 0 or after_days > cohort_days_ago:
            raise ValueError("Некорректный период retention")
        result = await self.database.execute_query(
            """
        SELECT COUNT(*) AS cohort_size,
               COALESCE(SUM(EXISTS(
                   SELECT 1 FROM analytics_events ae
                   WHERE ae.user_id = u.user_id
                     AND DATE(ae.timestamp) = DATE('now', ?)
               )), 0) AS returned_users
        FROM users u
        WHERE DATE(u.first_seen) = DATE('now', ?)
        """,
            (f"-{cohort_days_ago - after_days} days", f"-{cohort_days_ago} days"),
        )
        cohort_size = sql_int(result[0]["cohort_size"])
        return sql_int(result[0]["returned_users"]) / cohort_size * 100 if cohort_size else 0.0

    async def get_top_items_with_names(self, item_type: str, days: int = 30, limit: int = 30) -> list[TopItem]:
        event_map = {"mob": "view_mob", "resource": "view_resource", "gear": "view_gear", "card": "view_card"}
        event = event_map.get(item_type)
        if not event:
            return []
        table = {"mob": "mobs", "resource": "resources", "gear": "gear", "card": "cards"}[item_type]
        query = f"""
        SELECT ae.target_id, COUNT(*) AS views, {table}.name AS name, {table}.emoji AS emoji
        FROM analytics_events ae
        LEFT JOIN {table} ON ae.target_id = {table}.id
        WHERE ae.event_type = ? AND ae.timestamp >= datetime('now', ?)
        GROUP BY ae.target_id
        ORDER BY views DESC
        LIMIT ?
        """
        rows = await self.database.execute_query(query, (event, f"-{days} days", limit))
        return [
            TopItem(
                target_id=sql_optional_int(row["target_id"]),
                name=sql_optional_text(row["name"]) or f"[Удалён ID {sql_optional_int(row['target_id'])}]",
                emoji=sql_optional_text(row["emoji"]) or "❓",
                views=sql_int(row["views"]),
            )
            for row in rows
        ]

    async def get_top_search_queries(
        self, days: int = 30, limit: int = 30, search_type: str = "all"
    ) -> list[SearchCount]:
        if search_type == "text":
            event_type = "search"
        elif search_type == "inline":
            event_type = "inline_search"
        else:
            query = """
        SELECT json_extract(metadata, '$.query') as query, COUNT(*) as count
        FROM analytics_events
        WHERE event_type IN ('search', 'inline_search')
        AND timestamp >= datetime('now', ?)
        AND metadata IS NOT NULL
        GROUP BY query
        ORDER BY count DESC
        LIMIT ?
        """
            return _search_counts(await self.database.execute_query(query, (f"-{days} days", limit)))
        query = """
        SELECT json_extract(metadata, '$.query') as query, COUNT(*) as count
        FROM analytics_events
        WHERE event_type = ? AND timestamp >= datetime('now', ?) AND metadata IS NOT NULL
        GROUP BY query
        ORDER BY count DESC
        LIMIT ?
    """
        return _search_counts(await self.database.execute_query(query, (event_type, f"-{days} days", limit)))

    async def get_db_stats(self) -> DatabaseStats:
        events = await self.database.execute_query("SELECT COUNT() as cnt FROM analytics_events")
        users = await self.database.execute_query("SELECT COUNT() as cnt FROM users")
        size = await self.database.execute_query(
            "SELECT page_count * page_size as size FROM pragma_page_count(), pragma_page_size()"
        )
        return {
            "events": sql_int(events[0]["cnt"]) if events else 0,
            "users": sql_int(users[0]["cnt"]) if users else 0,
            "db_size_bytes": sql_int(size[0]["size"]) if size else 0,
        }

    async def get_users_page(self, offset: int = 0, limit: int = 11) -> list[UserSummary]:
        """Возвращает пользователей с агрегированной активностью, новые первыми."""
        rows = await self.database.execute_query(
            """
        WITH page AS (
            SELECT user_id, username, first_name, last_name, first_seen, last_activity
            FROM users ORDER BY last_activity DESC,user_id DESC LIMIT ? OFFSET ?
        )
        SELECT
            u.user_id, u.username, u.first_name, u.last_name,
            u.first_seen, u.last_activity,
            COUNT(ae.id) AS event_count,
            COALESCE(SUM(CASE WHEN ae.timestamp >= datetime('now', '-7 days') THEN 1 ELSE 0 END), 0) AS events_7d,
            MAX(ae.timestamp) AS last_event
        FROM page u
        LEFT JOIN analytics_events ae ON ae.user_id = u.user_id
        GROUP BY u.user_id
        ORDER BY u.last_activity DESC, u.user_id DESC
        """,
            (limit, offset),
        )
        return [
            UserSummary(
                **_user_identity(row),
                event_count=sql_int(row["event_count"]),
                events_7d=sql_int(row["events_7d"]),
                last_event=_optional_text(row["last_event"]),
            )
            for row in rows
        ]

    async def get_user_activity(self, user_id: int) -> UserActivity | None:
        users = await self.database.execute_query(
            """
        SELECT user_id, username, first_name, last_name, first_seen, last_activity
        FROM users WHERE user_id = ?
        """,
            (user_id,),
        )
        if not users:
            return None
        totals = await self.database.execute_query(
            """
        SELECT
            COUNT(*) AS total_events,
            COALESCE(SUM(CASE WHEN timestamp >= datetime('now', '-1 day') THEN 1 ELSE 0 END), 0) AS events_1d,
            COALESCE(SUM(CASE WHEN timestamp >= datetime('now', '-7 days') THEN 1 ELSE 0 END), 0) AS events_7d,
            COALESCE(SUM(CASE WHEN timestamp >= datetime('now', '-30 days') THEN 1 ELSE 0 END), 0) AS events_30d,
            MIN(timestamp) AS first_event,
            MAX(timestamp) AS last_event
        FROM analytics_events WHERE user_id = ?
        """,
            (user_id,),
        )
        event_types = await self.database.execute_query(
            """
        SELECT event_type, COUNT(*) AS count
        FROM analytics_events
        WHERE user_id = ?
        GROUP BY event_type
        ORDER BY count DESC, event_type
        """,
            (user_id,),
        )
        recent_searches = await self.database.execute_query(
            """
        SELECT json_extract(metadata, '$.query') AS query, timestamp
        FROM analytics_events
        WHERE user_id = ?
          AND event_type IN ('search', 'inline_search')
          AND metadata IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 10
        """,
            (user_id,),
        )
        return {
            "user": _user_identity(users[0]),
            "totals": ActivityTotals(
                total_events=sql_int(totals[0]["total_events"]),
                events_1d=sql_int(totals[0]["events_1d"]),
                events_7d=sql_int(totals[0]["events_7d"]),
                events_30d=sql_int(totals[0]["events_30d"]),
                first_event=_optional_text(totals[0]["first_event"]),
                last_event=_optional_text(totals[0]["last_event"]),
            ),
            "event_types": [
                EventCount(event_type=sql_text(row["event_type"]), count=sql_int(row["count"])) for row in event_types
            ],
            "recent_searches": [
                RecentSearch(query=_optional_text(row["query"]), timestamp=_optional_text(row["timestamp"]))
                for row in recent_searches
            ],
        }
