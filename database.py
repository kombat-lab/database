import os
import sqlite3
from typing import List, Dict, Any, Optional

# Путь к базе из переменной окружения, по умолчанию 'game.db'
DB_PATH = os.getenv("DATABASE_PATH", "game.db")

def get_db_connection():
    return sqlite3.connect(DB_PATH)

def execute_query(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def search(query: str) -> Dict[str, List[Dict]]:
    """Поиск по мобам, ресурсам, снаряжению"""
    query = f"%{query}%"
    mobs = execute_query(
        "SELECT id, name, emoji, hp, dust_min, dust_max, exp, location_id FROM mobs WHERE name LIKE ?",
        (query,)
    )
    resources = execute_query(
        "SELECT id, name, emoji FROM resources WHERE name LIKE ?",
        (query,)
    )
    gear = execute_query(
        "SELECT id, name, rarity, slot, emoji FROM gear WHERE name LIKE ?",
        (query,)
    )
    return {"mobs": mobs, "resources": resources, "gear": gear}

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

def get_gear_info(gear_id: int) -> Optional[Dict]:
    res = execute_query(
        "SELECT id, name, rarity, slot, craftable, craft_dust, emoji FROM gear WHERE id = ?",
        (gear_id,)
    )
    return res[0] if res else None

def get_gear_mobs(gear_id: int) -> List[Dict]:
    return execute_query(
        "SELECT m.id, m.name, m.emoji FROM gear_drops gd JOIN mobs m ON gd.mob_id = m.id WHERE gd.gear_id = ?",
        (gear_id,)
    )

def get_locations() -> List[Dict]:
    return execute_query("SELECT id, name, emoji FROM locations ORDER BY id")
