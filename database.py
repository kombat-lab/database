import os
import asyncio
import logging
import aiosqlite
from typing import List, Dict, Any, Optional
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "game.db")

def _lower_unicode(s: str) -> str:
    if s is None:
        return None
    return s.lower()

class Database:
    ALLOWED_MOB_FIELDS = {'name', 'emoji', 'hp', 'dust_min', 'dust_max', 'exp', 'location_id'}
    ALLOWED_RESOURCE_FIELDS = {'name', 'emoji', 'type'}

    def __init__(self):
        self._conn: Optional[aiosqlite.Connection] = None
        self._locations_cache: Dict[int, Dict] = {}
        self._in_transaction = False

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
            "CREATE INDEX IF NOT EXISTS idx_drops_mob ON drops(mob_id)",
            "CREATE INDEX IF NOT EXISTS idx_drops_item ON drops(item_type, item_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipes_result ON recipes(result_type, result_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON recipe_ingredients(recipe_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_owners_recipe ON recipe_owners(recipe_id)",
            "CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)",
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
                if not query.strip().upper().startswith("SELECT") and not self._in_transaction:
                    await self._conn.commit()
                if not rows:
                    return []
                if not hasattr(rows[0], 'keys'):
                    col_names = [desc[0] for desc in cursor.description]
                    return [dict(zip(col_names, row)) for row in rows]
                return [dict(row) for row in rows]
            except Exception as e:
                if not self._in_transaction:
                    await self._conn.rollback()
                raise e

    @asynccontextmanager
    async def transaction(self):
        self._in_transaction = True
        try:
            await self._conn.execute("BEGIN")
            yield
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        finally:
            self._in_transaction = False

    async def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        return self._locations_cache.get(location_id)

    async def get_locations(self) -> List[Dict]:
        return list(self._locations_cache.values())

    async def invalidate_location_cache(self):
        await self._load_locations_cache()

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
                (SELECT GROUP_CONCAT(item_id || '|' || r.name || '|' || r.emoji)
                 FROM drops d JOIN resources r ON d.item_id = r.id
                 WHERE d.mob_id = m.id AND d.item_type = 'resource') as resource_drops,
                (SELECT GROUP_CONCAT(item_id || '|' || g.name || '|' || g.emoji || '|' || g.slot || '|' || g.rarity)
                 FROM drops d JOIN gear g ON d.item_id = g.id
                 WHERE d.mob_id = m.id AND d.item_type = 'gear') as gear_drops
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

    async def update_mob_field(self, mob_id: int, field: str, value):
        if field not in self.ALLOWED_MOB_FIELDS:
            raise ValueError(f"Invalid field: {field}")
        query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
        await self.execute_query(query, (value, mob_id))

    async def delete_mob(self, mob_id: int):
        await self.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))

    # ========== РЕСУРСЫ ==========
    async def get_resource_card(self, resource_id: int) -> Optional[Dict]:
        query = """
            SELECT r.id, r.name, r.emoji, r.type, r.note,
                   GROUP_CONCAT(m.id || '|' || m.name || '|' || m.emoji || '|' || l.name || '|' || l.emoji) as mobs
            FROM resources r
            LEFT JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
            LEFT JOIN mobs m ON d.mob_id = m.id
            LEFT JOIN locations l ON m.location_id = l.id
            WHERE r.id = ?
            GROUP BY r.id
        """
        res = await self.execute_query(query, (resource_id,))
        if not res:
            return None
        row = res[0]
        if row["mobs"]:
            row["mobs"] = [self._parse_mob_with_location(s) for s in row["mobs"].split(",")]
        else:
            row["mobs"] = []
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
        await self.execute_query(
            "INSERT INTO resources (name, emoji, type, note) VALUES (?, ?, ?, ?)",
            (name, emoji, resource_type, note)
        )
        res = await self.execute_query("SELECT last_insert_rowid() as id")
        return res[0]['id']

    async def update_resource(self, resource_id: int, name: str = None, emoji: str = None, resource_type: str = None, note: str = None):
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

    async def delete_resource(self, resource_id: int):
        await self.execute_query("DELETE FROM drops WHERE item_type = 'resource' AND item_id = ?", (resource_id,))
        await self.execute_query("DELETE FROM recipe_ingredients WHERE resource_id = ?", (resource_id,))
        await self.execute_query("DELETE FROM resources WHERE id = ?", (resource_id,))

    async def get_resources_by_type(self, resource_type: str, offset: int, limit: int) -> List[Dict]:
        if resource_type == 'scroll_recipe':
            return await self.execute_query(
                "SELECT id, name, emoji, type FROM resources WHERE type IN ('scroll_recipe', 'scroll') ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (limit, offset)
            )
        else:
            return await self.execute_query(
                "SELECT id, name, emoji, type FROM resources WHERE type = ? ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?",
                (resource_type, limit, offset)
            )

    async def get_resources_by_type_all(self, resource_type: str) -> List[Dict]:
        if resource_type == 'scroll_recipe':
            return await self.execute_query(
                "SELECT id, name, emoji, type FROM resources WHERE type IN ('scroll_recipe', 'scroll') ORDER BY name COLLATE NOCASE"
            )
        else:
            return await self.execute_query(
                "SELECT id, name, emoji, type FROM resources WHERE type = ? ORDER BY name COLLATE NOCASE",
                (resource_type,)
            )

    async def get_all_resources_simple(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM resources ORDER BY id")

    async def get_prev_next_resource_by_type(self, resource_id: int, resource_type: str) -> Dict[str, Optional[int]]:
        if resource_type == 'scroll_recipe':
            prev_query = """
                SELECT id FROM resources
                WHERE id < ? AND type IN ('scroll_recipe', 'scroll')
                ORDER BY id DESC LIMIT 1
            """
            next_query = """
                SELECT id FROM resources
                WHERE id > ? AND type IN ('scroll_recipe', 'scroll')
                ORDER BY id LIMIT 1
            """
            prev_params = (resource_id,)
            next_params = (resource_id,)
        else:
            prev_query = """
                SELECT id FROM resources
                WHERE id < ? AND type = ?
                ORDER BY id DESC LIMIT 1
            """
            next_query = """
                SELECT id FROM resources
                WHERE id > ? AND type = ?
                ORDER BY id LIMIT 1
            """
            prev_params = (resource_id, resource_type)
            next_params = (resource_id, resource_type)

        prev_res = await self.execute_query(prev_query, prev_params)
        next_res = await self.execute_query(next_query, next_params)

        return {
            'prev_id': prev_res[0]['id'] if prev_res else None,
            'next_id': next_res[0]['id'] if next_res else None
        }

    # ========== СНАРЯЖЕНИЕ ==========
    async def get_all_gear(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji FROM gear ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_gear_by_id(self, gear_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT * FROM gear WHERE id = ?", (gear_id,))
        return res[0] if res else None

    async def add_gear(self, name: str, rarity: str, slot: str, emoji: str) -> int:
        await self.execute_query(
            "INSERT INTO gear (name, rarity, slot, emoji) VALUES (?, ?, ?, ?)",
            (name, rarity, slot, emoji)
        )
        res = await self.execute_query("SELECT last_insert_rowid() as id")
        return res[0]['id']

    async def update_gear(self, gear_id: int, name: str = None, rarity: str = None, slot: str = None, emoji: str = None):
        current = await self.get_gear_by_id(gear_id)
        if not current:
            raise ValueError("Gear not found")
        new_name = name if name is not None else current['name']
        new_rarity = rarity if rarity is not None else current['rarity']
        new_slot = slot if slot is not None else current['slot']
        new_emoji = emoji if emoji is not None else current['emoji']
        await self.execute_query(
            "UPDATE gear SET name=?, rarity=?, slot=?, emoji=? WHERE id=?",
            (new_name, new_rarity, new_slot, new_emoji, gear_id)
        )

    async def delete_gear(self, gear_id: int):
        await self.execute_query("DELETE FROM drops WHERE item_type='gear' AND item_id=?", (gear_id,))
        recipe = await self.execute_query(
            "SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear_id,)
        )
        if recipe:
            recipe_id = recipe[0]['id']
            await self.execute_query("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
            await self.execute_query("DELETE FROM recipe_owners WHERE recipe_id=?", (recipe_id,))
            await self.execute_query("DELETE FROM recipes WHERE id=?", (recipe_id,))
        await self.execute_query("DELETE FROM gear WHERE id=?", (gear_id,))

    async def get_gear_card(self, gear_id: int) -> Optional[Dict]:
        query = """
            SELECT g.id, g.name, g.rarity, g.slot, g.emoji,
                   (SELECT GROUP_CONCAT(m.id || '|' || m.name || '|' || m.emoji)
                    FROM drops d JOIN mobs m ON d.mob_id = m.id
                    WHERE d.item_type = 'gear' AND d.item_id = g.id) as mobs,
                   (SELECT GROUP_CONCAT(ri.resource_id || '|' || r.name || '|' || r.emoji || '|' || ri.quantity)
                    FROM recipes rc
                    JOIN recipe_ingredients ri ON rc.id = ri.recipe_id
                    JOIN resources r ON ri.resource_id = r.id
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as ingredients,
                   (SELECT GROUP_CONCAT(player_username)
                    FROM recipe_owners ro
                    WHERE ro.recipe_id = (SELECT id FROM recipes WHERE result_type='gear' AND result_id=g.id)) as owners
            FROM gear g
            WHERE g.id = ?
        """
        res = await self.execute_query(query, (gear_id,))
        if not res:
            return None
        row = res[0]
        direct_mobs = [self._parse_drop_item(s) for s in (row["mobs"].split(",") if row["mobs"] else [])]
        if row['rarity'] == 'epic' and not direct_mobs:
            ingredients = [self._parse_ingredient(s) for s in (row["ingredients"].split(",") if row["ingredients"] else [])]
            scroll_resource_id = None
            for ing in ingredients:
                if ing['id'] in range(59, 70) or 'свиток' in ing['name'].lower():
                    scroll_resource_id = ing['id']
                    break
            if scroll_resource_id:
                mobs_data = await self.execute_query(
                    "SELECT m.id, m.name, m.emoji FROM drops d "
                    "JOIN mobs m ON d.mob_id = m.id "
                    "WHERE d.item_type = 'resource' AND d.item_id = ? ORDER BY m.id",
                    (scroll_resource_id,)
                )
                row["mobs"] = mobs_data
            else:
                row["mobs"] = []
        else:
            row["mobs"] = direct_mobs
        row["ingredients"] = [self._parse_ingredient(s) for s in (row["ingredients"].split(",") if row["ingredients"] else [])]
        row["owners"] = row["owners"].split(",") if row["owners"] else []
        row["craftable"] = bool(row["ingredients"])
        return row

    async def get_gear_by_rarity(self, rarity: str, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji FROM gear WHERE rarity = ? ORDER BY id LIMIT ? OFFSET ?",
            (rarity, limit, offset)
        )

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
        await self.execute_query(
            "INSERT INTO recipes (result_type, result_id, quantity) VALUES (?, ?, ?)",
            (result_type, result_id, quantity)
        )
        res = await self.execute_query("SELECT last_insert_rowid() as id")
        return res[0]['id']

    async def delete_recipe(self, recipe_id: int):
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

    async def get_card_by_id(self, card_id: int) -> Optional[Dict]:
        res = await self.execute_query("SELECT * FROM cards WHERE id = ?", (card_id,))
        return res[0] if res else None

    async def add_card(self, name: str, emoji: str, slot: str,
                       bonus1: str = '', bonus2: str = '', bonus3: str = '', bonus4: str = '',
                       note: str = '') -> int:
        await self.execute_query(
            """INSERT INTO cards (name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
        )
        res = await self.execute_query("SELECT last_insert_rowid() as id")
        return res[0]['id']

    async def update_card(self, card_id: int, **kwargs):
        allowed = {'name', 'emoji', 'slot', 'bonus1', 'bonus2', 'bonus3', 'bonus4', 'note'}
        for field, value in kwargs.items():
            if field in allowed:
                await self.execute_query(f"UPDATE cards SET {field}=? WHERE id=?", (value, card_id))

    async def delete_card(self, card_id: int):
        await self.execute_query("DELETE FROM drops WHERE item_type='card' AND item_id=?", (card_id,))
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

    async def get_all_cards_simple(self) -> List[Dict]:
        return await self.execute_query("SELECT id, name, emoji FROM cards ORDER BY id")

    async def get_all_cards(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, slot, bonus1, bonus2, bonus3, bonus4 FROM cards ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    # ========== ДРОПЫ ==========
    async def get_drop_status(self, mob_id: int, item_type: str, item_id: int) -> bool:
        res = await self.execute_query(
            "SELECT 1 FROM drops WHERE mob_id = ? AND item_type = ? AND item_id = ?",
            (mob_id, item_type, item_id)
        )
        return len(res) > 0

    async def add_drop(self, mob_id: int, item_type: str, item_id: int):
        await self.execute_query(
            "INSERT OR IGNORE INTO drops (mob_id, item_type, item_id) VALUES (?, ?, ?)",
            (mob_id, item_type, item_id)
        )

    async def remove_drop(self, mob_id: int, item_type: str, item_id: int):
        await self.execute_query(
            "DELETE FROM drops WHERE mob_id = ? AND item_type = ? AND item_id = ?",
            (mob_id, item_type, item_id)
        )

    # ========== ВСПОМОГАТЕЛЬНЫЕ ==========
    async def get_resources_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, type FROM resources ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_common_gear_page(self, offset: int, limit: int) -> List[Dict]:
        return await self.execute_query(
            "SELECT id, name, emoji, slot FROM gear WHERE rarity = 'common' ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_all_resources(self):
        return await self.execute_query("SELECT id, name, emoji, type FROM resources")

    # ========== ПАРСЕРЫ ==========
    @staticmethod
    def _parse_drop_item(s: str, gear: bool = False) -> Dict:
        parts = s.split("|")
        if gear:
            if len(parts) >= 5:
                return {
                    "id": int(parts[0]),
                    "name": parts[1],
                    "emoji": parts[2],
                    "slot": parts[3],
                    "rarity": parts[4]
                }
            else:
                return {"id": int(parts[0]), "name": parts[1], "emoji": parts[2], "slot": parts[3], "rarity": "common"}
        else:
            return {"id": int(parts[0]), "name": parts[1], "emoji": parts[2]}

    @staticmethod
    def _parse_ingredient(s: str) -> Dict:
        parts = s.split("|")
        return {"id": int(parts[0]), "name": parts[1], "emoji": parts[2], "quantity": int(parts[3])}

    @staticmethod
    def _parse_mob_with_location(s: str) -> Dict:
        parts = s.split("|")
        return {
            "id": int(parts[0]),
            "name": parts[1],
            "emoji": parts[2],
            "location_name": parts[3] if len(parts) > 3 else None,
            "location_emoji": parts[4] if len(parts) > 4 else None
        }

db = Database()
