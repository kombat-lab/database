import os
import asyncio
import logging
import aiosqlite
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "game.db")

def _lower_unicode(s: str) -> str:
    if s is None:
        return None
    return s.lower()

class Database:
    def __init__(self):
        self._conn: Optional[aiosqlite.Connection] = None
        self._locations_cache: Dict[int, Dict] = {}

    async def connect(self):
        self._conn = await aiosqlite.connect(DB_PATH, timeout=30.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.create_function("LOWER_UNICODE", 1, _lower_unicode)
        await self._ensure_indexes()
        await self._load_locations_cache()
        logger.info(f"Database connected: {DB_PATH}")

    async def _ensure_indexes(self):
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_mobs_location ON mobs(location_id)",
            "CREATE INDEX IF NOT EXISTS idx_mobs_name ON mobs(name)",
            "CREATE INDEX IF NOT EXISTS idx_resources_name ON resources(name)",
            "CREATE INDEX IF NOT EXISTS idx_gear_name ON gear(name)",
            "CREATE INDEX IF NOT EXISTS idx_mob_drops_mob ON mob_drops(mob_id)",
            "CREATE INDEX IF NOT EXISTS idx_mob_drops_resource ON mob_drops(resource_id)",
            "CREATE INDEX IF NOT EXISTS idx_gear_drops_mob ON gear_drops(mob_id)",
            "CREATE INDEX IF NOT EXISTS idx_gear_drops_gear ON gear_drops(gear_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_result ON recipes(result_type, result_id)",
        ]
        for sql in indexes:
            await self._conn.execute(sql)
        await self._conn.commit()

    async def _load_locations_cache(self):
        locations = await self.execute_query("SELECT id, name, emoji FROM locations")
        self._locations_cache = {loc["id"]: dict(loc) for loc in locations}

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        async with self._conn.execute(query, params) as cursor:
            try:
                rows = await cursor.fetchall()
                if not query.strip().upper().startswith("SELECT"):
                    await self._conn.commit()
                if not rows:
                    return []
                if not hasattr(rows[0], 'keys'):
                    col_names = [desc[0] for desc in cursor.description]
                    return [dict(zip(col_names, row)) for row in rows]
                return [dict(row) for row in rows]
            except Exception as e:
                await self._conn.rollback()
                raise e

    # ---------- Кэшированные локации ----------
    async def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        return self._locations_cache.get(location_id)

    async def get_locations(self) -> List[Dict]:
        return list(self._locations_cache.values())

    def invalidate_location_cache(self):
        asyncio.create_task(self._load_locations_cache())

    # ---------- Мобы ----------
    async def get_mob_full_card(self, mob_id: int) -> Optional[Dict]:
        query = """
            SELECT
                m.id, m.name, m.emoji, m.hp, m.dust_min, m.dust_max, m.exp, m.location_id,
                l.name as loc_name, l.emoji as loc_emoji,
                (SELECT GROUP_CONCAT(r.id || '|' || r.name || '|' || r.emoji)
                 FROM mob_drops md JOIN resources r ON md.resource_id = r.id
                 WHERE md.mob_id = m.id) as resource_drops,
                (SELECT GROUP_CONCAT(g.id || '|' || g.name || '|' || g.emoji || '|' || g.slot)
                 FROM gear_drops gd JOIN gear g ON gd.gear_id = g.id
                 WHERE gd.mob_id = m.id) as gear_drops
            FROM mobs m
            JOIN locations l ON m.location_id = l.id
            WHERE m.id = ?
        """
        res = await self.execute_query(query, (mob_id,))
        if not res:
            return None
        row = res[0]
        row["resource_drops"] = [self._parse_drop_item(s) for s in (row["resource_drops"].split(",") if row["resource_drops"] else [])]
        row["gear_drops"] = [self._parse_drop_item(s, gear=True) for s in (row["gear_drops"].split(",") if row["gear_drops"] else [])]
        return row

    async def get_mobs_by_location(self, location_id: int, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs "
            "WHERE location_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (location_id, limit, offset)
        )

    # ---------- Ресурсы ----------
    async def get_resource_card(self, resource_id: int) -> Optional[Dict]:
        query = """
            SELECT r.id, r.name, r.emoji,
                   GROUP_CONCAT(m.id || '|' || m.name || '|' || m.emoji) as mobs
            FROM resources r
            LEFT JOIN mob_drops md ON r.id = md.resource_id
            LEFT JOIN mobs m ON md.mob_id = m.id
            WHERE r.id = ?
            GROUP BY r.id
        """
        res = await self.execute_query(query, (resource_id,))
        if not res:
            return None
        row = res[0]
        if row["mobs"]:
            row["mobs"] = [self._parse_drop_item(s) for s in row["mobs"].split(",")]
        else:
            row["mobs"] = []
        return row

    async def get_resources_by_location(self, location_id: int, offset: int, limit: int) -> List[Dict]:
        query = """
            SELECT DISTINCT r.id, r.name, r.emoji
            FROM resources r
            JOIN mob_drops md ON r.id = md.resource_id
            JOIN mobs m ON md.mob_id = m.id
            WHERE m.location_id = ?
            ORDER BY r.id LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (location_id, limit, offset))

    # ---------- Снаряжение ----------
    async def get_gear_card(self, gear_id: int) -> Optional[Dict]:
        query = """
            SELECT g.id, g.name, g.rarity, g.slot, g.craftable, g.craft_dust, g.emoji, g.scroll_resource_id,
                   (SELECT GROUP_CONCAT(m.id || '|' || m.name || '|' || m.emoji)
                    FROM gear_drops gd JOIN mobs m ON gd.mob_id = m.id
                    WHERE gd.gear_id = g.id) as gear_drops_mobs,
                   (SELECT GROUP_CONCAT(ri.resource_id || '|' || r.name || '|' || r.emoji || '|' || ri.quantity)
                    FROM recipes rc
                    JOIN recipe_ingredients ri ON rc.id = ri.recipe_id
                    JOIN resources r ON ri.resource_id = r.id
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as ingredients,
                   (SELECT GROUP_CONCAT(player_username) FROM recipe_owners ro
                    WHERE ro.recipe_id = (SELECT id FROM recipes WHERE result_type='gear' AND result_id=g.id)) as owners
            FROM gear g
            WHERE g.id = ?
        """
        res = await self.execute_query(query, (gear_id,))
        if not res:
            return None
        row = res[0]
        if row['rarity'] == 'epic' and row.get('scroll_resource_id'):
            mobs_data = await self.execute_query(
                "SELECT m.id, m.name, m.emoji FROM mob_drops md "
                "JOIN mobs m ON md.mob_id = m.id WHERE md.resource_id = ? ORDER BY m.id",
                (row['scroll_resource_id'],)
            )
            row["mobs"] = mobs_data
        else:
            mobs_str = row['gear_drops_mobs']
            row["mobs"] = [self._parse_drop_item(s) for s in (mobs_str.split(",") if mobs_str else [])]
        row["ingredients"] = [self._parse_ingredient(s) for s in (row["ingredients"].split(",") if row["ingredients"] else [])]
        row["owners"] = row["owners"].split(",") if row["owners"] else []
        if 'gear_drops_mobs' in row:
            del row['gear_drops_mobs']
        if 'scroll_resource_id' in row:
            del row['scroll_resource_id']
        return row

    async def get_gear_by_rarity(self, rarity: str, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji, craftable, craft_dust "
            "FROM gear WHERE rarity = ? ORDER BY id LIMIT ? OFFSET ?",
            (rarity, limit, offset)
        )

    # ---------- Поиск ----------
    async def search(self, query: str) -> Dict[str, List[Dict]]:
        search_pattern = f"%{query}%"
        mobs = await self.execute_query(
            "SELECT m.id, m.name, m.emoji, m.hp, m.dust_min, m.dust_max, m.exp, "
            "l.id as location_id, l.name as location_name, l.emoji as location_emoji "
            "FROM mobs m JOIN locations l ON m.location_id = l.id "
            "WHERE LOWER_UNICODE(m.name) LIKE LOWER_UNICODE(?) ORDER BY m.id LIMIT 50",
            (search_pattern,)
        )
        resources = await self.execute_query(
            "SELECT id, name, emoji FROM resources "
            "WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ORDER BY id LIMIT 50",
            (search_pattern,)
        )
        gear = await self.execute_query(
            "SELECT id, name, rarity, slot, emoji FROM gear "
            "WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ORDER BY id LIMIT 50",
            (search_pattern,)
        )
        return {"mobs": mobs, "resources": resources, "gear": gear}

    # ---------- Крафт ресурсов ----------
    async def get_craftable_resources(self) -> List[Dict]:
        query = """
            SELECT DISTINCT r.id, r.name, r.emoji
            FROM resources r
            JOIN recipes rc ON rc.result_type = 'resource' AND rc.result_id = r.id
            ORDER BY r.id
        """
        return await self.execute_query(query)

    async def get_recipe_for_resource(self, resource_id: int) -> Optional[Dict]:
        recipe_info = await self.execute_query(
            "SELECT id, craft_dust FROM recipes WHERE result_type = 'resource' AND result_id = ?",
            (resource_id,)
        )
        if not recipe_info:
            return None
        recipe = recipe_info[0]
        ingredients = await self.execute_query(
            """
            SELECT ri.resource_id, r.name, r.emoji, ri.quantity
            FROM recipe_ingredients ri
            JOIN resources r ON ri.resource_id = r.id
            WHERE ri.recipe_id = ?
            ORDER BY ri.resource_id
            """,
            (recipe['id'],)
        )
        return {
            'craft_dust': recipe['craft_dust'],
            'ingredients': ingredients
        }

    # ---------- Парсеры ----------
    @staticmethod
    def _parse_drop_item(s: str, gear: bool = False) -> Dict:
        parts = s.split("|")
        if gear:
            return {"id": int(parts[0]), "name": parts[1], "emoji": parts[2], "slot": parts[3]}
        else:
            return {"id": int(parts[0]), "name": parts[1], "emoji": parts[2]}

    @staticmethod
    def _parse_ingredient(s: str) -> Dict:
        parts = s.split("|")
        return {"id": int(parts[0]), "name": parts[1], "emoji": parts[2], "quantity": int(parts[3])}

    # ---------- Пагинация для админки ----------
    async def get_resources_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji FROM resources ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_common_gear_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, slot FROM gear WHERE rarity = 'common' ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_recipes_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji FROM recipes ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_total_resources_count(self) -> int:
        res = await self.execute_query("SELECT COUNT(*) as cnt FROM resources")
        return res[0]["cnt"]

    # ---------- Управление мобами ----------
    async def update_mob_field(self, mob_id: int, field: str, value):
        query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
        await self.execute_query(query, (value, mob_id))

    async def delete_mob(self, mob_id: int):
        await self.execute_query("DELETE FROM mob_drops WHERE mob_id = ?", (mob_id,))
        await self.execute_query("DELETE FROM gear_drops WHERE mob_id = ?", (mob_id,))
        await self.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))

    # ---------- Дроп ----------
    async def get_mob_drop_status(self, mob_id: int, category: str, item_id: int) -> bool:
        if category == 'resource':
            res = await self.execute_query(
                "SELECT 1 FROM mob_drops WHERE mob_id = ? AND resource_id = ? LIMIT 1",
                (mob_id, item_id)
            )
        elif category == 'gear':
            res = await self.execute_query(
                "SELECT 1 FROM gear_drops WHERE mob_id = ? AND gear_id = ? LIMIT 1",
                (mob_id, item_id)
            )
        elif category == 'recipe':
            res = await self.execute_query(
                "SELECT 1 FROM recipe_drops WHERE mob_id = ? AND recipe_id = ? LIMIT 1",
                (mob_id, item_id)
            )
        else:
            return False
        return len(res) > 0

    async def add_drop(self, mob_id: int, category: str, item_id: int):
        if category == 'resource':
            await self.execute_query(
                "INSERT OR IGNORE INTO mob_drops (mob_id, resource_id) VALUES (?, ?)",
                (mob_id, item_id)
            )
        elif category == 'gear':
            await self.execute_query(
                "INSERT OR IGNORE INTO gear_drops (mob_id, gear_id) VALUES (?, ?)",
                (mob_id, item_id)
            )
        elif category == 'recipe':
            await self.execute_query(
                "INSERT OR IGNORE INTO recipe_drops (mob_id, recipe_id) VALUES (?, ?)",
                (mob_id, item_id)
            )

    async def remove_drop(self, mob_id: int, category: str, item_id: int):
        if category == 'resource':
            await self.execute_query(
                "DELETE FROM mob_drops WHERE mob_id = ? AND resource_id = ?",
                (mob_id, item_id)
            )
        elif category == 'gear':
            await self.execute_query(
                "DELETE FROM gear_drops WHERE mob_id = ? AND gear_id = ?",
                (mob_id, item_id)
            )
        elif category == 'recipe':
            await self.execute_query(
                "DELETE FROM recipe_drops WHERE mob_id = ? AND recipe_id = ?",
                (mob_id, item_id)
            )

    # ---------- Совместимость со старым кодом ----------
    async def get_all_resources(self):
        return await self.execute_query("SELECT id, name, emoji FROM resources")

    async def get_all_common_gear(self):
        return await self.execute_query(
            "SELECT id, name, emoji, slot FROM gear WHERE rarity = 'common'"
        )

    async def get_all_recipes(self):
        return await self.execute_query("SELECT id, name, emoji FROM recipes")

    async def get_resource_by_id(self, resource_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT id, name, emoji FROM resources WHERE id = ?", (resource_id,))
        return res[0] if res else None

    async def add_resource(self, name: str, emoji: str) -> int:
        await self.execute_query("INSERT INTO resources (name, emoji) VALUES (?, ?)", (name, emoji))
        res = await self.execute_query("SELECT last_insert_rowid() as id")
        return res[0]['id']

    async def update_resource(self, resource_id: int, name: str, emoji: str):
        await self.execute_query("UPDATE resources SET name = ?, emoji = ? WHERE id = ?", (name, emoji, resource_id))

    async def delete_resource(self, resource_id: int):
        await self.execute_query("DELETE FROM mob_drops WHERE resource_id = ?", (resource_id,))
        await self.execute_query("DELETE FROM resources WHERE id = ?", (resource_id,))

    async def get_gear_info(self, gear_id: int) -> Optional[Dict]:
        res = await self.execute_query(
            "SELECT id, name, rarity, slot, craftable, craft_dust, emoji FROM gear WHERE id = ?",
            (gear_id,)
        )
        return res[0] if res else None

    async def get_gear_mobs(self, gear_id: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT m.id, m.name, m.emoji FROM gear_drops gd "
            "JOIN mobs m ON gd.mob_id = m.id WHERE gd.gear_id = ? ORDER BY m.id",
            (gear_id,)
        )

    async def get_recipe_for_gear(self, gear_id: int) -> List[Dict]:
        query = """
            SELECT ri.resource_id, r.name, r.emoji, ri.quantity
            FROM recipes rc
            JOIN recipe_ingredients ri ON rc.id = ri.recipe_id
            JOIN resources r ON ri.resource_id = r.id
            WHERE rc.result_type = 'gear' AND rc.result_id = ?
            ORDER BY ri.resource_id
        """
        return await self.execute_query(query, (gear_id,))

    async def get_recipe_owners(self, gear_id: int) -> List[str]:
        query = """
            SELECT player_username
            FROM recipes rc
            JOIN recipe_owners ro ON rc.id = ro.recipe_id
            WHERE rc.result_type = 'gear' AND rc.result_id = ?
        """
        owners = await self.execute_query(query, (gear_id,))
        return [owner['player_username'] for owner in owners]

db = Database()
