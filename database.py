import os
import asyncio
import json
import logging
import aiosqlite
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "game.db")
CURRENT_SCHEMA_VERSION = 2


def _lower_unicode(s: str) -> str:
    if s is None:
        return None
    return s.lower()


class Database:
    ALLOWED_MOB_FIELDS = {'name', 'emoji', 'hp', 'dust_min', 'dust_max', 'exp', 'location_id'}
    
    SLOT_ORDER = {
        'шлем': 1,
        'плечи': 2,
        'тело': 3,
        'плащ': 4,
        'пояс': 5,
        'штаны': 6,
        'ботинки': 7,
        'перчатки': 8,
        'кольцо': 9,
        'амул': 10,
        'серьга': 11,
        'основная рука': 12,
        'вторая рука': 13,
    }

    def __init__(self, path: str | None = None):
        self.path = path or DB_PATH
        self._conn: Optional[aiosqlite.Connection] = None
        self._locations_cache: Dict[int, Dict] = {}
        self._connection_lock = asyncio.Lock()
        self._connection_lock_owner = None
        self._connection_lock_depth = 0
        self._transaction_depth = 0

    @asynccontextmanager
    async def _connection_guard(self):
        """Serialize work on the shared connection while allowing nested DB calls."""
        task = asyncio.current_task()
        if self._connection_lock_owner is task:
            self._connection_lock_depth += 1
            try:
                yield
            finally:
                self._connection_lock_depth -= 1
            return

        await self._connection_lock.acquire()
        self._connection_lock_owner = task
        self._connection_lock_depth = 1
        try:
            yield
        finally:
            self._connection_lock_depth = 0
            self._connection_lock_owner = None
            self._connection_lock.release()

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path, timeout=30.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA busy_timeout = 30000")
        await self._conn.create_function("LOWER_UNICODE", 1, _lower_unicode)
        await self._ensure_schema()
        await self._ensure_indexes()
        await self._load_locations_cache()
        logger.info("Database connected: %s", self.path)

    async def _ensure_schema(self):
        """Create a complete empty database without touching existing rows."""
        statements = [
            """
            CREATE TABLE IF NOT EXISTS schema_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            f"""
            INSERT INTO schema_metadata (key, value)
            VALUES ('schema_version', '{CURRENT_SCHEMA_VERSION}')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            """
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS mobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '',
                hp INTEGER NOT NULL DEFAULT 0,
                dust_min INTEGER NOT NULL DEFAULT 0,
                dust_max INTEGER NOT NULL DEFAULT 0,
                exp INTEGER NOT NULL DEFAULT 0,
                location_id INTEGER NOT NULL,
                FOREIGN KEY (location_id) REFERENCES locations(id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '',
                type TEXT NOT NULL DEFAULT 'craft',
                note TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS gear (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL DEFAULT 'common',
                slot TEXT NOT NULL DEFAULT '',
                emoji TEXT NOT NULL DEFAULT '',
                level INTEGER NOT NULL DEFAULT 1,
                classes TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                emoji TEXT NOT NULL DEFAULT '',
                slot TEXT NOT NULL DEFAULT '',
                bonus1 TEXT NOT NULL DEFAULT '',
                bonus2 TEXT NOT NULL DEFAULT '',
                bonus3 TEXT NOT NULL DEFAULT '',
                bonus4 TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS drops (
                mob_id INTEGER NOT NULL,
                item_type TEXT NOT NULL,
                item_id INTEGER NOT NULL,
                PRIMARY KEY (mob_id, item_type, item_id),
                FOREIGN KEY (mob_id) REFERENCES mobs(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                result_type TEXT NOT NULL,
                result_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                UNIQUE (result_type, result_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS recipe_ingredients (
                recipe_id INTEGER NOT NULL,
                resource_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (recipe_id, resource_id),
                FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                FOREIGN KEY (resource_id) REFERENCES resources(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS recipe_owners (
                recipe_id INTEGER NOT NULL,
                player_username TEXT NOT NULL,
                PRIMARY KEY (recipe_id, player_username),
                FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
                last_activity DATETIME DEFAULT CURRENT_TIMESTAMP,
                username TEXT,
                first_name TEXT,
                last_name TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS analytics_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                target_id INTEGER,
                target_type TEXT,
                metadata TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """,
        ]
        for statement in statements:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def _ensure_indexes(self):
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_mobs_location ON mobs(location_id)",
            "CREATE INDEX IF NOT EXISTS idx_mobs_location_hp ON mobs(location_id, hp, id)",
            "CREATE INDEX IF NOT EXISTS idx_mobs_name ON mobs(name)",
            "CREATE INDEX IF NOT EXISTS idx_resources_name ON resources(name)",
            "CREATE INDEX IF NOT EXISTS idx_gear_name ON gear(name)",
            "CREATE INDEX IF NOT EXISTS idx_drops_mob ON drops(mob_id)",
            "CREATE INDEX IF NOT EXISTS idx_drops_item ON drops(item_type, item_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_result ON recipes(result_type, result_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_owners_recipe ON recipe_owners(recipe_id)",
            "CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)",
            "CREATE INDEX IF NOT EXISTS idx_resources_type_name ON resources(type, name)",
            "CREATE INDEX IF NOT EXISTS idx_gear_rarity_slot ON gear(rarity, slot)",
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON analytics_events(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_events_type ON analytics_events(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_events_target ON analytics_events(target_type, target_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_user_timestamp ON analytics_events(user_id, timestamp)",
        ]
        for sql in indexes:
            await self._conn.execute(sql)
        await self._conn.commit()

    async def _load_locations_cache(self):
        locations = await self.execute_query("SELECT id, name, emoji FROM locations")
        self._locations_cache = {loc["id"]: dict(loc) for loc in locations}

    async def close(self):
        async with self._connection_guard():
            if self._conn:
                await self._conn.close()
                self._conn = None

    async def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        async with self._connection_guard():
            try:
                async with self._conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    if not query.lstrip().upper().startswith(("SELECT", "PRAGMA")) and self._transaction_depth == 0:
                        await self._conn.commit()
                    if not rows:
                        return []
                    if not hasattr(rows[0], 'keys'):
                        col_names = [desc[0] for desc in cursor.description]
                        return [dict(zip(col_names, row)) for row in rows]
                    return [dict(row) for row in rows]
            except Exception as e:
                if self._transaction_depth == 0:
                    try:
                        await self._conn.rollback()
                    except Exception:
                        pass
                logger.error("[DB ERROR] %s", e)
                raise

    async def execute_insert(self, query: str, params: tuple = ()) -> int:
        """Execute one INSERT and return its row id without a concurrency race."""
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        async with self._connection_guard():
            try:
                cursor = await self._conn.execute(query, params)
                try:
                    row_id = cursor.lastrowid
                finally:
                    await cursor.close()
                if self._transaction_depth == 0:
                    await self._conn.commit()
                return int(row_id)
            except Exception:
                if self._transaction_depth == 0:
                    try:
                        await self._conn.rollback()
                    except Exception:
                        pass
                raise

    @asynccontextmanager
    async def transaction(self):
        """Run DB calls atomically and isolate them from concurrent handlers."""
        async with self._connection_guard():
            nested = self._transaction_depth > 0
            savepoint = f"nested_{self._transaction_depth + 1}"
            if nested:
                await self._conn.execute(f"SAVEPOINT {savepoint}")
            else:
                await self._conn.execute("BEGIN IMMEDIATE")
            self._transaction_depth += 1
            try:
                yield
            except BaseException:
                self._transaction_depth -= 1
                if nested:
                    await self._conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    await self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    await self._conn.rollback()
                raise
            else:
                self._transaction_depth -= 1
                if nested:
                    await self._conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    await self._conn.commit()

    async def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        return self._locations_cache.get(location_id)

    async def get_locations(self) -> List[Dict]:
        return list(self._locations_cache.values())

    # ========== АНАЛИТИКА ==========
    async def register_user_if_not_exists(self, user_id: int, username: str = None,
                                          first_name: str = None, last_name: str = None):
        await self.execute_query(
            """
            INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_activity)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                last_activity = CURRENT_TIMESTAMP
            """,
            (user_id, username, first_name, last_name)
        )

    # ========== ПОИСК ==========
    async def search(self, query: str) -> Dict[str, List[Dict]]:
        like_pattern = f"%{query}%"
        results = {"mobs": [], "resources": [], "gear": [], "cards": []}

        mobs = await self.execute_query("""
            SELECT m.id, m.name, m.emoji, m.hp, m.dust_min, m.dust_max, m.exp,
                   l.name AS location_name, l.emoji AS location_emoji
            FROM mobs m
            JOIN locations l ON m.location_id = l.id
            WHERE LOWER_UNICODE(m.name) LIKE LOWER_UNICODE(?)
            ORDER BY m.id
            LIMIT 50
        """, (like_pattern,))
        results["mobs"] = mobs

        resources = await self.execute_query("""
            SELECT id, name, emoji, type
            FROM resources
            WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?)
            ORDER BY id
            LIMIT 50
        """, (like_pattern,))
        results["resources"] = resources

        gear = await self.execute_query("""
            SELECT id, name, emoji, rarity, slot
            FROM gear
            WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?)
            ORDER BY id
            LIMIT 50
        """, (like_pattern,))
        results["gear"] = gear

        cards = await self.execute_query("""
            SELECT id, name, emoji, slot
            FROM cards
            WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?)
            ORDER BY id
            LIMIT 50
        """, (like_pattern,))
        results["cards"] = cards

        return results

    # ========== МОБЫ ==========
    async def get_mob_full_card(self, mob_id: int) -> Optional[Dict]:
        query = """
            SELECT
                m.id, m.name, m.emoji, m.hp, m.dust_min, m.dust_max, m.exp, m.location_id,
                l.name as loc_name, l.emoji as loc_emoji,
                (SELECT json_group_array(json_object('id', r.id, 'name', r.name, 'emoji', r.emoji))
                 FROM drops d JOIN resources r ON d.item_id = r.id
                 WHERE d.mob_id = m.id AND d.item_type = 'resource') as resource_drops,
                (SELECT json_group_array(json_object(
                    'id', g.id, 'name', g.name, 'emoji', g.emoji,
                    'slot', g.slot, 'rarity', g.rarity
                 ))
                 FROM drops d JOIN gear g ON d.item_id = g.id
                 WHERE d.mob_id = m.id AND d.item_type = 'gear') as gear_drops,
                (SELECT json_group_array(json_object(
                    'id', c.id, 'name', c.name, 'emoji', c.emoji, 'slot', c.slot
                 ))
                 FROM drops d JOIN cards c ON d.item_id = c.id
                 WHERE d.mob_id = m.id AND d.item_type = 'card') as card_drops
            FROM mobs m
            JOIN locations l ON m.location_id = l.id
            WHERE m.id = ?
        """
        res = await self.execute_query(query, (mob_id,))
        if not res:
            return None
        row = res[0]
        row["resource_drops"] = json.loads(row["resource_drops"] or "[]")
        row["gear_drops"] = json.loads(row["gear_drops"] or "[]")
        row["card_drops"] = json.loads(row["card_drops"] or "[]")
        return row

    async def get_mobs_by_location_sorted_by_hp(self, location_id: int, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs "
            "WHERE location_id = ? ORDER BY hp ASC, id LIMIT ? OFFSET ?",
            (location_id, limit, offset)
        )

    async def get_prev_next_mob_by_hp(self, mob_id: int, location_id: int) -> Dict[str, Optional[int]]:
        rows = await self.execute_query(
            """
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY hp ASC, id) AS prev_id,
                       LEAD(id) OVER (ORDER BY hp ASC, id) AS next_id
                FROM mobs
                WHERE location_id = ?
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (location_id, mob_id),
        )
        return rows[0] if rows else {'prev_id': None, 'next_id': None}

    async def update_mob_field(self, mob_id: int, field: str, value):
        if field not in self.ALLOWED_MOB_FIELDS:
            raise ValueError(f"Invalid field: {field}")
        query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
        await self.execute_query(query, (value, mob_id))

    async def delete_mob(self, mob_id: int):
        async with self.transaction():
            await self.execute_query("DELETE FROM drops WHERE mob_id = ?", (mob_id,))
            await self.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))

    # ========== РЕСУРСЫ ==========
    async def get_resource_card(self, resource_id: int) -> Optional[Dict]:
        query = """
            SELECT r.id, r.name, r.emoji, r.type, r.note,
                   (SELECT json_group_array(json_object(
                       'id', m.id, 'name', m.name, 'emoji', m.emoji,
                       'location_name', l.name, 'location_emoji', l.emoji
                    ))
                    FROM drops d
                    JOIN mobs m ON d.mob_id = m.id
                    JOIN locations l ON m.location_id = l.id
                    WHERE d.item_type = 'resource' AND d.item_id = r.id) AS mobs
            FROM resources r
            WHERE r.id = ?
        """
        res = await self.execute_query(query, (resource_id,))
        if not res:
            return None
        row = res[0]
        row["mobs"] = json.loads(row["mobs"] or "[]")
        return row

    async def get_resources_by_location(self, location_id: int, offset: int, limit: int) -> List[Dict]:
        query = """
            SELECT DISTINCT r.id, r.name, r.emoji, r.type
            FROM resources r
            JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
            JOIN mobs m ON d.mob_id = m.id
            WHERE m.location_id = ?
            ORDER BY r.id LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (location_id, limit, offset))

    async def get_resource_by_id(self, resource_id: int) -> Optional[Dict]:
        res = await self.execute_query(
            "SELECT id, name, emoji, type, note FROM resources WHERE id = ?",
            (resource_id,)
        )
        return res[0] if res else None

    async def add_resource(self, name: str, emoji: str, resource_type: str = 'craft', note: str = '') -> int:
        return await self.execute_insert(
            "INSERT INTO resources (name, emoji, type, note) VALUES (?, ?, ?, ?)",
            (name, emoji, resource_type, note)
        )

    async def update_resource(self, resource_id: int, name: str = None, emoji: str = None, resource_type: str = None, note: str = None):
        async with self.transaction():
            current = await self.get_resource_by_id(resource_id)
            if not current:
                raise ValueError("Resource not found")
            new_name = name if name is not None else current['name']
            new_emoji = emoji if emoji is not None else current['emoji']
            new_type = resource_type if resource_type is not None else current['type']
            new_note = note if note is not None else current['note']
            await self.execute_query(
                "UPDATE resources SET name=?, emoji=?, type=?, note=? WHERE id=?",
                (new_name, new_emoji, new_type, new_note, resource_id)
            )

    async def _delete_recipes_by_result(self, result_type: str, result_id: int):
        recipes = await self.execute_query(
            "SELECT id FROM recipes WHERE result_type = ? AND result_id = ?",
            (result_type, result_id),
        )
        for recipe in recipes:
            recipe_id = recipe['id']
            await self.execute_query(
                "DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,)
            )
            await self.execute_query(
                "DELETE FROM recipe_owners WHERE recipe_id = ?", (recipe_id,)
            )
        await self.execute_query(
            "DELETE FROM recipes WHERE result_type = ? AND result_id = ?",
            (result_type, result_id),
        )

    async def delete_resource(self, resource_id: int):
        async with self.transaction():
            await self.execute_query("DELETE FROM drops WHERE item_type = 'resource' AND item_id = ?", (resource_id,))
            await self.execute_query("DELETE FROM recipe_ingredients WHERE resource_id = ?", (resource_id,))
            await self._delete_recipes_by_result('resource', resource_id)
            await self.execute_query("DELETE FROM resources WHERE id = ?", (resource_id,))

    async def get_resources_by_type(self, resource_type: str, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, type FROM resources WHERE type = ? ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
            (resource_type, limit, offset)
        )

    async def get_all_resources_simple(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM resources ORDER BY id")

    async def get_prev_next_resource_by_type(self, resource_id: int, resource_type: str) -> Dict[str, Optional[int]]:
        rows = await self.execute_query(
            """
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY name COLLATE NOCASE, id) AS prev_id,
                       LEAD(id) OVER (ORDER BY name COLLATE NOCASE, id) AS next_id
                FROM resources
                WHERE type = ?
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (resource_type, resource_id),
        )
        return rows[0] if rows else {'prev_id': None, 'next_id': None}

    # ========== СНАРЯЖЕНИЕ ==========
    async def get_all_gear(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji, level, classes, note FROM gear ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_gear_by_id(self, gear_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT * FROM gear WHERE id = ?", (gear_id,))
        return res[0] if res else None

    async def add_gear(self, name: str, rarity: str, slot: str, emoji: str, level: int = 1, classes: str = "", note: str = "") -> int:
        return await self.execute_insert(
            "INSERT INTO gear (name, rarity, slot, emoji, level, classes, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, rarity, slot, emoji, level, classes, note)
        )

    async def update_gear(self, gear_id: int, name: str = None, rarity: str = None, slot: str = None, emoji: str = None, level: int = None, classes: str = None, note: str = None):
        async with self.transaction():
            current = await self.get_gear_by_id(gear_id)
            if not current:
                raise ValueError("Gear not found")
            new_name = name if name is not None else current['name']
            new_rarity = rarity if rarity is not None else current['rarity']
            new_slot = slot if slot is not None else current['slot']
            new_emoji = emoji if emoji is not None else current['emoji']
            new_level = level if level is not None else current.get('level', 1)
            new_classes = classes if classes is not None else current.get('classes', '')
            new_note = note if note is not None else current.get('note', '')
            await self.execute_query(
                "UPDATE gear SET name=?, rarity=?, slot=?, emoji=?, level=?, classes=?, note=? WHERE id=?",
                (new_name, new_rarity, new_slot, new_emoji, new_level, new_classes, new_note, gear_id)
            )

    async def delete_gear(self, gear_id: int):
        async with self.transaction():
            await self.execute_query("DELETE FROM drops WHERE item_type='gear' AND item_id=?", (gear_id,))
            await self._delete_recipes_by_result('gear', gear_id)
            await self.execute_query("DELETE FROM gear WHERE id=?", (gear_id,))

    async def get_gear_card(self, gear_id: int) -> Optional[Dict]:
        query = """
            SELECT g.id, g.name, g.rarity, g.slot, g.emoji, g.level, g.classes, g.note,
                   (SELECT rc.id FROM recipes rc
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as recipe_id,
                   (SELECT json_group_array(json_object('id', m.id, 'name', m.name, 'emoji', m.emoji))
                    FROM drops d JOIN mobs m ON d.mob_id = m.id
                    WHERE d.item_type = 'gear' AND d.item_id = g.id) as mobs,
                   (SELECT json_group_array(DISTINCT json_object(
                       'id', m.id, 'name', m.name, 'emoji', m.emoji
                    ))
                    FROM recipes rc
                    JOIN recipe_ingredients ri ON ri.recipe_id = rc.id
                    JOIN resources scroll ON scroll.id = ri.resource_id
                    JOIN drops d ON d.item_type = 'resource' AND d.item_id = scroll.id
                    JOIN mobs m ON m.id = d.mob_id
                    WHERE rc.result_type = 'gear'
                      AND rc.result_id = g.id
                      AND scroll.type = 'scroll_recipe') as scroll_mobs,
                   (SELECT json_group_array(json_object(
                       'id', ri.resource_id, 'name', r.name,
                       'emoji', r.emoji, 'type', r.type, 'quantity', ri.quantity
                    ))
                    FROM recipes rc
                    JOIN recipe_ingredients ri ON rc.id = ri.recipe_id
                    JOIN resources r ON ri.resource_id = r.id
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as ingredients,
                   (SELECT json_group_array(ro.player_username)
                    FROM recipe_owners ro
                    JOIN recipes rc ON ro.recipe_id = rc.id
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as owners
            FROM gear g
            WHERE g.id = ?
        """
        res = await self.execute_query(query, (gear_id,))
        if not res:
            return None
        row = res[0]
        row["mobs"] = json.loads(row["mobs"] or "[]")
        row["scroll_mobs"] = json.loads(row["scroll_mobs"] or "[]")
        row["ingredients"] = json.loads(row["ingredients"] or "[]")
        row["owners"] = json.loads(row["owners"] or "[]")
        row["craftable"] = row["recipe_id"] is not None
        return row

    async def get_prev_next_gear_by_slot(self, gear_id: int, rarity: str) -> Dict[str, Optional[int]]:
        case_expression = "CASE slot "
        for slot, order in self.SLOT_ORDER.items():
            case_expression += f" WHEN '{slot}' THEN {order}"
        case_expression += " ELSE 99 END"
        
        rows = await self.execute_query(
            f"""
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY {case_expression}, name COLLATE NOCASE, id) AS prev_id,
                       LEAD(id) OVER (ORDER BY {case_expression}, name COLLATE NOCASE, id) AS next_id
                FROM gear
                WHERE rarity = ?
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (rarity, gear_id),
        )
        return rows[0] if rows else {'prev_id': None, 'next_id': None}

    async def get_all_gear_simple(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM gear ORDER BY id")

    # ========== РЕЦЕПТЫ ==========
    async def get_all_recipes(self, result_type: str, offset: int, limit: int) -> List[Dict]:
        query = f"""
            SELECT r.id, r.result_type, r.result_id,
                   CASE WHEN r.result_type='gear' THEN g.name ELSE res.name END as result_name,
                   CASE WHEN r.result_type='gear' THEN g.emoji ELSE res.emoji END as result_emoji,
                   (SELECT COUNT(*) FROM recipe_owners WHERE recipe_id=r.id) as owner_count,
                   (SELECT COUNT(*) FROM recipe_ingredients WHERE recipe_id=r.id) as ingredient_count
            FROM recipes r
            LEFT JOIN gear g ON r.result_type='gear' AND r.result_id=g.id
            LEFT JOIN resources res ON r.result_type='resource' AND r.result_id=res.id
            WHERE r.result_type=?
            ORDER BY r.id LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (result_type, limit, offset))

    async def get_recipe_id_by_gear(self, gear_id: int) -> Optional[int]:
        """Возвращает ID рецепта для указанного снаряжения (если есть)."""
        res = await self.execute_query(
            "SELECT id FROM recipes WHERE result_type = 'gear' AND result_id = ?",
            (gear_id,)
        )
        return res[0]['id'] if res else None

    async def get_recipe_owners(self, recipe_id: int) -> List[str]:
        """Возвращает список username владельцев рецепта."""
        rows = await self.execute_query(
            "SELECT player_username FROM recipe_owners WHERE recipe_id = ?",
            (recipe_id,)
        )
        return [row['player_username'] for row in rows]

    async def get_recipe_details(self, recipe_id: int) -> Optional[Dict]:
        recipe = await self.execute_query("SELECT * FROM recipes WHERE id=?", (recipe_id,))
        if not recipe:
            return None
        recipe = recipe[0]
        ingredients = await self.execute_query(
            "SELECT ri.resource_id, r.name, r.emoji, ri.quantity FROM recipe_ingredients ri "
            "JOIN resources r ON ri.resource_id = r.id WHERE ri.recipe_id=? ORDER BY ri.resource_id",
            (recipe_id,)
        )
        owners = await self.execute_query(
            "SELECT player_username FROM recipe_owners WHERE recipe_id=?",
            (recipe_id,)
        )
        owners = [o['player_username'] for o in owners]
        return {
            'id': recipe['id'],
            'result_type': recipe['result_type'],
            'result_id': recipe['result_id'],
            'quantity': recipe['quantity'],
            'ingredients': ingredients,
            'owners': owners
        }

    async def create_recipe(self, result_type: str, result_id: int, quantity: int = 1) -> int:
        return await self.execute_insert(
            "INSERT INTO recipes (result_type, result_id, quantity) VALUES (?, ?, ?)",
            (result_type, result_id, quantity)
        )

    async def delete_recipe(self, recipe_id: int):
        async with self.transaction():
            await self.execute_query("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
            await self.execute_query("DELETE FROM recipe_owners WHERE recipe_id=?", (recipe_id,))
            await self.execute_query("DELETE FROM recipes WHERE id=?", (recipe_id,))

    async def add_ingredient(self, recipe_id: int, resource_id: int, quantity: int):
        await self.execute_query(
            "INSERT INTO recipe_ingredients (recipe_id, resource_id, quantity) VALUES (?, ?, ?)",
            (recipe_id, resource_id, quantity)
        )

    async def update_ingredient(self, recipe_id: int, resource_id: int, quantity: int):
        await self.execute_query(
            "UPDATE recipe_ingredients SET quantity=? WHERE recipe_id=? AND resource_id=?",
            (quantity, recipe_id, resource_id)
        )

    async def remove_ingredient(self, recipe_id: int, resource_id: int):
        await self.execute_query(
            "DELETE FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?",
            (recipe_id, resource_id)
        )

    async def add_recipe_owner(self, recipe_id: int, player_username: str):
        await self.execute_query(
            "INSERT OR IGNORE INTO recipe_owners (recipe_id, player_username) VALUES (?, ?)",
            (recipe_id, player_username)
        )

    async def remove_recipe_owner(self, recipe_id: int, player_username: str):
        await self.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id=? AND player_username=?",
            (recipe_id, player_username)
        )

    async def get_recipe_for_resource(self, resource_id: int) -> Optional[Dict]:
        recipe_info = await self.execute_query(
            "SELECT id FROM recipes WHERE result_type = 'resource' AND result_id = ?",
            (resource_id,)
        )
        if not recipe_info:
            return None
        recipe_id = recipe_info[0]['id']
        ingredients = await self.execute_query(
            "SELECT ri.resource_id, r.name, r.emoji, ri.quantity "
            "FROM recipe_ingredients ri JOIN resources r ON ri.resource_id = r.id "
            "WHERE ri.recipe_id = ? ORDER BY ri.resource_id",
            (recipe_id,)
        )
        return {'ingredients': ingredients}

    # ========== КАРТЫ ==========
    async def get_cards_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, slot FROM cards ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_all_cards_sorted_by_slot(self, offset: int, limit: int) -> List[Dict]:
        case_expression = "CASE slot "
        for slot, order in self.SLOT_ORDER.items():
            case_expression += f" WHEN '{slot}' THEN {order}"
        case_expression += " ELSE 99 END"
        
        query = f"""
            SELECT id, name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note
            FROM cards
            ORDER BY {case_expression}, name COLLATE NOCASE
            LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (limit, offset))

    async def get_card_by_id(self, card_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT * FROM cards WHERE id = ?", (card_id,))
        return res[0] if res else None

    async def add_card(self, name: str, emoji: str, slot: str,
                       bonus1: str = '', bonus2: str = '', bonus3: str = '', bonus4: str = '',
                       note: str = '') -> int:
        return await self.execute_insert(
            """INSERT INTO cards (name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
        )

    async def update_card(self, card_id: int, **kwargs):
        allowed = {'name', 'emoji', 'slot', 'bonus1', 'bonus2', 'bonus3', 'bonus4', 'note'}
        updates = [(field, value) for field, value in kwargs.items() if field in allowed]
        if not updates:
            return
        assignments = ", ".join(f"{field} = ?" for field, _ in updates)
        params = tuple(value for _, value in updates) + (card_id,)
        await self.execute_query(f"UPDATE cards SET {assignments} WHERE id = ?", params)

    async def delete_card(self, card_id: int):
        async with self.transaction():
            await self.execute_query("DELETE FROM drops WHERE item_type='card' AND item_id=?", (card_id,))
            await self._delete_recipes_by_result('card', card_id)
            await self.execute_query("DELETE FROM cards WHERE id=?", (card_id,))

    async def get_card_drop_mobs(self, card_id: int) -> List[Dict]:
        return await self.execute_query(
            """SELECT m.id, m.name, m.emoji, l.name as location_name, l.emoji as location_emoji
               FROM drops d
               JOIN mobs m ON d.mob_id = m.id
               JOIN locations l ON m.location_id = l.id
               WHERE d.item_type='card' AND d.item_id=?
               ORDER BY m.id""",
            (card_id,)
        )

    async def get_prev_next_card_by_slot(self, card_id: int) -> Dict[str, Optional[int]]:
        case_expression = "CASE slot "
        for slot, order in self.SLOT_ORDER.items():
            case_expression += f" WHEN '{slot}' THEN {order}"
        case_expression += " ELSE 99 END"
        
        rows = await self.execute_query(
            f"""
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY {case_expression}, name COLLATE NOCASE, id) AS prev_id,
                       LEAD(id) OVER (ORDER BY {case_expression}, name COLLATE NOCASE, id) AS next_id
                FROM cards
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (card_id,),
        )
        return rows[0] if rows else {'prev_id': None, 'next_id': None}

    # ========== ДРОПЫ ==========
    async def search_drop_items(self, mob_id: int, query: str, limit: int = 20) -> List[Dict]:
        limit = max(1, min(limit, 50))
        sql = """
            WITH matching AS (
                SELECT
                    'resource' AS item_type,
                    r.id,
                    r.name,
                    r.emoji,
                    NULL AS rarity,
                    EXISTS (
                        SELECT 1 FROM drops d
                        WHERE d.mob_id = ?
                          AND d.item_type = 'resource'
                          AND d.item_id = r.id
                    ) AS enabled
                FROM resources r
                WHERE INSTR(LOWER_UNICODE(r.name), LOWER_UNICODE(?)) > 0

                UNION ALL

                SELECT
                    'gear' AS item_type,
                    g.id,
                    g.name,
                    g.emoji,
                    g.rarity,
                    EXISTS (
                        SELECT 1 FROM drops d
                        WHERE d.mob_id = ?
                          AND d.item_type = 'gear'
                          AND d.item_id = g.id
                    ) AS enabled
                FROM gear g
                WHERE INSTR(LOWER_UNICODE(g.name), LOWER_UNICODE(?)) > 0

                UNION ALL

                SELECT
                    'card' AS item_type,
                    c.id,
                    c.name,
                    c.emoji,
                    NULL AS rarity,
                    EXISTS (
                        SELECT 1 FROM drops d
                        WHERE d.mob_id = ?
                          AND d.item_type = 'card'
                          AND d.item_id = c.id
                    ) AS enabled
                FROM cards c
                WHERE INSTR(LOWER_UNICODE(c.name), LOWER_UNICODE(?)) > 0
            )
            SELECT item_type, id, name, emoji, rarity, enabled
            FROM matching
            ORDER BY LOWER_UNICODE(name), item_type, id
            LIMIT ?
        """
        return await self.execute_query(
            sql,
            (mob_id, query, mob_id, query, mob_id, query, limit),
        )

    async def get_drop_status(self, mob_id: int, item_type: str, item_id: int) -> bool:
        res = await self.execute_query(
            "SELECT 1 FROM drops WHERE mob_id = ? AND item_type = ? AND item_id = ?",
            (mob_id, item_type, item_id)
        )
        return len(res) > 0

    async def add_drop(self, mob_id: int, item_type: str, item_id: int):
        try:
            async with self.transaction():
                if item_type == 'resource':
                    check = await self.execute_query("SELECT 1 FROM resources WHERE id = ?", (item_id,))
                elif item_type == 'gear':
                    check = await self.execute_query("SELECT 1 FROM gear WHERE id = ?", (item_id,))
                elif item_type == 'card':
                    check = await self.execute_query("SELECT 1 FROM cards WHERE id = ?", (item_id,))
                else:
                    raise ValueError(f"Unknown item_type: {item_type}")

                mob = await self.execute_query("SELECT 1 FROM mobs WHERE id = ?", (mob_id,))
                if not mob:
                    raise ValueError(f"mob with id {mob_id} does not exist")
                if not check:
                    raise ValueError(f"{item_type} with id {item_id} does not exist")

                await self.execute_query(
                    "INSERT OR IGNORE INTO drops (mob_id, item_type, item_id) VALUES (?, ?, ?)",
                    (mob_id, item_type, item_id)
                )
        except Exception as e:
            logger.error(f"Failed to add drop: {e}")
            raise

    async def remove_drop(self, mob_id: int, item_type: str, item_id: int):
        await self.execute_query(
            "DELETE FROM drops WHERE mob_id = ? AND item_type = ? AND item_id = ?",
            (mob_id, item_type, item_id)
        )

    async def get_resources_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, type FROM resources "
            "ORDER BY LOWER_UNICODE(name), id LIMIT ? OFFSET ?",
            (limit, offset)
        )

db = Database()
