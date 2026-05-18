import os
import sqlite3
from typing import List, Dict, Any, Optional

DB_PATH = os.getenv("DATABASE_PATH", "game.db")

# --------------------------------------------------------------
# Функция для приведения строки к нижнему регистру (Юникод)
# --------------------------------------------------------------
def lower_unicode(s: str) -> str:
    """Возвращает строку в нижнем регистре. Работает с любой Юникод-строкой (кириллицей)."""
    if s is None:
        return None
    return s.lower()

# --------------------------------------------------------------
# Подключение с регистрацией пользовательской SQL-функции
# --------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.create_function("LOWER_UNICODE", 1, lower_unicode)
    return conn

def execute_query(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Выполняет запрос и возвращает список словарей."""
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

# --------------------------------------------------------------
# ПОИСК (регистронезависимый для кириллицы)
# --------------------------------------------------------------
def search(query: str) -> Dict[str, List[Dict]]:
    search_pattern = f"%{query}%"
    
    mobs = execute_query(
        "SELECT id, name, emoji, hp, dust_min, dust_max, exp, location_id FROM mobs WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?)",
        (search_pattern,)
    )
    resources = execute_query(
        "SELECT id, name, emoji FROM resources WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?)",
        (search_pattern,)
    )
    gear = execute_query(
        "SELECT id, name, rarity, slot, emoji FROM gear WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?)",
        (search_pattern,)
    )
    return {"mobs": mobs, "resources": resources, "gear": gear}

# --------------------------------------------------------------
# Основные функции
# --------------------------------------------------------------
def get_location_by_id(location_id: int) -> Optional[Dict]:
    res = execute_query("SELECT id, name, emoji FROM locations WHERE id = ?", (location_id,))
    return res[0] if res else None

def get_mobs_by_location(location_id: int, offset: int, limit: int) -> List[Dict]:
    return execute_query(
        "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs WHERE location_id = ? LIMIT ? OFFSET ?",
        (location_id, limit, offset)
    )

def get_mob_drops(mob_id: int) -> List[Dict]:
    return execute_query(
        "SELECT r.id, r.name, r.emoji FROM mob_drops md JOIN resources r ON md.resource_id = r.id WHERE md.mob_id = ?",
        (mob_id,)
    )

def get_mob_gear_drops(mob_id: int) -> List[Dict]:
    return execute_query(
        "SELECT g.id, g.name, g.rarity, g.slot, g.emoji FROM gear_drops gd JOIN gear g ON gd.gear_id = g.id WHERE gd.mob_id = ? AND g.rarity = 'common'",
        (mob_id,)
    )

def get_resources_by_location(location_id: int, offset: int, limit: int) -> List[Dict]:
    query = """
        SELECT DISTINCT r.id, r.name, r.emoji
        FROM resources r
        JOIN mob_drops md ON r.id = md.resource_id
        JOIN mobs m ON md.mob_id = m.id
        WHERE m.location_id = ?
        LIMIT ? OFFSET ?
    """
    return execute_query(query, (location_id, limit, offset))

def get_resource_info(resource_id: int) -> Optional[Dict]:
    res = execute_query("SELECT id, name, emoji FROM resources WHERE id = ?", (resource_id,))
    return res[0] if res else None

def get_resource_mobs(resource_id: int) -> List[Dict]:
    return execute_query(
        "SELECT m.id, m.name, m.emoji FROM mob_drops md JOIN mobs m ON md.mob_id = m.id WHERE md.resource_id = ?",
        (resource_id,)
    )

def get_gear_by_location(location_id: int, offset: int, limit: int) -> List[Dict]:
    query = """
        SELECT DISTINCT g.id, g.name, g.rarity, g.slot, g.emoji
        FROM gear g
        JOIN gear_drops gd ON g.id = gd.gear_id
        JOIN mobs m ON gd.mob_id = m.id
        WHERE m.location_id = ? AND g.rarity = 'common'
        LIMIT ? OFFSET ?
    """
    return execute_query(query, (location_id, limit, offset))

# ---------- НОВАЯ ФУНКЦИЯ: получение снаряжения по редкости (без локации) ----------
def get_gear_by_rarity(rarity: str, offset: int, limit: int) -> List[Dict]:
    """Возвращает список снаряжения указанной редкости с пагинацией."""
    return execute_query(
        "SELECT id, name, rarity, slot, emoji, craftable, craft_dust FROM gear WHERE rarity = ? LIMIT ? OFFSET ?",
        (rarity, limit, offset)
    )

def get_gear_info(gear_id: int) -> Optional[Dict]:
    res = execute_query(
        "SELECT id, name, rarity, slot, craftable, craft_dust, emoji FROM gear WHERE id = ?",
        (gear_id,)
    )
    return res[0] if res else None

def get_gear_mobs(gear_id: int) -> List[Dict]:
    # Исправлено: правильный JOIN (было m.id = ?)
    return execute_query(
        "SELECT m.id, m.name, m.emoji FROM gear_drops gd JOIN mobs m ON gd.mob_id = m.id WHERE gd.gear_id = ?",
        (gear_id,)
    )

def get_locations() -> List[Dict]:
    return execute_query("SELECT id, name, emoji FROM locations ORDER BY id")
