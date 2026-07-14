import json
import logging
from typing import Dict, Any, Optional, Callable, Awaitable, List
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from database import db

logger = logging.getLogger(__name__)

# Флаг, что идёт сброс – запрещаем запись в БД
_resetting = False


# ========== MIDDLEWARE ==========
class AnalyticsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: User = data.get('event_from_user')
        if user and not user.is_bot and not _resetting:
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
async def _log_event(user_id: int, event_type: str, target_id: int = None,
                     target_type: str = None, metadata: dict = None):
    if _resetting:
        return
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


async def log_start(user_id: int):
    await _log_event(user_id, 'start')

async def log_open_section(user_id: int, section: str):
    await _log_event(user_id, 'open_section', target_type=section)

async def log_view_mob(user_id: int, mob_id: int):
    await _log_event(user_id, 'view_mob', target_id=mob_id, target_type='mob')

async def log_view_resource(user_id: int, resource_id: int):
    await _log_event(user_id, 'view_resource', target_id=resource_id, target_type='resource')

async def log_view_gear(user_id: int, gear_id: int):
    await _log_event(user_id, 'view_gear', target_id=gear_id, target_type='gear')

async def log_view_card(user_id: int, card_id: int):
    await _log_event(user_id, 'view_card', target_id=card_id, target_type='card')

async def log_search(user_id: int, query: str):
    await _log_event(user_id, 'search', metadata={'query': query})

async def log_inline_search(user_id: int, query: str):
    await _log_event(user_id, 'inline_search', metadata={'query': query})

async def log_inline_result_chosen(user_id: int, result_id: str, query: str):
    await _log_event(user_id, 'inline_choice', metadata={'result_id': result_id, 'query': query})


# ========== СТАТИСТИКА ==========
async def get_active_users_count(days: int = 1) -> int:
    res = await db.execute_query(
        "SELECT COUNT(DISTINCT user_id) as cnt FROM analytics_events WHERE timestamp >= datetime('now', ?)",
        (f'-{days} days',)
    )
    return res[0]['cnt'] if res else 0

async def get_retention(cohort_days_ago: int, after_days: int) -> float:
    if cohort_days_ago < 0 or after_days < 0 or after_days > cohort_days_ago:
        raise ValueError("Некорректный период retention")
    first_visitors = await db.execute_query(
        "SELECT user_id FROM users WHERE DATE(first_seen) = DATE('now', ?)",
        (f'-{cohort_days_ago} days',)
    )
    if not first_visitors:
        return 0.0
    user_ids = [u['user_id'] for u in first_visitors]
    placeholders = ','.join('?' * len(user_ids))
    active = await db.execute_query(
        f"""
        SELECT COUNT(DISTINCT user_id) as cnt
        FROM analytics_events
        WHERE user_id IN ({placeholders})
        AND DATE(timestamp) = DATE('now', ?)
        """,
        tuple(user_ids) + (f'-{cohort_days_ago - after_days} days',)
    )
    return (active[0]['cnt'] / len(user_ids)) * 100

async def get_top_items_with_names(item_type: str, days: int = 30, limit: int = 30) -> List[Dict]:
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
    for row in rows:
        if not row['name']:
            row['name'] = f"[Удалён ID {row['target_id']}]"
        if not row['emoji']:
            row['emoji'] = '❓'
    return rows

async def get_top_search_queries(days: int = 30, limit: int = 30, search_type: str = 'all') -> List[Dict]:
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
        return await db.execute_query(query, (f'-{days} days', limit))

    query = """
        SELECT json_extract(metadata, '$.query') as query, COUNT(*) as count
        FROM analytics_events
        WHERE event_type = ? AND timestamp >= datetime('now', ?) AND metadata IS NOT NULL
        GROUP BY query
        ORDER BY count DESC
        LIMIT ?
    """
    return await db.execute_query(query, (event_type, f'-{days} days', limit))

async def get_db_stats() -> dict:
    events = await db.execute_query("SELECT COUNT() as cnt FROM analytics_events")
    users = await db.execute_query("SELECT COUNT() as cnt FROM users")
    size = await db.execute_query(
        "SELECT page_count * page_size as size FROM pragma_page_count(), pragma_page_size()"
    )
    return {
        'events': events[0]['cnt'] if events else 0,
        'users': users[0]['cnt'] if users else 0,
        'db_size_bytes': size[0]['size'] if size else 0
    }


# ========== СБРОС АНАЛИТИКИ ==========
async def reset_analytics_data():
    global _resetting
    logger.warning("=== СБРОС АНАЛИТИКИ ===")
    _resetting = True
    try:
        await db.execute_query("DELETE FROM users")          # каскадно удалит analytics_events
        await db.execute_query("DELETE FROM sqlite_sequence WHERE name = 'analytics_events'")
        await db.vacuum()                                   # метод vacuum() должен быть в Database
        logger.info("Таблицы очищены, VACUUM выполнен")
    except Exception as e:
        logger.exception(f"Ошибка при сбросе: {e}")
        raise
    finally:
        _resetting = False
