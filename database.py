import asyncio
import json
import logging
import os
import hashlib
import secrets
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Awaitable
from typing import Any, TypeAlias, TypeVar

import aiosqlite

from catalog_types import (
    ItemRow as ItemRow,
    ResourceRow as ResourceRow,
    GearRow as GearRow,
    CardRow as CardRow,
    NavigationIds as NavigationIds,
    RecipeOwnerEntry as RecipeOwnerEntry,
    SearchItem as SearchItem,
    GearDropRow as GearDropRow,
    CardDropRow as CardDropRow,
    MobCardRow as MobCardRow,
    ResourceDropMobRow as ResourceDropMobRow,
    ResourceUsageRow as ResourceUsageRow,
    ResourceCardRow as ResourceCardRow,
    GearIngredientRow as GearIngredientRow,
    GearCardRow as GearCardRow,
    RecipeIngredientRow as RecipeIngredientRow,
    ResourceRecipeRow as ResourceRecipeRow,
    LearningRecipeRow as LearningRecipeRow,
    RecipeDetailsRow as RecipeDetailsRow,
)
from game_constants import (
    GEAR_SLOTS, RARITY_KEYS, RESOURCE_TYPE_KEYS,
    LEGACY_ALCHEMY_CRAFT_LOCATIONS, LEGACY_DEFAULT_ALCHEMY_CRAFT_LOCATION,
)
from recipe_domain import (
    DomainError, DraftConflictError, GearDraft, GearDraftPayload, GearSaveResult,
    LearningScrollInput, MaterialInput, ResourceDependencies, positive_integer, validate_draft_payload, validate_craft_location,
)

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DATABASE_PATH", "game.db")
SCHEMA_VERSION = 2
_T = TypeVar("_T")
# Arbitrary SQL may return differently shaped rows; entity methods narrow them.
SqlValue: TypeAlias = int | float | str | bytes | None
SqlParams: TypeAlias = tuple[SqlValue, ...]
DbRow: TypeAlias = dict[str, Any]


def _lower_unicode(s: str | None) -> str | None:
    if s is None:
        return None
    return s.lower()


def _item_row(row: DbRow) -> ItemRow:
    return ItemRow(id=int(row['id']), name=str(row['name']), emoji=str(row['emoji']))


def _gear_row(row: DbRow) -> GearRow:
    return GearRow(
        **_item_row(row), rarity=str(row['rarity']), slot=str(row['slot']),
        level=int(row['level']), classes=str(row['classes']), note=str(row['note']),
    )


def _resource_row(row: DbRow) -> ResourceRow:
    return ResourceRow(**_item_row(row), type=str(row['type']), note=str(row['note']))


def _json_rows(value: str | None) -> list[DbRow]:
    decoded: object = json.loads(value or '[]')
    if not isinstance(decoded, list):
        raise ValueError("Expected a JSON array from the catalog query")
    rows: list[DbRow] = []
    for item in decoded:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise ValueError("Expected JSON objects from the catalog query")
        rows.append(dict(item))
    return rows


def _recipe_ingredient(row: DbRow) -> RecipeIngredientRow:
    return RecipeIngredientRow(
        resource_id=int(row['resource_id']), name=str(row['name']),
        emoji=str(row['emoji']), quantity=int(row['quantity']),
    )


def _owner_entry(row: DbRow) -> RecipeOwnerEntry:
    return RecipeOwnerEntry(
        owner_id=int(row['owner_id']),
        user_id=int(row['user_id']) if row['user_id'] is not None else None,
        player_username=str(row['player_username']) if row['player_username'] is not None else None,
    )


def _search_item(row: DbRow) -> SearchItem:
    item: SearchItem = {
        'id': int(row['id']), 'name': str(row['name']), 'emoji': str(row['emoji']),
    }
    if 'location_name' in row:
        item['location_name'] = str(row['location_name']) if row['location_name'] is not None else None
    if 'location_emoji' in row:
        item['location_emoji'] = str(row['location_emoji']) if row['location_emoji'] is not None else None
    if 'hp' in row:
        item['hp'] = int(row['hp'])
    if 'dust_min' in row:
        item['dust_min'] = int(row['dust_min'])
    if 'dust_max' in row:
        item['dust_max'] = int(row['dust_max'])
    if 'exp' in row:
        item['exp'] = int(row['exp'])
    if 'rarity' in row:
        item['rarity'] = str(row['rarity'])
    if 'slot' in row:
        item['slot'] = str(row['slot'])
    if 'type' in row:
        item['type'] = str(row['type'])
    return item


