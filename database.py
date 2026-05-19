import os
import asyncio
import logging
import aiosqlite
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Путь к БД читается из переменной окружения DATABASE_PATH,
# значение по умолчанию — "game.db"
DB_PATH = os.getenv("DATABASE_PATH", "game.db")

# --------------------------------------------------------------
# Функция для приведения строки к нижнему регистру (Юникод)
# --------------------------------------------------------------
def _lower_unicode(s: str) -> str:
    if s is None:
        return None
    return s.lower()

# --------------------------------------------------------------
# Класс для управления подключением к БД
# --------------------------------------------------------------
class Database:
    def __init__(self):
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self):
        """Инициализирует соединение, устанавливает row_factory и регистрирует SQL-функцию."""
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
        """Безопасно преобразует aiosqlite.Row в dict."""
        return {key: row[key] for key in row.keys()}

    async def execute_query(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        """
        Выполняет SQL-запрос.
        Для запросов, отличных от SELECT, автоматически выполняет commit.
        Возвращает список словарей.
        """
        async with self._lock:
            async with self._conn.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                # Если запрос не SELECT — фиксируем транзакцию
                if not query.strip().upper().startswith("SELECT"):
                    await self._conn.commit()
                if not rows:
                    return []
                # Если row_factory не сработал (кортежи) — используем описание курсора
                if not hasattr(rows[0], 'keys'):
                    col_names = [desc[0] for desc in cursor.description]
                    return [dict(zip(col_names, row)) for row in rows]
                return [self._row_to_dict(row) for row in rows]

    # --------------------------------------------------------------
    # ПОИСК (регистронезависимый)
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
    # Локации
    # --------------------------------------------------------------
    async def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT id, name, emoji FROM locations WHERE id = ?", (location_id,))
        return res[0] if res else None

    async def get_locations(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM locations ORDER BY id")

    # --------------------------------------------------------------
    # Мобы
    # --------------------------------------------------------------
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

    # --------------------------------------------------------------
    # Ресурсы
    # --------------------------------------------------------------
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

    # --------------------------------------------------------------
    # Снаряжение
    # --------------------------------------------------------------
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

    # --------------------------------------------------------------
    # Рецепты (крафт)
    # --------------------------------------------------------------
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

# Глобальный экземпляр БД
db = Database()
