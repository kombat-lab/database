import os
import asyncio
import logging
import aiosqlite
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "game.db")

# --------------------------------------------------------------
# Функция для приведения строки к нижнему регистру (Юникод)
# --------------------------------------------------------------
def _lower_unicode(s: str) -> str:
    if s is None:
        return None
    return s.lower()

# --------------------------------------------------------------
# Класс для управления подключением к БД (одно соединение + блокировка)
# --------------------------------------------------------------
class Database:
    def __init__(self):
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """Инициализирует соединение и регистрирует SQL-функцию."""
        self._conn = await aiosqlite.connect(DB_PATH)
        await self._conn.create_function("LOWER_UNICODE", 1, _lower_unicode)
        # Включаем поддержку внешних ключей (опционально)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        logger.info("Database connected")

    async def close(self):
        if self._conn:
            await self._conn.close()
            logger.info("Database closed")

    async def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """Выполняет запрос и возвращает список словарей."""
        async with self._lock:
            async with self._conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                # Преобразуем aiosqlite.Row в dict
                return [dict(row) for row in rows]

    # --------------------------------------------------------------
    # ПОИСК (регистронезависимый для кириллицы)
    # --------------------------------------------------------------
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

    # --------------------------------------------------------------
    # Основные функции
    # --------------------------------------------------------------
    async def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT id, name, emoji FROM locations WHERE id = ?", (location_id,))
        return res[0] if res else None

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

    async def get_locations(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM locations ORDER BY id")

    async def get_recipe_for_gear(self, gear_id: int) -> List[Dict]:
        """
        Возвращает список ингредиентов для крафта снаряжения.
        Каждый элемент: {'resource_id': int, 'name': str, 'emoji': str, 'quantity': int}
        """
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
        """
        Возвращает список Telegram username владельцев рецепта для данного снаряжения.
        """
        query = """
            SELECT player_username
            FROM recipes rc
            JOIN recipe_owners ro ON rc.id = ro.recipe_id
            WHERE rc.result_type = 'gear' AND rc.result_id = ?
        """
        owners = await self.execute_query(query, (gear_id,))
        return [owner['player_username'] for owner in owners]

# Глобальный экземпляр БД
db = Database()
