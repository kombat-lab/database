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
        self._lock = asyncio.Lock()

    async def connect(self):
        self._conn = await aiosqlite.connect(DB_PATH)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.create_function("LOWER_UNICODE", 1, _lower_unicode)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        logger.info(f"Database connected: {DB_PATH}")

    async def close(self):
        if self._conn:
            await self._conn.close()
            logger.info("Database closed")

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> Dict[str, Any]:
        return {key: row[key] for key in row.keys()}

    async def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        async with self._lock:
            async with self._conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                if not query.strip().upper().startswith("SELECT"):
                    await self._conn.commit()
                if not rows:
                    return []
                if not hasattr(rows[0], 'keys'):
                    col_names = [desc[0] for desc in cursor.description]
                    return [dict(zip(col_names, row)) for row in rows]
                return [self._row_to_dict(row) for row in rows]

    # ---------- Поиск ----------
    async def search(self, query: str) -> Dict[str, List[Dict]]:
        search_pattern = f"%{query}%"
        mobs = await self.execute_query(
            "SELECT id, name, emoji, hp, dust_min, dust_max, exp, location_id FROM mobs "
            "WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ORDER BY id",
            (search_pattern,)
        )
        resources = await self.execute_query(
            "SELECT id, name, emoji FROM resources WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ORDER BY id",
            (search_pattern,)
        )
        gear = await self.execute_query(
            "SELECT id, name, rarity, slot, emoji FROM gear WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ORDER BY id",
            (search_pattern,)
        )
        return {"mobs": mobs, "resources": resources, "gear": gear}

    # ---------- Локации ----------
    async def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT id, name, emoji FROM locations WHERE id = ?", (location_id,))
        return res[0] if res else None

    async def get_locations(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM locations ORDER BY id")

    # ---------- Мобы ----------
    async def get_mobs_by_location(self, location_id: int, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs "
            "WHERE location_id = ? ORDER BY id LIMIT ? OFFSET ?",
            (location_id, limit, offset)
        )

    async def get_mob_drops(self, mob_id: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT r.id, r.name, r.emoji FROM mob_drops md "
            "JOIN resources r ON md.resource_id = r.id WHERE md.mob_id = ? ORDER BY r.id",
            (mob_id,)
        )

    async def get_mob_gear_drops(self, mob_id: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT g.id, g.name, g.rarity, g.slot, g.emoji FROM gear_drops gd "
            "JOIN gear g ON gd.gear_id = g.id WHERE gd.mob_id = ? AND g.rarity = 'common' ORDER BY g.id",
            (mob_id,)
        )

    # ---------- Ресурсы (CRUD + получение) ----------
    async def get_all_resources(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM resources ORDER BY name")

    async def get_resource_by_id(self, resource_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT id, name, emoji FROM resources WHERE id = ?", (resource_id,))
        return res[0] if res else None

    async def add_resource(self, name: str, emoji: str) -> int:
        await self.execute_query("INSERT INTO resources (name, emoji) VALUES (?, ?)", (name, emoji))
        res = await self.execute_query("SELECT last_insert_rowid() as id")
        return res[0]['id']

    async def update_resource(self, resource_id: int, name: str, emoji: str) -> None:
        await self.execute_query("UPDATE resources SET name = ?, emoji = ? WHERE id = ?", (name, emoji, resource_id))

    async def delete_resource(self, resource_id: int) -> None:
        await self.execute_query("DELETE FROM mob_drops WHERE resource_id = ?", (resource_id,))
        await self.execute_query("DELETE FROM resources WHERE id = ?", (resource_id,))

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

    async def get_resource_info(self, resource_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT id, name, emoji FROM resources WHERE id = ?", (resource_id,))
        return res[0] if res else None

    async def get_resource_mobs(self, resource_id: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT m.id, m.name, m.emoji FROM mob_drops md "
            "JOIN mobs m ON md.mob_id = m.id WHERE md.resource_id = ? ORDER BY m.id",
            (resource_id,)
        )

    # ---------- Снаряжение ----------
    async def get_all_common_gear(self) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, slot FROM gear WHERE rarity = 'common' ORDER BY name"
        )

    async def get_gear_by_rarity(self, rarity: str, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji, craftable, craft_dust "
            "FROM gear WHERE rarity = ? ORDER BY id LIMIT ? OFFSET ?",
            (rarity, limit, offset)
        )

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

    # ---------- Рецепты (как ресурсы) ----------
    async def get_all_recipes(self) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji FROM resources WHERE id >= 59 ORDER BY name"
        )

    # ---------- Крафт ----------
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

    # ---------- Управление дропами (общее) ----------
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
                "SELECT 1 FROM mob_drops WHERE mob_id = ? AND resource_id = ? LIMIT 1",
                (mob_id, item_id)
            )
        else:
            return False
        return len(res) > 0

    async def add_drop(self, mob_id: int, category: str, item_id: int) -> None:
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
                "INSERT OR IGNORE INTO mob_drops (mob_id, resource_id) VALUES (?, ?)",
                (mob_id, item_id)
            )

    async def remove_drop(self, mob_id: int, category: str, item_id: int) -> None:
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
                "DELETE FROM mob_drops WHERE mob_id = ? AND resource_id = ?",
                (mob_id, item_id)
            )

    # ---------- Карты (заглушка) ----------
    async def get_all_maps(self) -> List[Dict]:
        return []

db = Database()