class Database:
    ALLOWED_MOB_FIELDS = frozenset({'name', 'emoji', 'hp', 'dust_min', 'dust_max', 'exp', 'location_id'})
    RESOURCE_NAME_ORDER = "LOWER_UNICODE(name), id"
    
    @staticmethod
    def _slot_order_case(column: str = "slot") -> str:
        branches = " ".join(
            f"WHEN '{slot}' THEN {order}"
            for order, slot in enumerate(GEAR_SLOTS, start=1)
        )
        return f"CASE {column} {branches} ELSE 99 END"

    @staticmethod
    def _rarity_order_case(column: str = "rarity") -> str:
        branches = " ".join(
            f"WHEN '{rarity}' THEN {order}"
            for order, rarity in enumerate(RARITY_KEYS, start=1)
        )
        return f"CASE {column} {branches} ELSE 99 END"

    def __init__(self, path: str | None = None) -> None:
        self.path = path or DB_PATH
        self._conn: aiosqlite.Connection | None = None
        self._locations_cache: dict[int, ItemRow] = {}
        self._connection_lock = asyncio.Lock()
        self._connection_lock_owner: asyncio.Task[object] | None = None
        self._connection_lock_depth = 0
        self._transaction_depth = 0

    @asynccontextmanager
    async def _connection_guard(self) -> AsyncIterator[None]:
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

    @staticmethod
    async def _finish_sqlite(operation: Awaitable[_T]) -> tuple[_T, bool]:
        """Drain a queued SQLite operation before the connection lock is released.

        aiosqlite cannot stop SQL already running on its worker thread. Shielding
        and draining also handles repeated cancellation during rollback/close.
        """
        pending = asyncio.ensure_future(operation)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        return pending.result(), cancelled

    def _require_connection(self) -> aiosqlite.Connection:
        connection = self._conn
        if connection is None:
            raise RuntimeError("Database is not connected")
        return connection

    async def _discard_connection(self) -> None:
        connection = self._conn
        self._conn = None
        if connection is not None:
            await self._finish_sqlite(connection.close())

    async def _rollback_safely(self) -> None:
        if self._conn is not None:
            try:
                await self._finish_sqlite(self._conn.rollback())
            except BaseException:
                # A failed rollback makes the connection unsafe for later work.
                await self._discard_connection()
                raise

    async def connect(self) -> None:
        async with self._connection_guard():
            if self._conn is not None:
                return
            if self._transaction_depth:
                raise RuntimeError("Cannot reconnect inside an unfinished transaction")
            try:
                connection, cancelled = await self._finish_sqlite(
                    aiosqlite.connect(self.path, timeout=30.0)
                )
                self._conn = connection
                if cancelled:
                    raise asyncio.CancelledError
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA foreign_keys = ON")
                version = await self.execute_query("PRAGMA user_version")
                if version[0]['user_version'] > SCHEMA_VERSION:
                    raise RuntimeError("Database schema is newer than this application")
                await connection.execute("PRAGMA journal_mode = WAL")
                await connection.execute("PRAGMA busy_timeout = 30000")
                await connection.create_function("LOWER_UNICODE", 1, _lower_unicode)
                await self._ensure_schema()
                await self._migrate_schema()
                await self._ensure_indexes()
                await self._load_locations_cache()
            except BaseException:
                await self._discard_connection()
                raise
            logger.info("Database connected: %s", self.path)

    async def _migrate_schema(self) -> None:
        """Upgrade legacy owner rows atomically without assigning user identities."""
        async with self.transaction():
            version = (await self.execute_query("PRAGMA user_version"))[0]['user_version']
            if version >= SCHEMA_VERSION:
                return
            columns = {row['name'] for row in await self.execute_query(
                "PRAGMA table_info(recipe_owners)"
            )}
            if not {'owner_id', 'user_id'}.issubset(columns):
                await self.execute_query("""
                    CREATE TABLE recipe_owners_v1 (
                        owner_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        recipe_id INTEGER NOT NULL,
                        user_id INTEGER,
                        player_username TEXT,
                        FOREIGN KEY (recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                        FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE,
                        CHECK (user_id IS NOT NULL OR player_username IS NOT NULL)
                    )
                """)
                # Legacy names remain manual records. COLLATE NOCASE deduplicates
                # ASCII Telegram usernames without ever attributing them to users.
                await self.execute_query("""
                    INSERT INTO recipe_owners_v1 (recipe_id, player_username)
                    SELECT recipe_id, MIN(player_username)
                    FROM recipe_owners
                    GROUP BY recipe_id, player_username COLLATE NOCASE
                """)
                await self.execute_query("DROP TABLE recipe_owners")
                await self.execute_query("ALTER TABLE recipe_owners_v1 RENAME TO recipe_owners")
            await self.execute_query("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recipe_owners_user
                ON recipe_owners(recipe_id, user_id) WHERE user_id IS NOT NULL
            """)
            await self.execute_query("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recipe_owners_manual
                ON recipe_owners(recipe_id, player_username COLLATE NOCASE)
                WHERE user_id IS NULL
            """)
            await self._migrate_learning_and_drafts()
            await self.execute_query(f"PRAGMA user_version = {SCHEMA_VERSION}")

    async def _migrate_learning_and_drafts(self) -> None:
        recipe_columns = {str(row['name']) for row in await self.execute_query('PRAGMA table_info(recipes)')}
        if 'craft_location' not in recipe_columns:
            await self.execute_query("ALTER TABLE recipes ADD COLUMN craft_location TEXT NOT NULL DEFAULT ''")
            for recipe in await self.execute_query(
                "SELECT rec.id,s.name FROM recipes rec JOIN resources s ON rec.result_type='resource' AND s.id=rec.result_id",
            ):
                craft_location = LEGACY_ALCHEMY_CRAFT_LOCATIONS.get(str(recipe['name']).casefold(), LEGACY_DEFAULT_ALCHEMY_CRAFT_LOCATION)
                await self.execute_query('UPDATE recipes SET craft_location=? WHERE id=?', (craft_location, recipe['id']))
        # Validate before moving any legacy edge: one scroll teaches one formula.
        legacy = await self.execute_query("""
            SELECT ri.recipe_id, ri.resource_id, ri.quantity, rec.result_type, g.id AS gear_id
            FROM recipe_ingredients ri
            JOIN resources s ON s.id = ri.resource_id AND s.type = 'scroll_recipe'
            JOIN recipes rec ON rec.id = ri.recipe_id
            LEFT JOIN gear g ON rec.result_type = 'gear' AND g.id = rec.result_id
        """)
        if (any(row['quantity'] != 1 or row['result_type'] != 'gear' or row['gear_id'] is None
                for row in legacy)
                or len({row['recipe_id'] for row in legacy}) != len(legacy)
                or len({row['resource_id'] for row in legacy}) != len(legacy)):
            raise DomainError('Неоднозначные старые связи изучаемых свитков; миграция отменена.')
        if await self.execute_query("""
            SELECT 1 FROM recipes rec JOIN resources s
            ON rec.result_type = 'resource' AND rec.result_id = s.id
            WHERE s.type = 'scroll_recipe' LIMIT 1
        """):
            raise DomainError('Найден рецепт изготовления изучаемого свитка; требуется проверка данных.')
        for sql in (
            """CREATE TABLE IF NOT EXISTS recipe_learning_requirements (
                recipe_id INTEGER PRIMARY KEY,
                scroll_resource_id INTEGER NOT NULL UNIQUE,
                FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
                FOREIGN KEY(scroll_resource_id) REFERENCES resources(id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS gear_aliases (
                alias_id INTEGER PRIMARY KEY,
                canonical_gear_id INTEGER NOT NULL,
                FOREIGN KEY(canonical_gear_id) REFERENCES gear(id) ON DELETE RESTRICT,
                CHECK(alias_id != canonical_gear_id)
            )""",
            """CREATE TABLE IF NOT EXISTS gear_drafts (
                draft_id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'editing' CHECK(status IN ('editing','saved','cancelled')),
                target_gear_id INTEGER,
                base_fingerprint TEXT,
                base_owner_fingerprint TEXT,
                reference_fingerprints_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL,
                saved_result_json TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_gear_drafts_owner_chat ON gear_drafts(owner_user_id,chat_id,status)",
        ):
            await self.execute_query(sql)
        for row in legacy:
            await self.execute_query(
                'INSERT INTO recipe_learning_requirements(recipe_id,scroll_resource_id) VALUES (?,?)',
                (row['recipe_id'], row['resource_id']),
            )
            await self.execute_query(
                'DELETE FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?',
                (row['recipe_id'], row['resource_id']),
            )
        await self._validate_resource_graph()
        for row in await self.execute_query('SELECT quantity FROM recipes'):
            positive_integer(row['quantity'], 'Количество результата')
        # Existing resource foreign keys used CASCADE. A material must never disappear
        # silently when a resource is deleted, including through direct maintenance SQL.
        await self.execute_query("""CREATE TABLE recipe_ingredients_v2 (
            recipe_id INTEGER NOT NULL,
            resource_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            PRIMARY KEY(recipe_id,resource_id),
            FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
            FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE RESTRICT
        )""")
        await self.execute_query('INSERT INTO recipe_ingredients_v2 SELECT recipe_id,resource_id,quantity FROM recipe_ingredients')
        await self.execute_query('DROP TABLE recipe_ingredients')
        await self.execute_query('ALTER TABLE recipe_ingredients_v2 RENAME TO recipe_ingredients')

    async def _ensure_schema(self) -> None:
        """Create a complete empty database without touching existing rows."""
        statements = [
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
        connection = self._require_connection()
        for statement in statements:
            await connection.execute(statement)
        await connection.commit()

    async def _ensure_indexes(self) -> None:
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
            "CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_resource ON recipe_ingredients(resource_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_owners_recipe ON recipe_owners(recipe_id)",
            "CREATE INDEX IF NOT EXISTS idx_recipe_owners_user_id ON recipe_owners(user_id) WHERE user_id IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name)",
            "CREATE INDEX IF NOT EXISTS idx_resources_type_name ON resources(type, name)",
            "CREATE INDEX IF NOT EXISTS idx_gear_rarity_slot ON gear(rarity, slot)",
            "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON analytics_events(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_events_type ON analytics_events(event_type)",
            "CREATE INDEX IF NOT EXISTS idx_events_target ON analytics_events(target_type, target_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_user_timestamp ON analytics_events(user_id, timestamp)",
        ]
        connection = self._require_connection()
        for sql in indexes:
            await connection.execute(sql)
        await connection.commit()

    async def _load_locations_cache(self) -> None:
        locations = await self.execute_query("SELECT id, name, emoji FROM locations")
        self._locations_cache = {
            int(loc['id']): ItemRow(id=int(loc['id']), name=str(loc['name']), emoji=str(loc['emoji']))
            for loc in locations
        }

    async def close(self) -> None:
        async with self._connection_guard():
            connection = self._conn
            self._conn = None
            if connection is not None:
                _, cancelled = await self._finish_sqlite(connection.close())
                if cancelled:
                    raise asyncio.CancelledError

    async def execute_query(self, query: str, params: SqlParams = ()) -> list[DbRow]:
        async with self._connection_guard():
            connection = self._require_connection()
            try:
                async with connection.execute(query, params) as cursor:
                    rows = list(await cursor.fetchall())
                    if not query.lstrip().upper().startswith(("SELECT", "PRAGMA")) and self._transaction_depth == 0:
                        await connection.commit()
                    if not rows:
                        return []
                    if not hasattr(rows[0], 'keys'):
                        if cursor.description is None:
                            raise RuntimeError("SQL returned rows without column metadata")
                        col_names = [desc[0] for desc in cursor.description]
                        return [dict(zip(col_names, row)) for row in rows]
                    return [dict(row) for row in rows]
            except BaseException as e:
                if self._transaction_depth == 0:
                    await self._rollback_safely()
                if not isinstance(e, asyncio.CancelledError):
                    logger.error("[DB ERROR] %s", e)
                raise

    async def execute_insert(self, query: str, params: SqlParams = ()) -> int:
        """Execute one INSERT and return its row id without a concurrency race."""
        async with self._connection_guard():
            connection = self._require_connection()
            try:
                cursor = await connection.execute(query, params)
                try:
                    row_id = cursor.lastrowid
                finally:
                    await cursor.close()
                if self._transaction_depth == 0:
                    await connection.commit()
                if row_id is None:
                    raise RuntimeError("INSERT did not return a row id")
                return row_id
            except BaseException:
                if self._transaction_depth == 0:
                    await self._rollback_safely()
                raise

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Keep BEGIN, body, commit and cancellation cleanup under one lock."""
        async with self._connection_guard():
            if self._conn is None:
                raise RuntimeError("Database is not connected")
            nested = self._transaction_depth > 0
            savepoint = f"nested_{self._transaction_depth + 1}"
            started = entered = finished = False
            try:
                sql = f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE"
                cursor, cancelled = await self._finish_sqlite(self._conn.execute(sql))
                started = True
                _, close_cancelled = await self._finish_sqlite(cursor.close())
                if cancelled or close_cancelled:
                    raise asyncio.CancelledError
                self._transaction_depth += 1
                entered = True
                yield
                connection = self._require_connection()
                if nested:
                    cursor, cancelled = await self._finish_sqlite(
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    )
                    finished = True
                    _, close_cancelled = await self._finish_sqlite(cursor.close())
                    cancelled = cancelled or close_cancelled
                else:
                    _, cancelled = await self._finish_sqlite(connection.commit())
                    finished = True
                # Once COMMIT has completed cancellation cannot undo it, but the
                # connection is settled and cannot leak work into another handler.
                if cancelled:
                    raise asyncio.CancelledError
            except BaseException:
                if not finished:
                    if nested and started:
                        try:
                            connection = self._require_connection()
                            for sql in (f"ROLLBACK TO SAVEPOINT {savepoint}",
                                        f"RELEASE SAVEPOINT {savepoint}"):
                                cursor, _ = await self._finish_sqlite(connection.execute(sql))
                                await self._finish_sqlite(cursor.close())
                        except BaseException:
                            # The outer caller may catch this failure. Discard the
                            # entire connection so it cannot commit the inner work.
                            await self._discard_connection()
                            raise
                    elif not nested:
                        await self._rollback_safely()
                raise
            finally:
                if entered:
                    self._transaction_depth -= 1

    async def get_location_by_id(self, location_id: int) -> ItemRow | None:
        return self._locations_cache.get(location_id)

    async def get_locations(self) -> list[ItemRow]:
        return list(self._locations_cache.values())

    # ========== АНАЛИТИКА ==========
    async def register_user_if_not_exists(self, user_id: int, username: str | None = None,
                                          first_name: str | None = None, last_name: str | None = None) -> None:
        async with self.transaction():
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
            await self.execute_query(
                "UPDATE recipe_owners SET player_username = ? WHERE user_id = ? AND player_username IS NOT ?",
                (username, user_id, username),
            )

    # ========== ПОИСК ==========
    async def search(self, query: str) -> dict[str, list[SearchItem]]:
        literal = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{literal}%"
        results: dict[str, list[SearchItem]] = {"mobs": [], "resources": [], "gear": [], "cards": []}

        mobs = await self.execute_query("""
            SELECT m.id, m.name, m.emoji, m.hp, m.dust_min, m.dust_max, m.exp,
                   l.name AS location_name, l.emoji AS location_emoji
            FROM mobs m
            JOIN locations l ON m.location_id = l.id
            WHERE LOWER_UNICODE(m.name) LIKE LOWER_UNICODE(?) ESCAPE '\\'
            ORDER BY m.id
            LIMIT 50
        """, (like_pattern,))
        results["mobs"] = [_search_item(row) for row in mobs]

        resources = await self.execute_query("""
            SELECT id, name, emoji, type
            FROM resources
            WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ESCAPE '\\'
            ORDER BY id
            LIMIT 50
        """, (like_pattern,))
        results["resources"] = [_search_item(row) for row in resources]

        gear = await self.execute_query("""
            SELECT id, name, emoji, rarity, slot
            FROM gear
            WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ESCAPE '\\'
            ORDER BY id
            LIMIT 50
        """, (like_pattern,))
        results["gear"] = [_search_item(row) for row in gear]

        cards = await self.execute_query("""
            SELECT id, name, emoji, slot
            FROM cards
            WHERE LOWER_UNICODE(name) LIKE LOWER_UNICODE(?) ESCAPE '\\'
            ORDER BY id
            LIMIT 50
        """, (like_pattern,))
        results["cards"] = [_search_item(row) for row in cards]

        return results

    # ========== МОБЫ ==========
    async def get_mob_full_card(self, mob_id: int) -> MobCardRow | None:
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
        return MobCardRow(
            **_item_row(row), hp=int(row['hp']), dust_min=int(row['dust_min']),
            dust_max=int(row['dust_max']), exp=int(row['exp']),
            location_id=int(row['location_id']), loc_name=str(row['loc_name']), loc_emoji=str(row['loc_emoji']),
            resource_drops=[_item_row(drop) for drop in _json_rows(row['resource_drops'])],
            gear_drops=[GearDropRow(**_item_row(drop), slot=str(drop['slot']), rarity=str(drop['rarity']))
                        for drop in _json_rows(row['gear_drops'])],
            card_drops=[CardDropRow(**_item_row(drop), slot=str(drop['slot']))
                        for drop in _json_rows(row['card_drops'])],
        )

    async def get_mobs_by_location_sorted_by_hp(self, location_id: int, offset: int, limit: int) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs "
            "WHERE location_id = ? ORDER BY hp ASC, id LIMIT ? OFFSET ?",
            (location_id, limit, offset)
        )

    async def get_prev_next_mob_by_hp(self, mob_id: int, location_id: int) -> NavigationIds:
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
        return NavigationIds(
            prev_id=rows[0]['prev_id'] if rows else None,
            next_id=rows[0]['next_id'] if rows else None,
        )

    async def update_mob_field(self, mob_id: int, field: str, value: str | int) -> None:
        if field not in self.ALLOWED_MOB_FIELDS:
            raise ValueError(f"Invalid field: {field}")
        async with self.transaction():
            rows = await self.execute_query(
                "SELECT dust_min, dust_max FROM mobs WHERE id = ?", (mob_id,)
            )
            if not rows:
                raise ValueError("Моб не найден.")
            if field in {'hp', 'dust_min', 'dust_max', 'exp', 'location_id'}:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("Значение должно быть целым числом.")
                if value < 0:
                    raise ValueError("Значение не может быть отрицательным.")
                if field in {'dust_min', 'dust_max'}:
                    minimum = value if field == 'dust_min' else rows[0]['dust_min']
                    maximum = value if field == 'dust_max' else rows[0]['dust_max']
                    if minimum > maximum:
                        raise ValueError("Минимум пыли не может быть больше максимума.")
            query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
            await self.execute_query(query, (value, mob_id))

    async def delete_mob(self, mob_id: int) -> None:
        async with self.transaction():
            await self.execute_query("DELETE FROM drops WHERE mob_id = ?", (mob_id,))
            await self.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))

    # ========== РЕСУРСЫ ==========
    async def get_resource_card(self, resource_id: int) -> ResourceCardRow | None:
        query = """
            SELECT r.id, r.name, r.emoji, r.type, r.note,
                   COALESCE((SELECT craft_location FROM recipes WHERE result_type='resource' AND result_id=r.id),'') AS craft_location,
                   (SELECT json_group_array(json_object(
                        'id', m.id, 'name', m.name, 'emoji', m.emoji,
                        'location_id', l.id,
                        'location_name', l.name, 'location_emoji', l.emoji
                    ))
                    FROM drops d
                    JOIN mobs m ON d.mob_id = m.id
                    JOIN locations l ON m.location_id = l.id
                    WHERE d.item_type = 'resource' AND d.item_id = r.id) AS mobs,
                   (SELECT json_group_array(json_object(
                       'recipe_id', usage.recipe_id,
                       'result_type', usage.result_type,
                       'result_id', usage.result_id,
                       'result_name', usage.result_name,
                       'result_emoji', usage.result_emoji,
                       'result_rarity', usage.result_rarity,
                       'quantity', usage.quantity
                    ))
                    FROM (
                        SELECT * FROM (
                            SELECT rec.id AS recipe_id, rec.result_type, rec.result_id,
                                   g.name AS result_name, g.emoji AS result_emoji,
                                   g.rarity AS result_rarity, ri.quantity, 1 AS type_order
                            FROM recipe_ingredients ri
                            JOIN recipes rec ON rec.id = ri.recipe_id
                            JOIN gear g ON rec.result_type = 'gear' AND g.id = rec.result_id
                            WHERE ri.resource_id = r.id
                            UNION ALL
                            SELECT rec.id AS recipe_id, rec.result_type, rec.result_id,
                                   result.name AS result_name, result.emoji AS result_emoji,
                                   NULL AS result_rarity, ri.quantity, 2 AS type_order
                            FROM recipe_ingredients ri
                            JOIN recipes rec ON rec.id = ri.recipe_id
                            JOIN resources result
                              ON rec.result_type = 'resource' AND result.id = rec.result_id
                            WHERE ri.resource_id = r.id
                        )
                        ORDER BY type_order, LOWER_UNICODE(result_name), result_id
                    ) AS usage) AS used_in
            FROM resources r
            WHERE r.id = ?
        """
        res = await self.execute_query(query, (resource_id,))
        if not res:
            return None
        row = res[0]
        mobs = [ResourceDropMobRow(
            **_item_row(mob), location_id=int(mob['location_id']),
            location_name=str(mob['location_name']), location_emoji=str(mob['location_emoji']),
        ) for mob in _json_rows(row['mobs'])]
        mobs.sort(key=lambda mob: (mob['location_id'], mob['name'].casefold(), mob['id']))
        usages = [ResourceUsageRow(
            recipe_id=int(usage['recipe_id']), result_type=str(usage['result_type']),
            result_id=int(usage['result_id']), result_name=str(usage['result_name']),
            result_emoji=str(usage['result_emoji']), quantity=int(usage['quantity']),
            result_rarity=str(usage['result_rarity']) if usage['result_rarity'] is not None else None,
        ) for usage in _json_rows(row['used_in'])]
        learning_recipes: list[LearningRecipeRow] = []
        for link in await self.execute_query("""
            SELECT rec.id, rec.result_id, rec.quantity, g.name, g.emoji, g.rarity
            FROM recipe_learning_requirements lr JOIN recipes rec ON rec.id=lr.recipe_id
            JOIN gear g ON rec.result_type='gear' AND g.id=rec.result_id
            WHERE lr.scroll_resource_id=? ORDER BY rec.id
        """, (resource_id,)):
            details = await self.get_recipe_details(int(link['id']))
            if details is not None:
                entries = details['owner_entries']
                learning_recipes.append(LearningRecipeRow(
                    recipe_id=int(link['id']), result_id=int(link['result_id']),
                    result_name=str(link['name']), result_emoji=str(link['emoji']),
                    result_rarity=str(link['rarity']), quantity=int(link['quantity']),
                    ingredients=details['ingredients'], owner_entries=entries,
                    owner_user_ids=[entry['user_id'] for entry in entries if entry['user_id'] is not None],
                ))
        return ResourceCardRow(**_resource_row(row), mobs=mobs, used_in=usages, learning_recipes=learning_recipes, craft_location=str(row['craft_location']))

    async def get_resources_by_location(self, location_id: int, offset: int, limit: int) -> list[DbRow]:
        query = """
            SELECT DISTINCT r.id, r.name, r.emoji, r.type
            FROM resources r
            JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
            JOIN mobs m ON d.mob_id = m.id
            WHERE m.location_id = ?
            ORDER BY r.id LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (location_id, limit, offset))

    async def get_resource_by_id(self, resource_id: int) -> ResourceRow | None:
        res = await self.execute_query(
            "SELECT id, name, emoji, type, note FROM resources WHERE id = ?",
            (resource_id,)
        )
        if not res:
            return None
        row = res[0]
        return ResourceRow(
            id=int(row['id']), name=str(row['name']), emoji=str(row['emoji']),
            type=str(row['type']), note=str(row['note']),
        )

    async def add_resource(self, name: str, emoji: str, resource_type: str = 'craft', note: str = '') -> int:
        return await self.execute_insert(
            "INSERT INTO resources (name, emoji, type, note) VALUES (?, ?, ?, ?)",
            (name, emoji, resource_type, note)
        )

    async def update_resource(self, resource_id: int, name: str | None = None, emoji: str | None = None, resource_type: str | None = None, note: str | None = None) -> None:
        async with self.transaction():
            current = await self.get_resource_by_id(resource_id)
            if not current:
                raise ValueError("Resource not found")
            new_name = name if name is not None else current['name']
            new_emoji = emoji if emoji is not None else current['emoji']
            new_type = resource_type if resource_type is not None else current['type']
            new_note = note if note is not None else current['note']
            if new_type not in RESOURCE_TYPE_KEYS:
                raise DomainError('Неизвестный тип ресурса.')
            if new_type != current['type']:
                dependencies = await self.get_resource_dependencies(resource_id)
                if (dependencies['learning_recipe_ids'] and new_type != 'scroll_recipe') or (
                    new_type == 'scroll_recipe' and (dependencies['ingredient_recipe_ids'] or dependencies['result_recipe_ids'])
                ):
                    raise DomainError('Тип ресурса несовместим с существующими материалами или изучением.')
            await self.execute_query(
                "UPDATE resources SET name=?, emoji=?, type=?, note=? WHERE id=?",
                (new_name, new_emoji, new_type, new_note, resource_id)
            )

    async def _delete_recipes_by_result(self, result_type: str, result_id: int) -> None:
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

    async def get_resource_dependencies(self, resource_id: int) -> ResourceDependencies:
        return ResourceDependencies(
            ingredient_recipe_ids=[int(row['recipe_id']) for row in await self.execute_query(
                'SELECT recipe_id FROM recipe_ingredients WHERE resource_id=? ORDER BY recipe_id', (resource_id,))],
            learning_recipe_ids=[int(row['recipe_id']) for row in await self.execute_query(
                'SELECT recipe_id FROM recipe_learning_requirements WHERE scroll_resource_id=?', (resource_id,))],
            result_recipe_ids=[int(row['id']) for row in await self.execute_query(
                "SELECT id FROM recipes WHERE result_type='resource' AND result_id=?", (resource_id,))],
            drop_mob_ids=[int(row['mob_id']) for row in await self.execute_query(
                "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id", (resource_id,))],
        )

    async def delete_resource(self, resource_id: int) -> None:
        async with self.transaction():
            dependencies = await self.get_resource_dependencies(resource_id)
            if any(dependencies.values()):
                raise DomainError('Ресурс используется в материалах, изучении, рецепте или источниках. Сначала явно удалите эти связи.')
            await self.execute_query('DELETE FROM resources WHERE id=?', (resource_id,))

    async def get_resources_by_type(self, resource_type: str, offset: int, limit: int) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, emoji, type FROM resources WHERE type = ? "
            f"ORDER BY {self.RESOURCE_NAME_ORDER} LIMIT ? OFFSET ?",
            (resource_type, limit, offset)
        )

    async def get_all_resources_simple(self) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, emoji FROM resources "
            f"ORDER BY {self.RESOURCE_NAME_ORDER}"
        )

    async def get_prev_next_resource_by_type(self, resource_id: int, resource_type: str) -> NavigationIds:
        rows = await self.execute_query(
            f"""
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY {self.RESOURCE_NAME_ORDER}) AS prev_id,
                       LEAD(id) OVER (ORDER BY {self.RESOURCE_NAME_ORDER}) AS next_id
                FROM resources
                WHERE type = ?
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (resource_type, resource_id),
        )
        return NavigationIds(
            prev_id=rows[0]['prev_id'] if rows else None,
            next_id=rows[0]['next_id'] if rows else None,
        )

    async def get_prev_next_resource_by_location(
        self,
        resource_id: int,
        location_id: int,
    ) -> NavigationIds:
        rows = await self.execute_query(
            """
            WITH location_resources AS (
                SELECT r.id
                FROM resources r
                JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
                JOIN mobs m ON m.id = d.mob_id
                WHERE m.location_id = ?
                GROUP BY r.id
            ), ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY id) AS prev_id,
                       LEAD(id) OVER (ORDER BY id) AS next_id
                FROM location_resources
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (location_id, resource_id),
        )
        return NavigationIds(
            prev_id=rows[0]['prev_id'] if rows else None,
            next_id=rows[0]['next_id'] if rows else None,
        )

    # ========== СНАРЯЖЕНИЕ ==========
    async def get_all_gear(self, offset: int, limit: int) -> list[DbRow]:
        slot_order = self._slot_order_case()
        rarity_order = self._rarity_order_case()
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji, level, classes, note "
            f"FROM gear ORDER BY {slot_order}, {rarity_order}, level, "
            "LOWER_UNICODE(name), id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_gear_by_slot(
        self,
        slot: str,
        offset: int,
        limit: int,
    ) -> list[DbRow]:
        rarity_order = self._rarity_order_case()
        return await self.execute_query(
            "SELECT id, name, emoji, rarity, level FROM gear WHERE slot = ? "
            f"ORDER BY {rarity_order}, level, LOWER_UNICODE(name), id "
            "LIMIT ? OFFSET ?",
            (slot, limit, offset),
        )

    async def get_gear_by_rarity_slot(
        self,
        rarity: str,
        slot: str,
        offset: int,
        limit: int,
    ) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, rarity, slot, emoji, level, classes, note "
            "FROM gear WHERE rarity = ? AND slot = ? "
            "ORDER BY level, LOWER_UNICODE(name), id LIMIT ? OFFSET ?",
            (rarity, slot, limit, offset),
        )

    async def get_gear_by_id(self, gear_id: int) -> GearRow | None:
        gear_id = await self.resolve_gear_id(gear_id)
        res = await self.execute_query("SELECT * FROM gear WHERE id = ?", (gear_id,))
        if not res:
            return None
        row = res[0]
        return GearRow(
            id=int(row['id']), name=str(row['name']), emoji=str(row['emoji']),
            rarity=str(row['rarity']), slot=str(row['slot']), level=int(row['level']),
            classes=str(row['classes']), note=str(row['note']),
        )

    async def add_gear(self, name: str, rarity: str, slot: str, emoji: str, level: int = 1, classes: str = "", note: str = "") -> int:
        return await self.execute_insert(
            "INSERT INTO gear (name, rarity, slot, emoji, level, classes, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, rarity, slot, emoji, level, classes, note)
        )

    async def update_gear(self, gear_id: int, name: str | None = None, rarity: str | None = None, slot: str | None = None, emoji: str | None = None, level: int | None = None, classes: str | None = None, note: str | None = None) -> None:
        async with self.transaction():
            gear_id = await self.resolve_gear_id(gear_id)
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

    async def delete_gear(self, gear_id: int) -> None:
        async with self.transaction():
            gear_id = await self.resolve_gear_id(gear_id)
            if await self.execute_query('SELECT 1 FROM gear_aliases WHERE canonical_gear_id=?', (gear_id,)):
                raise DomainError('Предмет сохраняет старые ссылки после объединения. Его удаление требует проверки алиасов.')
            await self.execute_query("DELETE FROM drops WHERE item_type='gear' AND item_id=?", (gear_id,))
            await self._delete_recipes_by_result('gear', gear_id)
            await self.execute_query("DELETE FROM gear WHERE id=?", (gear_id,))

    async def get_gear_card(self, gear_id: int) -> GearCardRow | None:
        gear_id = await self.resolve_gear_id(gear_id)
        query = """
            SELECT g.id, g.name, g.rarity, g.slot, g.emoji, g.level, g.classes, g.note,
                   (SELECT rc.id FROM recipes rc
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as recipe_id,
                   (SELECT rc.quantity FROM recipes rc
                    WHERE rc.result_type = 'gear' AND rc.result_id = g.id) as craft_quantity,
                   (SELECT json_group_array(json_object(
                       'id', source.id, 'name', source.name, 'emoji', source.emoji
                    )) FROM (
                       SELECT m.id, m.name, m.emoji
                       FROM drops d JOIN mobs m ON d.mob_id = m.id
                       WHERE d.item_type = 'gear' AND d.item_id = g.id
                       ORDER BY LOWER_UNICODE(m.name), m.id
                    ) source) as mobs,
                   (SELECT json_group_array(json_object(
                       'id', source.id, 'name', source.name, 'emoji', source.emoji
                    )) FROM (
                       SELECT m.id, m.name, m.emoji
                       FROM recipes rc
                       JOIN recipe_learning_requirements lr ON lr.recipe_id = rc.id
                       JOIN resources scroll ON scroll.id = lr.scroll_resource_id
                       JOIN drops d ON d.item_type = 'resource' AND d.item_id = scroll.id
                       JOIN mobs m ON m.id = d.mob_id
                       WHERE rc.result_type = 'gear'
                         AND rc.result_id = g.id
                         AND scroll.type = 'scroll_recipe'
                       GROUP BY m.id, m.name, m.emoji
                       ORDER BY LOWER_UNICODE(m.name), m.id
                    ) source) as scroll_mobs,
                   (SELECT json_group_array(json_object(
                       'id', source.resource_id, 'name', source.name,
                       'emoji', source.emoji, 'type', source.type,
                       'quantity', source.quantity
                    )) FROM (
                       SELECT ri.resource_id, r.name, r.emoji, r.type, ri.quantity
                       FROM recipes rc
                       JOIN recipe_ingredients ri ON rc.id = ri.recipe_id
                       JOIN resources r ON ri.resource_id = r.id
                       WHERE rc.result_type = 'gear' AND rc.result_id = g.id
                       ORDER BY LOWER_UNICODE(r.name), r.id
                    ) source) as ingredients,
                   (SELECT json_group_array(json_object(
                        'owner_id', source.owner_id, 'user_id', source.user_id,
                        'player_username', source.player_username
                    )) FROM (
                        SELECT ro.owner_id, ro.user_id, ro.player_username
                       FROM recipe_owners ro
                       JOIN recipes rc ON ro.recipe_id = rc.id
                       WHERE rc.result_type = 'gear' AND rc.result_id = g.id
                       ORDER BY LOWER_UNICODE(ro.player_username)
                    ) source) as owner_entries
            FROM gear g
            WHERE g.id = ?
        """
        res = await self.execute_query(query, (gear_id,))
        if not res:
            return None
        row = res[0]
        owners = [_owner_entry(owner) for owner in _json_rows(row['owner_entries'])]
        learning_scroll = await self.get_recipe_learning_scroll(int(row['recipe_id'])) if row['recipe_id'] is not None else None
        return GearCardRow(
            **_gear_row(row), recipe_id=int(row['recipe_id']) if row['recipe_id'] is not None else None,
            craft_quantity=int(row['craft_quantity']) if row['craft_quantity'] is not None else 1,
            mobs=[_item_row(mob) for mob in _json_rows(row['mobs'])],
            scroll_mobs=[_item_row(mob) for mob in _json_rows(row['scroll_mobs'])],
            ingredients=[GearIngredientRow(
                **_item_row(ingredient), type=str(ingredient['type']), quantity=int(ingredient['quantity']),
            ) for ingredient in _json_rows(row['ingredients'])],
            owners=[owner['player_username'] for owner in owners if owner['player_username']],
            owner_entries=owners,
            owner_user_ids=[owner['user_id'] for owner in owners if owner['user_id'] is not None],
            craftable=row['recipe_id'] is not None, learning_scroll=learning_scroll, can_learn=learning_scroll is not None,
        )

    async def get_prev_next_gear(
        self,
        gear_id: int,
        rarity: str,
        slot: str | None = None,
    ) -> NavigationIds:
        order_by = (
            "level, LOWER_UNICODE(name), id"
            if slot is not None
            else f"{self._slot_order_case()}, LOWER_UNICODE(name), id"
        )
        slot_filter = " AND slot = ?" if slot is not None else ""
        params = (rarity, slot, gear_id) if slot is not None else (rarity, gear_id)
        rows = await self.execute_query(
            f"""
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY {order_by}) AS prev_id,
                       LEAD(id) OVER (ORDER BY {order_by}) AS next_id
                FROM gear
                WHERE rarity = ?{slot_filter}
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            params,
        )
        return NavigationIds(
            prev_id=rows[0]['prev_id'] if rows else None,
            next_id=rows[0]['next_id'] if rows else None,
        )

    async def get_all_gear_simple(self) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, emoji FROM gear "
            f"ORDER BY {self._slot_order_case()}, LOWER_UNICODE(name), id"
        )

    # ========== РЕЦЕПТЫ ==========
    async def get_all_recipes(self, result_type: str, offset: int, limit: int) -> list[DbRow]:
        if result_type == 'gear':
            order_by = f"{self._slot_order_case('g.slot')}, g.name COLLATE NOCASE, r.id"
        else:
            order_by = "r.id"

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
            ORDER BY {order_by} LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (result_type, limit, offset))

    async def get_recipe_owners(self, recipe_id: int) -> list[str]:
        rows = await self.execute_query(
            "SELECT player_username FROM recipe_owners WHERE recipe_id = ? "
            "ORDER BY LOWER_UNICODE(player_username)",
            (recipe_id,),
        )
        return [row['player_username'] for row in rows if row['player_username']]

    async def get_recipe_learning_scroll(self, recipe_id: int) -> ResourceRow | None:
        rows = await self.execute_query(
            'SELECT s.* FROM recipe_learning_requirements lr JOIN resources s ON s.id=lr.scroll_resource_id WHERE lr.recipe_id=?',
            (recipe_id,),
        )
        return _resource_row(rows[0]) if rows else None

    async def get_recipe_details(self, recipe_id: int) -> RecipeDetailsRow | None:
        recipe_rows = await self.execute_query('SELECT * FROM recipes WHERE id=?', (recipe_id,))
        if not recipe_rows:
            return None
        recipe = recipe_rows[0]
        ingredients = await self.execute_query(
            'SELECT ri.resource_id, r.name, r.emoji, ri.quantity FROM recipe_ingredients ri '
            'JOIN resources r ON ri.resource_id=r.id WHERE ri.recipe_id=? ORDER BY ri.resource_id',
            (recipe_id,),
        )
        entries = await self.get_recipe_owner_entries(recipe_id)
        scroll = await self.get_recipe_learning_scroll(recipe_id)
        return RecipeDetailsRow(
            id=int(recipe['id']), result_type=str(recipe['result_type']), result_id=int(recipe['result_id']),
            quantity=int(recipe['quantity']), ingredients=[_recipe_ingredient(row) for row in ingredients],
            owners=[entry['player_username'] for entry in entries if entry['player_username']],
            owner_entries=entries, learning_scroll=scroll, can_learn=scroll is not None, craft_location=str(recipe['craft_location']),
        )

    async def create_recipe(self, result_type: str, result_id: int, quantity: int = 1) -> int:
        tables = {'gear': 'gear', 'resource': 'resources'}
        if result_type not in tables:
            raise DomainError("Недопустимый тип результата рецепта.")
        positive_integer(quantity, 'Количество результата')
        positive_integer(result_id, 'Предмет результата')
        async with self.transaction():
            if result_type == 'resource':
                resource = await self.get_resource_by_id(result_id)
                if resource is not None and resource['type'] == 'scroll_recipe':
                    raise DomainError('Изучаемый свиток не может быть результатом изготовления.')
            if not await self.execute_query(
                f"SELECT 1 FROM {tables[result_type]} WHERE id = ?", (result_id,)
            ):
                raise DomainError("Предмет результата уже удалён. Откройте список заново.")
            if await self.execute_query(
                "SELECT 1 FROM recipes WHERE result_type = ? AND result_id = ?",
                (result_type, result_id),
            ):
                raise DomainError("Для этого предмета рецепт уже существует.")
            return await self.execute_insert(
                "INSERT INTO recipes (result_type, result_id, quantity) VALUES (?, ?, ?)",
                (result_type, result_id, quantity)
            )

    async def update_recipe_quantity(self, recipe_id: int, quantity: int) -> None:
        positive_integer(quantity, 'Количество результата')
        async with self.transaction():
            if not await self.execute_query('SELECT 1 FROM recipes WHERE id=?', (recipe_id,)):
                raise DomainError('Рецепт уже удалён.')
            await self.execute_query('UPDATE recipes SET quantity=? WHERE id=?', (quantity, recipe_id))

    async def update_recipe_craft_location(self, recipe_id: int, craft_location: str) -> None:
        value = validate_craft_location(craft_location)
        async with self.transaction():
            if not await self.execute_query("SELECT 1 FROM recipes WHERE id=? AND result_type='resource'", (recipe_id,)):
                raise DomainError('Место изготовления задаётся для существующего рецепта ресурса.')
            await self.execute_query('UPDATE recipes SET craft_location=? WHERE id=?', (value, recipe_id))

    async def delete_recipe(self, recipe_id: int) -> None:
        async with self.transaction():
            await self.execute_query("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
            await self.execute_query("DELETE FROM recipe_owners WHERE recipe_id=?", (recipe_id,))
            await self.execute_query("DELETE FROM recipes WHERE id=?", (recipe_id,))

    async def add_ingredient(self, recipe_id: int, resource_id: int, quantity: int) -> None:
        positive_integer(quantity, 'Количество ингредиента')
        async with self.transaction():
            if not await self.execute_query("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)):
                raise DomainError("Рецепт уже удалён. Откройте список заново.")
            resource = await self.get_resource_by_id(resource_id)
            if resource is None:
                raise DomainError('Ресурс уже удалён. Откройте список заново.')
            if resource['type'] == 'scroll_recipe':
                raise DomainError('Свиток изучается один раз: укажите его в разделе изучения, а не материалов.')
            if await self.execute_query(
                "SELECT 1 FROM recipe_ingredients WHERE recipe_id = ? AND resource_id = ?",
                (recipe_id, resource_id),
            ):
                raise DomainError("Этот ресурс уже есть в рецепте. Измените его количество через редактирование ингредиентов.")
            await self.execute_query(
                "INSERT INTO recipe_ingredients (recipe_id, resource_id, quantity) VALUES (?, ?, ?)",
                (recipe_id, resource_id, quantity)
            )
            await self._validate_resource_graph()

    async def update_ingredient(self, recipe_id: int, resource_id: int, quantity: int) -> None:
        positive_integer(quantity, 'Количество ингредиента')
        await self.execute_query(
            "UPDATE recipe_ingredients SET quantity=? WHERE recipe_id=? AND resource_id=?",
            (quantity, recipe_id, resource_id)
        )

    async def remove_ingredient(self, recipe_id: int, resource_id: int) -> None:
        async with self.transaction():
            if not await self.execute_query('SELECT 1 FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?', (recipe_id, resource_id)):
                return
            count = await self.execute_query('SELECT COUNT(*) AS n FROM recipe_ingredients WHERE recipe_id=?', (recipe_id,))
            if int(count[0]['n']) <= 1:
                raise DomainError('Нельзя удалить последний материал из опубликованного рецепта. Удалите рецепт целиком или сначала добавьте другой материал.')
            await self.execute_query('DELETE FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?', (recipe_id, resource_id))

    async def get_recipe_owner_entries(self, recipe_id: int) -> list[RecipeOwnerEntry]:
        rows = await self.execute_query(
            "SELECT owner_id, user_id, player_username FROM recipe_owners "
            "WHERE recipe_id = ? ORDER BY LOWER_UNICODE(player_username), owner_id",
            (recipe_id,),
        )
        return [_owner_entry(row) for row in rows]

    async def add_recipe_owner(self, recipe_id: int, player_username: str) -> None:
        username = player_username.strip().lstrip('@')
        if not username:
            raise ValueError("Имя владельца не может быть пустым.")
        await self.execute_query(
            "INSERT OR IGNORE INTO recipe_owners (recipe_id, player_username) VALUES (?, ?)",
            (recipe_id, username)
        )

    async def remove_recipe_owner(self, recipe_id: int, player_username: str) -> None:
        await self.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id = ? AND user_id IS NULL "
            "AND player_username = ? COLLATE NOCASE",
            (recipe_id, player_username.strip().lstrip('@'))
        )

    async def remove_recipe_owner_entry(self, recipe_id: int, owner_id: int) -> None:
        await self.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id = ? AND owner_id = ?",
            (recipe_id, owner_id),
        )

    async def claim_recipe_owner(
        self, recipe_id: int, user_id: int, username: str | None,
        *, expected_gear_id: int | None = None,
    ) -> None:
        async with self.transaction():
            rows = await self.execute_query(
                "SELECT g.id FROM recipes r JOIN gear g "
                "ON r.result_type = 'gear' AND g.id = r.result_id "
                "JOIN recipe_learning_requirements lr ON lr.recipe_id=r.id WHERE r.id = ?",
                (recipe_id,),
            )
            if expected_gear_id is not None:
                expected_gear_id = await self.resolve_gear_id(expected_gear_id)
            if (not rows
                    or (expected_gear_id is not None and rows[0]['id'] != expected_gear_id)):
                raise ValueError("Рецепт изменился или недоступен. Откройте карточку заново.")
            await self.execute_query(
                "INSERT INTO users (user_id, username) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
                "last_activity = CURRENT_TIMESTAMP",
                (user_id, username),
            )
            await self.execute_query(
                "UPDATE recipe_owners SET player_username = ? WHERE user_id = ? AND player_username IS NOT ?",
                (username, user_id, username),
            )
            await self.execute_query(
                "INSERT INTO recipe_owners (recipe_id, user_id, player_username) VALUES (?, ?, ?) "
                "ON CONFLICT(recipe_id, user_id) WHERE user_id IS NOT NULL "
                "DO UPDATE SET player_username = excluded.player_username",
                (recipe_id, user_id, username),
            )

    async def relinquish_recipe_owner(self, recipe_id: int, user_id: int) -> None:
        await self.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id = ? AND user_id = ?",
            (recipe_id, user_id),
        )

    async def get_recipe_for_resource(self, resource_id: int) -> ResourceRecipeRow | None:
        recipe_info = await self.execute_query(
            "SELECT id,quantity,craft_location FROM recipes WHERE result_type = 'resource' AND result_id = ?",
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
        return ResourceRecipeRow(ingredients=[_recipe_ingredient(row) for row in ingredients], quantity=int(recipe_info[0]['quantity']), craft_location=str(recipe_info[0]['craft_location']))

    async def save_resource_recipe(self, result_id: int, quantity: int, materials: list[MaterialInput], *, craft_location: str = '') -> int:
        """Publish a complete resource formula atomically; identical retries reuse its ID."""
        positive_integer(quantity, 'Количество результата')
        craft_location = validate_craft_location(craft_location)
        validated = validate_draft_payload({'materials': materials})['materials']
        if not validated:
            raise DomainError('Добавьте хотя бы один расходуемый материал.')
        async with self.transaction():
            existing = await self.execute_query(
                "SELECT id,quantity,craft_location FROM recipes WHERE result_type='resource' AND result_id=?", (result_id,),
            )
            resolved: list[tuple[int, int]] = []
            for material in validated:
                resource_id = material.get('resource_id')
                if resource_id is None:
                    matching = await self.execute_query(
                        "SELECT id FROM resources WHERE LOWER_UNICODE(TRIM(name))=LOWER_UNICODE(?) AND type='craft'", (material['name'],),
                    )
                    if existing and len(matching) == 1:
                        resource_id = int(matching[0]['id'])
                    else:
                        resource_id = await self._create_named_draft_resource(material['name'], material.get('emoji', ''), 'craft')
                resolved.append((resource_id, material['quantity']))
            if len({item[0] for item in resolved}) != len(resolved):
                raise DomainError('В рецепте повторяется материал.')
            if existing:
                recipe_id = int(existing[0]['id'])
                previous = await self.execute_query('SELECT resource_id,quantity FROM recipe_ingredients WHERE recipe_id=?', (recipe_id,))
                if (existing[0]['quantity'] != quantity or existing[0]['craft_location'] != craft_location or sorted(resolved) != sorted(
                    (int(item['resource_id']), int(item['quantity'])) for item in previous
                )):
                    raise DraftConflictError('Для этого результата уже существует другая формула. Откройте редактирование.')
                return recipe_id
            recipe_id = await self.create_recipe('resource', result_id, quantity)
            await self.update_recipe_craft_location(recipe_id, craft_location)
            for resource_id, amount in resolved:
                await self.add_ingredient(recipe_id, resource_id, amount)
            return recipe_id

    async def _validate_resource_graph(self) -> None:
        rows = await self.execute_query("""
            SELECT rec.result_id, ri.resource_id, ri.quantity
            FROM recipes rec JOIN recipe_ingredients ri ON ri.recipe_id=rec.id
            WHERE rec.result_type='resource'
        """)
        graph: dict[int, set[int]] = {}
        for row in rows:
            positive_integer(row['quantity'], 'Количество материала')
            graph.setdefault(int(row['result_id']), set()).add(int(row['resource_id']))
        # Iterative DFS also handles long chains without Python recursion limits.
        finished: set[int] = set()
        active: set[int] = set()
        for start in graph:
            stack = [(start, False)]
            while stack:
                node, leaving = stack.pop()
                if leaving:
                    active.discard(node)
                    finished.add(node)
                elif node in active:
                    raise DomainError('Рецепт содержит собственный результат или циклическую цепочку материалов.')
                elif node not in finished:
                    active.add(node)
                    stack.append((node, True))
                    stack.extend((child, False) for child in graph.get(node, set()))

    async def set_recipe_learning_scroll(self, recipe_id: int, scroll_resource_id: int | None) -> None:
        async with self.transaction():
            if not await self.execute_query(
                "SELECT 1 FROM recipes r JOIN gear g ON r.result_type='gear' AND g.id=r.result_id WHERE r.id=?",
                (recipe_id,),
            ):
                raise DomainError('Изучение доступно только для существующего рецепта снаряжения.')
            old = await self.get_recipe_learning_scroll(recipe_id)
            old_id = old['id'] if old is not None else None
            if old_id == scroll_resource_id:
                return
            if old_id is not None and await self.get_recipe_owner_entries(recipe_id):
                raise DomainError('У рецепта есть изучившие его игроки. Сначала явно проверьте и удалите эти записи перед сменой изучения.')
            if scroll_resource_id is not None:
                scroll = await self.get_resource_by_id(scroll_resource_id)
                if scroll is None or scroll['type'] != 'scroll_recipe':
                    raise DomainError('Выберите существующий ресурс типа «рецепт» для изучения.')
                dependencies = await self.get_resource_dependencies(scroll_resource_id)
                if (dependencies['ingredient_recipe_ids'] or dependencies['result_recipe_ids']
                        or any(item != recipe_id for item in dependencies['learning_recipe_ids'])):
                    raise DomainError('Свиток уже связан с другим рецептом или расходуемыми материалами.')
            await self.execute_query('DELETE FROM recipe_learning_requirements WHERE recipe_id=?', (recipe_id,))
            if scroll_resource_id is not None:
                await self.execute_query(
                    'INSERT INTO recipe_learning_requirements(recipe_id,scroll_resource_id) VALUES (?,?)',
                    (recipe_id, scroll_resource_id),
                )

    async def delete_recipe_bundle(self, recipe_id: int, *, delete_scroll: bool = False) -> None:
        """Explicit formula deletion; optional scroll deletion includes its drop links."""
        async with self.transaction():
            scroll = await self.get_recipe_learning_scroll(recipe_id)
            await self.delete_recipe(recipe_id)
            if delete_scroll and scroll is not None:
                dependencies = await self.get_resource_dependencies(scroll['id'])
                if (dependencies['ingredient_recipe_ids'] or dependencies['learning_recipe_ids']
                        or dependencies['result_recipe_ids']):
                    raise DomainError('Свиток используется в других рецептах; удаление отменено.')
                await self.execute_query("DELETE FROM drops WHERE item_type='resource' AND item_id=?", (scroll['id'],))
                await self.delete_resource(scroll['id'])

    async def resolve_gear_id(self, gear_id: int) -> int:
        rows = await self.execute_query('SELECT canonical_gear_id FROM gear_aliases WHERE alias_id=?', (gear_id,))
        return int(rows[0]['canonical_gear_id']) if rows else gear_id

    async def merge_gear(self, source_gear_id: int, target_gear_id: int) -> int:
        """Merge verified duplicate profiles, preserving old public IDs as aliases."""
        async with self.transaction():
            source_id = await self.resolve_gear_id(source_gear_id)
            target_id = await self.resolve_gear_id(target_gear_id)
            if source_id == target_id:
                return target_id
            source = await self.get_gear_by_id(source_id)
            target = await self.get_gear_by_id(target_id)
            if source is None or target is None:
                raise DomainError('Один из объединяемых предметов уже удалён.')
            left = validate_draft_payload({key: value for key, value in source.items() if key != 'id'})
            right = validate_draft_payload({key: value for key, value in target.items() if key != 'id'})
            if (source['name'].strip().casefold() != target['name'].strip().casefold()
                    or any(left.get(key) != right.get(key) for key in ('rarity', 'slot', 'level', 'classes', 'note'))):
                raise DomainError('Предметы отличаются уровнем, редкостью, слотом, классами или описанием.')
            source_recipes = await self.execute_query("SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (source_id,))
            target_recipes = await self.execute_query("SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (target_id,))
            if source_recipes and target_recipes:
                raise DomainError('Оба предмета имеют рецепты. Требуется отдельное разрешение конфликта формул и изучения.')
            await self.execute_query("UPDATE recipes SET result_id=? WHERE result_type='gear' AND result_id=?", (target_id, source_id))
            await self.execute_query(
                "INSERT OR IGNORE INTO drops(mob_id,item_type,item_id) SELECT mob_id,'gear',? FROM drops WHERE item_type='gear' AND item_id=?",
                (target_id, source_id),
            )
            await self.execute_query("DELETE FROM drops WHERE item_type='gear' AND item_id=?", (source_id,))
            await self.execute_query('UPDATE gear_aliases SET canonical_gear_id=? WHERE canonical_gear_id=?', (target_id, source_id))
            await self.execute_query('INSERT INTO gear_aliases(alias_id,canonical_gear_id) VALUES (?,?)', (source_id, target_id))
            await self.execute_query('DELETE FROM gear WHERE id=?', (source_id,))
            return target_id

    async def get_gear_draft_payload(self, gear_id: int) -> GearDraftPayload | None:
        gear = await self.get_gear_by_id(gear_id)
        if gear is None:
            return None
        payload = GearDraftPayload(
            gear_id=gear['id'], name=gear['name'], rarity=gear['rarity'], slot=gear['slot'],
            emoji=gear['emoji'], level=gear['level'], classes=gear['classes'], note=gear['note'],
            craftable=False, quantity=1, materials=[], learning_scroll=None,
            gear_mob_ids=[int(row['mob_id']) for row in await self.execute_query(
                "SELECT mob_id FROM drops WHERE item_type='gear' AND item_id=? ORDER BY mob_id", (gear['id'],))],
            scroll_mob_ids=[],
        )
        rows = await self.execute_query("SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear['id'],))
        if rows:
            recipe = await self.get_recipe_details(int(rows[0]['id']))
            if recipe is not None:
                payload['craftable'] = True
                payload['quantity'] = recipe['quantity']
                payload['materials'] = [MaterialInput(resource_id=item['resource_id'], quantity=item['quantity']) for item in recipe['ingredients']]
                scroll = recipe['learning_scroll']
                if scroll is not None:
                    payload['learning_scroll'] = LearningScrollInput(resource_id=scroll['id'])
                    payload['scroll_mob_ids'] = [int(row['mob_id']) for row in await self.execute_query(
                        "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id", (scroll['id'],))]
        return payload

    @staticmethod
    def _payload_fingerprint(payload: GearDraftPayload) -> str:
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()

    async def _gear_owner_fingerprint(self, gear_id: int) -> str:
        rows = await self.execute_query(
            "SELECT ro.owner_id,ro.user_id,ro.player_username FROM recipe_owners ro JOIN recipes r ON r.id=ro.recipe_id WHERE r.result_type='gear' AND r.result_id=? ORDER BY ro.owner_id",
            (gear_id,),
        )
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()

    async def _scroll_reference(self, scroll_id: int) -> tuple[str, list[int]]:
        resource = await self.get_resource_by_id(scroll_id)
        if resource is None or resource['type'] != 'scroll_recipe':
            raise DomainError('Выбранный свиток удалён или изменил тип.')
        drops = [int(row['mob_id']) for row in await self.execute_query(
            "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id", (scroll_id,),
        )]
        fingerprint = hashlib.sha256(json.dumps(
            {'resource': resource, 'drops': drops}, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        return fingerprint, drops

    async def _prepare_draft_references(self, payload: GearDraftPayload, previous: DbRow | None = None) -> str:
        scroll = payload.get('learning_scroll')
        scroll_id = scroll.get('resource_id') if scroll is not None else None
        if scroll_id is None:
            return '{}'
        if previous is not None:
            previous_scroll = self._decode_draft(previous)['payload'].get('learning_scroll')
            previous_id = previous_scroll.get('resource_id') if previous_scroll is not None else None
            if previous_id == scroll_id:
                return str(previous['reference_fingerprints_json'])
        fingerprint, drop_ids = await self._scroll_reference(scroll_id)
        # Selecting a physical scroll starts from its current sources. Further
        # source edits preserve this fingerprint and are compared at final save.
        payload['scroll_mob_ids'] = drop_ids
        return json.dumps({str(scroll_id): fingerprint}, sort_keys=True)

    async def _check_draft_references(self, row: DbRow, payload: GearDraftPayload) -> None:
        scroll = payload.get('learning_scroll')
        scroll_id = scroll.get('resource_id') if scroll is not None else None
        if scroll_id is None:
            return
        expected: object = json.loads(str(row['reference_fingerprints_json']))
        fingerprint, _ = await self._scroll_reference(scroll_id)
        if not isinstance(expected, dict) or expected.get(str(scroll_id)) != fingerprint:
            raise DraftConflictError('Выбранный свиток или его источники изменены другим редактором. Откройте новый черновик с актуальными данными.')

    @staticmethod
    def _decode_draft(row: DbRow) -> GearDraft:
        payload = validate_draft_payload(json.loads(str(row['payload_json'])))
        saved: GearSaveResult | None = None
        if row['saved_result_json'] is not None:
            value: object = json.loads(str(row['saved_result_json']))
            if not isinstance(value, dict) or not isinstance(value.get('draft_id'), str):
                raise DomainError('Повреждён результат сохранения черновика.')
            saved = GearSaveResult(
                draft_id=value['draft_id'], gear_id=positive_integer(value.get('gear_id'), 'Снаряжение'),
                recipe_id=positive_integer(value['recipe_id'], 'Рецепт') if value.get('recipe_id') is not None else None,
                scroll_resource_id=positive_integer(value['scroll_resource_id'], 'Свиток') if value.get('scroll_resource_id') is not None else None,
            )
        status = row['status']
        if status not in ('editing', 'saved', 'cancelled'):
            raise DomainError('Повреждён статус черновика.')
        draft: GearDraft = dict(
            draft_id=str(row['draft_id']), owner_user_id=int(row['owner_user_id']), chat_id=int(row['chat_id']),
            message_id=int(row['message_id']), revision=int(row['revision']), status='editing',
            payload=payload, saved_result=saved,
        )
        if status == 'saved':
            draft['status'] = 'saved'
        elif status == 'cancelled':
            draft['status'] = 'cancelled'
        return draft

    async def _context_draft(self, draft_id: str, owner_user_id: int, chat_id: int, message_id: int | None = None) -> DbRow:
        rows = await self.execute_query('SELECT * FROM gear_drafts WHERE draft_id=?', (draft_id,))
        if (not rows or rows[0]['owner_user_id'] != owner_user_id or rows[0]['chat_id'] != chat_id
                or (message_id is not None and rows[0]['message_id'] != message_id)):
            raise DraftConflictError('Черновик недоступен или открыт из другого сообщения. Откройте его заново.')
        return rows[0]

    @staticmethod
    def _check_draft_revision(row: DbRow, expected_revision: int) -> None:
        if row['status'] != 'editing' or row['revision'] != expected_revision:
            raise DraftConflictError('Черновик уже изменён или завершён. Откройте актуальный экран.')

    async def create_gear_draft(
        self, *, owner_user_id: int, chat_id: int, message_id: int,
        payload: GearDraftPayload | None = None, gear_id: int | None = None,
    ) -> GearDraft:
        positive_integer(owner_user_id, 'Администратор')
        positive_integer(message_id, 'Сообщение')
        async with self.transaction():
            original = await self.get_gear_draft_payload(gear_id) if gear_id is not None else None
            if gear_id is not None and original is None:
                raise DomainError('Снаряжение уже удалено.')
            value = validate_draft_payload(payload if payload is not None else (original or {}))
            target_id = original['gear_id'] if original is not None else None
            if value.get('gear_id') != target_id:
                raise DraftConflictError('Нельзя менять предмет, которому принадлежит черновик.')
            references = await self._prepare_draft_references(value)
            draft_id = secrets.token_hex(8)
            await self.execute_query(
                'INSERT INTO gear_drafts(draft_id,owner_user_id,chat_id,message_id,target_gear_id,base_fingerprint,base_owner_fingerprint,reference_fingerprints_json,payload_json) VALUES (?,?,?,?,?,?,?,?,?)',
                (draft_id, owner_user_id, chat_id, message_id, target_id,
                 self._payload_fingerprint(original) if original is not None else None,
                 await self._gear_owner_fingerprint(target_id) if target_id is not None else None, references,
                 json.dumps(value, ensure_ascii=False)),
            )
            return self._decode_draft(await self._context_draft(draft_id, owner_user_id, chat_id))

    async def get_gear_draft(self, draft_id: str, *, owner_user_id: int, chat_id: int) -> GearDraft | None:
        try:
            row = await self._context_draft(draft_id, owner_user_id, chat_id)
        except DraftConflictError:
            return None
        return self._decode_draft(row)

    async def list_gear_drafts(self, *, owner_user_id: int, chat_id: int) -> list[GearDraft]:
        rows = await self.execute_query(
            "SELECT * FROM gear_drafts WHERE owner_user_id=? AND chat_id=? AND status='editing' ORDER BY updated_at DESC,draft_id",
            (owner_user_id, chat_id),
        )
        return [self._decode_draft(row) for row in rows]

    async def update_gear_draft(
        self, draft_id: str, *, expected_revision: int, owner_user_id: int,
        chat_id: int, message_id: int, payload: GearDraftPayload,
    ) -> GearDraft:
        value = validate_draft_payload(payload)
        async with self.transaction():
            row = await self._context_draft(draft_id, owner_user_id, chat_id, message_id)
            self._check_draft_revision(row, expected_revision)
            if value.get('gear_id') != row['target_gear_id']:
                raise DraftConflictError('Нельзя менять предмет, которому принадлежит черновик.')
            references = await self._prepare_draft_references(value, row)
            await self.execute_query(
                'UPDATE gear_drafts SET payload_json=?,reference_fingerprints_json=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?',
                (json.dumps(value, ensure_ascii=False), references, draft_id),
            )
            return self._decode_draft(await self._context_draft(draft_id, owner_user_id, chat_id))

    async def bind_gear_draft_message(
        self, draft_id: str, *, old_message_id: int, new_message_id: int,
        expected_revision: int, owner_user_id: int, chat_id: int,
    ) -> GearDraft:
        positive_integer(new_message_id, 'Сообщение')
        async with self.transaction():
            row = await self._context_draft(draft_id, owner_user_id, chat_id, old_message_id)
            self._check_draft_revision(row, expected_revision)
            await self.execute_query(
                'UPDATE gear_drafts SET message_id=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?',
                (new_message_id, draft_id),
            )
            return self._decode_draft(await self._context_draft(draft_id, owner_user_id, chat_id))

    async def cancel_gear_draft(
        self, draft_id: str, *, expected_revision: int, owner_user_id: int, chat_id: int, message_id: int,
    ) -> GearDraft:
        async with self.transaction():
            row = await self._context_draft(draft_id, owner_user_id, chat_id, message_id)
            self._check_draft_revision(row, expected_revision)
            await self.execute_query(
                "UPDATE gear_drafts SET status='cancelled',revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?", (draft_id,),
            )
            return self._decode_draft(await self._context_draft(draft_id, owner_user_id, chat_id))

    async def delete_gear_draft_target(
        self, draft_id: str, *, expected_revision: int, owner_user_id: int,
        chat_id: int, message_id: int, delete_gear: bool, delete_scroll: bool = False,
    ) -> GearDraft:
        """Delete a reviewed draft target only if its aggregate still matches."""
        async with self.transaction():
            row = await self._context_draft(draft_id, owner_user_id, chat_id, message_id)
            if row['status'] == 'cancelled':
                return self._decode_draft(row)
            self._check_draft_revision(row, expected_revision)
            if row['target_gear_id'] is None:
                raise DomainError('У нового черновика пока нет опубликованного предмета.')
            gear_id = int(row['target_gear_id'])
            current = await self.get_gear_draft_payload(gear_id)
            if current is None or self._payload_fingerprint(current) != row['base_fingerprint']:
                raise DraftConflictError('Предмет изменён другим редактором. Откройте актуальную карточку перед удалением.')
            if await self._gear_owner_fingerprint(gear_id) != row['base_owner_fingerprint']:
                raise DraftConflictError('Список изучивших рецепт изменился. Откройте актуальную карточку перед удалением.')
            recipes = await self.execute_query("SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear_id,))
            if recipes:
                await self.delete_recipe_bundle(int(recipes[0]['id']), delete_scroll=delete_scroll)
            if delete_gear:
                await self.delete_gear(gear_id)
            await self.execute_query("UPDATE gear_drafts SET status='cancelled',revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?", (draft_id,))
            return self._decode_draft(await self._context_draft(draft_id, owner_user_id, chat_id))

    async def _create_named_draft_resource(self, name: str, emoji: str, resource_type: str, note: str = '') -> int:
        if await self.execute_query('SELECT 1 FROM resources WHERE LOWER_UNICODE(TRIM(name))=LOWER_UNICODE(?)', (name,)):
            raise DomainError(f'Ресурс «{name}» уже существует. Выберите его из списка, чтобы сохранить связи.')
        return await self.add_resource(name, emoji, resource_type, note)

    async def _replace_draft_drops(self, item_type: str, item_id: int, mob_ids: list[int]) -> None:
        await self.execute_query('DELETE FROM drops WHERE item_type=? AND item_id=?', (item_type, item_id))
        for mob_id in mob_ids:
            await self.add_drop(mob_id, item_type, item_id)

    async def save_gear_draft(
        self, draft_id: str, *, expected_revision: int, owner_user_id: int, chat_id: int, message_id: int,
    ) -> GearSaveResult:
        async with self.transaction():
            row = await self._context_draft(draft_id, owner_user_id, chat_id, message_id)
            draft = self._decode_draft(row)
            if draft['status'] == 'saved' and draft['saved_result'] is not None:
                return draft['saved_result']
            self._check_draft_revision(row, expected_revision)
            value = validate_draft_payload(draft['payload'], complete=True)
            await self._check_draft_references(row, value)
            gear_id = value.get('gear_id')
            current: GearDraftPayload | None = None
            if gear_id != row['target_gear_id']:
                raise DraftConflictError('Изменился предмет черновика.')
            if gear_id is not None:
                current = await self.get_gear_draft_payload(gear_id)
                if current is None or self._payload_fingerprint(current) != row['base_fingerprint']:
                    raise DraftConflictError('Предмет изменён другим редактором. Создайте новый черновик, чтобы не потерять изменения.')
            for mob_id in set(value['gear_mob_ids'] + value['scroll_mob_ids']):
                if not await self.execute_query('SELECT 1 FROM mobs WHERE id=?', (mob_id,)):
                    raise DomainError(f'Источник {mob_id} уже удалён. Обновите список источников.')
            identity_changed = current is None or (
                value['name'].casefold(), value['rarity'], value['slot'], value['level']
            ) != (current['name'].strip().casefold(), current['rarity'], current['slot'], current['level'])
            if identity_changed and await self.execute_query(
                'SELECT 1 FROM gear WHERE LOWER_UNICODE(TRIM(name))=LOWER_UNICODE(?) AND rarity=? AND slot=? AND level=? AND id IS NOT ?',
                (value['name'], value['rarity'], value['slot'], value['level'], gear_id),
            ):
                raise DomainError('Такое снаряжение этого уровня уже существует. Откройте его редактирование.')
            if gear_id is None:
                gear_id = await self.add_gear(value['name'], value['rarity'], value['slot'], value['emoji'], value['level'], value['classes'], value['note'])
            else:
                await self.update_gear(gear_id, value['name'], value['rarity'], value['slot'], value['emoji'], value['level'], value['classes'], value['note'])
            rows = await self.execute_query("SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear_id,))
            recipe_id = int(rows[0]['id']) if rows else None
            scroll_id: int | None = None
            if value['craftable']:
                if recipe_id is None:
                    recipe_id = await self.create_recipe('gear', gear_id, value['quantity'])
                else:
                    await self.execute_query('UPDATE recipes SET quantity=? WHERE id=?', (value['quantity'], recipe_id))
                materials: list[tuple[int, int]] = []
                for material in value['materials']:
                    resource_id = material.get('resource_id')
                    if resource_id is None:
                        resource_id = await self._create_named_draft_resource(material['name'], material.get('emoji', ''), 'craft')
                    resource = await self.get_resource_by_id(resource_id)
                    if resource is None or resource['type'] == 'scroll_recipe':
                        raise DomainError('Материал удалён или является изучаемым свитком.')
                    materials.append((resource_id, material['quantity']))
                if len({item[0] for item in materials}) != len(materials):
                    raise DomainError('В рецепте повторяется материал.')
                await self.execute_query('DELETE FROM recipe_ingredients WHERE recipe_id=?', (recipe_id,))
                for resource_id, quantity in materials:
                    await self.add_ingredient(recipe_id, resource_id, quantity)
                scroll = value['learning_scroll']
                if scroll is not None:
                    scroll_id = scroll.get('resource_id')
                    if scroll_id is None:
                        scroll_name = scroll.get('name', f"Рецепт ({value['name']})")
                        # The automatic label must obey the same limits as explicitly entered text.
                        validate_draft_payload({'learning_scroll': {'name': scroll_name}})
                        scroll_id = await self._create_named_draft_resource(scroll_name, scroll.get('emoji', '📜'), 'scroll_recipe', scroll.get('note', ''))
                await self.set_recipe_learning_scroll(recipe_id, scroll_id)
                if scroll_id is not None:
                    await self._replace_draft_drops('resource', scroll_id, value['scroll_mob_ids'])
            elif recipe_id is not None:
                raise DomainError('Рецепт уже существует. Для его удаления используйте явное удаление рецепта.')
            await self._replace_draft_drops('gear', gear_id, value['gear_mob_ids'])
            result = GearSaveResult(draft_id=draft_id, gear_id=gear_id, recipe_id=recipe_id, scroll_resource_id=scroll_id)
            await self.execute_query(
                "UPDATE gear_drafts SET status='saved',saved_result_json=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (json.dumps(result, ensure_ascii=False), draft_id),
            )
            return result

    # ========== КАРТЫ ==========
    async def get_cards_page(self, offset: int, limit: int) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, emoji, slot FROM cards ORDER BY id LIMIT ? OFFSET ?",
            (limit, offset)
        )

    async def get_all_cards_sorted_by_slot(self, offset: int, limit: int) -> list[DbRow]:
        case_expression = self._slot_order_case()
        
        query = f"""
            SELECT id, name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note
            FROM cards
            ORDER BY {case_expression}, name COLLATE NOCASE
            LIMIT ? OFFSET ?
        """
        return await self.execute_query(query, (limit, offset))

    async def get_card_by_id(self, card_id: int) -> CardRow | None:
        res = await self.execute_query("SELECT * FROM cards WHERE id = ?", (card_id,))
        if not res:
            return None
        row = res[0]
        return CardRow(
            id=int(row['id']), name=str(row['name']), emoji=str(row['emoji']),
            slot=str(row['slot']), bonus1=str(row['bonus1']), bonus2=str(row['bonus2']),
            bonus3=str(row['bonus3']), bonus4=str(row['bonus4']), note=str(row['note']),
        )

    async def add_card(self, name: str, emoji: str, slot: str,
                       bonus1: str = '', bonus2: str = '', bonus3: str = '', bonus4: str = '',
                       note: str = '') -> int:
        return await self.execute_insert(
            """INSERT INTO cards (name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note)
        )

    async def update_card(self, card_id: int, **kwargs: str) -> None:
        allowed = {'name', 'emoji', 'slot', 'bonus1', 'bonus2', 'bonus3', 'bonus4', 'note'}
        updates = [(field, value) for field, value in kwargs.items() if field in allowed]
        if not updates:
            return
        assignments = ", ".join(f"{field} = ?" for field, _ in updates)
        params = tuple(value for _, value in updates) + (card_id,)
        await self.execute_query(f"UPDATE cards SET {assignments} WHERE id = ?", params)

    async def delete_card(self, card_id: int) -> None:
        async with self.transaction():
            await self.execute_query("DELETE FROM drops WHERE item_type='card' AND item_id=?", (card_id,))
            await self._delete_recipes_by_result('card', card_id)
            await self.execute_query("DELETE FROM cards WHERE id=?", (card_id,))

    async def get_card_drop_mobs(self, card_id: int) -> list[ResourceDropMobRow]:
        rows = await self.execute_query(
            """SELECT m.id, m.name, m.emoji, l.id as location_id, l.name as location_name, l.emoji as location_emoji
               FROM drops d
               JOIN mobs m ON d.mob_id = m.id
               JOIN locations l ON m.location_id = l.id
               WHERE d.item_type='card' AND d.item_id=?
               ORDER BY m.id""",
            (card_id,),
        )
        return [ResourceDropMobRow(
            **_item_row(row), location_id=int(row['location_id']),
            location_name=str(row['location_name']), location_emoji=str(row['location_emoji']),
        ) for row in rows]

    async def get_prev_next_card_by_slot(self, card_id: int) -> NavigationIds:
        case_expression = self._slot_order_case()
        
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
        return NavigationIds(
            prev_id=rows[0]['prev_id'] if rows else None,
            next_id=rows[0]['next_id'] if rows else None,
        )

    # ========== ДРОПЫ ==========
    async def search_drop_items(self, mob_id: int, query: str, limit: int = 20) -> list[DbRow]:
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

    async def get_enabled_drop_ids(
        self,
        mob_id: int,
        item_type: str,
        item_ids: list[int],
    ) -> set[int]:
        if not item_ids:
            return set()
        placeholders = ", ".join("?" for _ in item_ids)
        rows = await self.execute_query(
            f"SELECT item_id FROM drops "
            f"WHERE mob_id = ? AND item_type = ? AND item_id IN ({placeholders})",
            (mob_id, item_type, *item_ids),
        )
        return {int(row['item_id']) for row in rows}

    async def get_drop_status(self, mob_id: int, item_type: str, item_id: int) -> bool:
        return item_id in await self.get_enabled_drop_ids(mob_id, item_type, [item_id])

    async def add_drop(self, mob_id: int, item_type: str, item_id: int) -> None:
        try:
            async with self.transaction():
                if item_type == 'resource':
                    check = await self.execute_query("SELECT 1 FROM resources WHERE id = ?", (item_id,))
                elif item_type == 'gear':
                    item_id = await self.resolve_gear_id(item_id)
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

    async def remove_drop(self, mob_id: int, item_type: str, item_id: int) -> None:
        await self.execute_query(
            "DELETE FROM drops WHERE mob_id = ? AND item_type = ? AND item_id = ?",
            (mob_id, item_type, item_id)
        )

    async def get_resources_page(self, offset: int, limit: int) -> list[DbRow]:
        return await self.execute_query(
            "SELECT id, name, emoji, type FROM resources "
            f"ORDER BY {self.RESOURCE_NAME_ORDER} LIMIT ? OFFSET ?",
            (limit, offset)
        )

db = Database()
