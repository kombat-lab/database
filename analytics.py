import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from database import DbRow, db

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
    return None if value is None else str(value)


def _user_identity(row: DbRow) -> UserIdentity:
    return UserIdentity(
        user_id=int(row['user_id']),
        username=_optional_text(row['username']),
        first_name=_optional_text(row['first_name']),
        last_name=_optional_text(row['last_name']),
        first_seen=_optional_text(row['first_seen']),
        last_activity=_optional_text(row['last_activity']),
    )


def _search_counts(rows: list[DbRow]) -> list[SearchCount]:
    return [SearchCount(query=_optional_text(row['query']), count=int(row['count'])) for row in rows]


# ========== MIDDLEWARE ==========
class AnalyticsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        user = data.get('event_from_user')
        if isinstance(user, User) and not user.is_bot:
            try:
                await db.register_user_if_not_exists(
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name
                )
            except Exception as e:
                logger.warning(f"register_user failed: {e}")
        return await handler(event, data)


# ========== ЛОГИРОВАНИЕ СОБЫТИЙ ==========
async def _log_event(user_id: int, event_type: str, target_id: int | None = None,
                     target_type: str | None = None, metadata: dict[str, object] | None = None) -> None:
    try:
        await db.execute_query(
            """
            INSERT INTO analytics_events
                (user_id, event_type, target_id, target_type, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, event_type, target_id, target_type,
             json.dumps(metadata, ensure_ascii=False) if metadata else None)
        )
    except Exception as e:
        logger.error(f"Failed to log event: {e}")


async def log_start(user_id: int) -> None:
    await _log_event(user_id, 'start')

async def log_view_mob(user_id: int, mob_id: int) -> None:
    await _log_event(user_id, 'view_mob', target_id=mob_id, target_type='mob')

async def log_view_resource(user_id: int, resource_id: int) -> None:
    await _log_event(user_id, 'view_resource', target_id=resource_id, target_type='resource')

async def log_view_gear(user_id: int, gear_id: int) -> None:
    await _log_event(user_id, 'view_gear', target_id=gear_id, target_type='gear')

async def log_view_card(user_id: int, card_id: int) -> None:
    await _log_event(user_id, 'view_card', target_id=card_id, target_type='card')

async def log_search(user_id: int, query: str) -> None:
    await _log_event(user_id, 'search', metadata={'query': query})

async def log_inline_search(user_id: int, query: str) -> None:
    await _log_event(user_id, 'inline_search', metadata={'query': query})

async def log_inline_result_chosen(user_id: int, result_id: str, query: str) -> None:
    await _log_event(user_id, 'inline_choice', metadata={'result_id': result_id, 'query': query})


# ========== СТАТИСТИКА ==========
async def get_active_users_count(days: int = 1) -> int:
    res = await db.execute_query(
        "SELECT COUNT(DISTINCT user_id) as cnt FROM analytics_events WHERE timestamp >= datetime('now', ?)",
        (f'-{days} days',)
    )
    return int(res[0]['cnt']) if res else 0

async def get_retention(cohort_days_ago: int, after_days: int) -> float:
    if cohort_days_ago < 0 or after_days < 0 or after_days > cohort_days_ago:
        raise ValueError("Некорректный период retention")
    result = await db.execute_query(
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
        (f'-{cohort_days_ago - after_days} days', f'-{cohort_days_ago} days'),
    )
    cohort_size = int(result[0]['cohort_size'])
    return int(result[0]['returned_users']) / cohort_size * 100 if cohort_size else 0.0


async def get_top_items_with_names(item_type: str, days: int = 30, limit: int = 30) -> list[TopItem]:
    event_map = {
        'mob': 'view_mob',
        'resource': 'view_resource',
        'gear': 'view_gear',
        'card': 'view_card'
    }
    event = event_map.get(item_type)
    if not event:
        return []
    table = {'mob': 'mobs', 'resource': 'resources', 'gear': 'gear', 'card': 'cards'}[item_type]

    query = f"""
    SELECT
        ae.target_id,
        COUNT(*) as views,
        {table}.name as name,
        {table}.emoji as emoji
    FROM analytics_events ae
    LEFT JOIN {table} ON ae.target_id = {table}.id
    WHERE ae.event_type = ?
    AND ae.timestamp >= datetime('now', ?)
    GROUP BY ae.target_id
    ORDER BY views DESC
    LIMIT ?
    """
    rows = await db.execute_query(query, (event, f'-{days} days', limit))
    return [TopItem(
        target_id=int(row['target_id']) if row['target_id'] is not None else None,
        name=str(row['name'] or f"[Удалён ID {row['target_id']}]"),
        emoji=str(row['emoji'] or '❓'),
        views=int(row['views']),
    ) for row in rows]

async def get_top_search_queries(days: int = 30, limit: int = 30, search_type: str = 'all') -> list[SearchCount]:
    if search_type == 'text':
        event_type = 'search'
    elif search_type == 'inline':
        event_type = 'inline_search'
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
        return _search_counts(await db.execute_query(query, (f'-{days} days', limit)))

    query = """
        SELECT json_extract(metadata, '$.query') as query, COUNT(*) as count
        FROM analytics_events
        WHERE event_type = ? AND timestamp >= datetime('now', ?) AND metadata IS NOT NULL
        GROUP BY query
        ORDER BY count DESC
        LIMIT ?
    """
    return _search_counts(await db.execute_query(query, (event_type, f'-{days} days', limit)))

async def get_db_stats() -> DatabaseStats:
    events = await db.execute_query("SELECT COUNT() as cnt FROM analytics_events")
    users = await db.execute_query("SELECT COUNT() as cnt FROM users")
    size = await db.execute_query(
        "SELECT page_count * page_size as size FROM pragma_page_count(), pragma_page_size()"
    )
    return {
        'events': int(events[0]['cnt']) if events else 0,
        'users': int(users[0]['cnt']) if users else 0,
        'db_size_bytes': int(size[0]['size']) if size else 0
    }


async def get_users_page(offset: int = 0, limit: int = 11) -> list[UserSummary]:
    """Возвращает пользователей с агрегированной активностью, новые первыми."""
    rows = await db.execute_query(
        """
        SELECT
            u.user_id, u.username, u.first_name, u.last_name,
            u.first_seen, u.last_activity,
            COUNT(ae.id) AS event_count,
            COALESCE(SUM(CASE WHEN ae.timestamp >= datetime('now', '-7 days') THEN 1 ELSE 0 END), 0) AS events_7d,
            MAX(ae.timestamp) AS last_event
        FROM users u
        LEFT JOIN analytics_events ae ON ae.user_id = u.user_id
        GROUP BY u.user_id
        ORDER BY u.last_activity DESC, u.user_id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    return [UserSummary(
        **_user_identity(row),
        event_count=int(row['event_count']),
        events_7d=int(row['events_7d']),
        last_event=_optional_text(row['last_event']),
    ) for row in rows]


async def get_user_activity(user_id: int) -> UserActivity | None:
    users = await db.execute_query(
        """
        SELECT user_id, username, first_name, last_name, first_seen, last_activity
        FROM users WHERE user_id = ?
        """,
        (user_id,),
    )
    if not users:
        return None

    totals = await db.execute_query(
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
    event_types = await db.execute_query(
        """
        SELECT event_type, COUNT(*) AS count
        FROM analytics_events
        WHERE user_id = ?
        GROUP BY event_type
        ORDER BY count DESC, event_type
        """,
        (user_id,),
    )
    recent_searches = await db.execute_query(
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
            total_events=int(totals[0]['total_events']),
            events_1d=int(totals[0]['events_1d']),
            events_7d=int(totals[0]['events_7d']),
            events_30d=int(totals[0]['events_30d']),
            first_event=_optional_text(totals[0]['first_event']),
            last_event=_optional_text(totals[0]['last_event']),
        ),
        "event_types": [EventCount(event_type=str(row['event_type']), count=int(row['count'])) for row in event_types],
        "recent_searches": [RecentSearch(
            query=_optional_text(row['query']), timestamp=_optional_text(row['timestamp']),
        ) for row in recent_searches],
    }
