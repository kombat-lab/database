# analytics.py
import json
import asyncio
import logging
from typing import Dict, Any, Optional, Callable, Awaitable, List
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User
from database import db

logger = logging.getLogger(__name__)

# ============================================================
# Буферизация событий (опционально)
# ============================================================

class AnalyticsBuffer:
    def __init__(self, flush_interval: float = 5.0, batch_size: int = 100):
        self.queue = []
        self.flush_interval = flush_interval
        self.batch_size = batch_size
        self.last_flush = asyncio.get_event_loop().time()
        self._task = None

    async def add(self, event: Dict[str, Any]):
        self.queue.append(event)
        if len(self.queue) >= self.batch_size or \
           asyncio.get_event_loop().time() - self.last_flush >= self.flush_interval:
            await self.flush()

    async def flush(self):
        if not self.queue:
            return
        events = self.queue[:]
        self.queue.clear()
        self.last_flush = asyncio.get_event_loop().time()
        try:
            async with db.transaction():
                for ev in events:
                    await db._conn.execute(
                        """
                        INSERT INTO analytics_events (user_id, event_type, target_id, target_type, metadata)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (ev['user_id'], ev['event_type'], ev.get('target_id'), ev.get('target_type'),
                         json.dumps(ev.get('metadata'), ensure_ascii=False) if ev.get('metadata') else None)
                    )
        except Exception as e:
            logger.error(f"Failed to flush analytics events: {e}")

    async def start(self):
        async def periodic_flush():
            while True:
                await asyncio.sleep(self.flush_interval)
                await self.flush()
        self._task = asyncio.create_task(periodic_flush())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.flush()

analytics_buffer = None

# ============================================================
# Middleware
# ============================================================

class AnalyticsMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        user: User = data.get('event_from_user')
        if user and not user.is_bot:
            await db.register_user_if_not_exists(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
        return await handler(event, data)

# ============================================================
# Логирование событий
# ============================================================

async def _log_event(user_id: int, event_type: str, target_id: int = None,
                     target_type: str = None, metadata: dict = None):
    event = {
        'user_id': user_id,
        'event_type': event_type,
        'target_id': target_id,
        'target_type': target_type,
        'metadata': metadata
    }
    if analytics_buffer:
        await analytics_buffer.add(event)
    else:
        try:
            await db._conn.execute(
                """
                INSERT INTO analytics_events (user_id, event_type, target_id, target_type, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user_id, event_type, target_id, target_type,
                 json.dumps(metadata, ensure_ascii=False) if metadata else None)
            )
            await db._conn.commit()
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

# ============================================================
# Функции для получения статистики
# ============================================================

async def get_active_users_count(days: int = 1) -> int:
    res = await db.execute_query(
        "SELECT COUNT(DISTINCT user_id) as cnt FROM analytics_events WHERE timestamp >= datetime('now', ?)",
        (f'-{days} days',)
    )
    return res[0]['cnt'] if res else 0

async def get_retention(cohort_days_ago: int, after_days: int) -> float:
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
        tuple(user_ids) + (f'-{after_days} days',)
    )
    return (active[0]['cnt'] / len(user_ids)) * 100

async def get_section_popularity(days: int = 30) -> List[Dict]:
    return await db.execute_query(
        """
        SELECT target_type as section, COUNT(*) as views
        FROM analytics_events
        WHERE event_type = 'open_section'
          AND timestamp >= datetime('now', ?)
        GROUP BY target_type
        ORDER BY views DESC
        """,
        (f'-{days} days',)
    )

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
    if item_type == 'mob':
        table = 'mobs'
    elif item_type == 'resource':
        table = 'resources'
    elif item_type == 'gear':
        table = 'gear'
    else:
        table = 'cards'
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
    """
    search_type: 'all', 'text', 'inline'
    """
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
    events = await db.execute_query("SELECT COUNT(*) as cnt FROM analytics_events")
    users = await db.execute_query("SELECT COUNT(*) as cnt FROM users")
    size = await db.execute_query("SELECT page_count * page_size as size FROM pragma_page_count(), pragma_page_size()")
    return {
        'events': events[0]['cnt'] if events else 0,
        'users': users[0]['cnt'] if users else 0,
        'db_size_bytes': size[0]['size'] if size else 0
    }

async def reset_analytics_data():
    await db.execute_query("DELETE FROM analytics_events")
    await db.execute_query("DELETE FROM users")
    await db.execute_query("DELETE FROM sqlite_sequence WHERE name IN ('analytics_events', 'users')")
