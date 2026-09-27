"""Versioned schema upgrades, independent from runtime catalog commands."""

from __future__ import annotations
from storage.types import sql_int, sql_text
from typing import TYPE_CHECKING
from game_constants import LEGACY_ALCHEMY_CRAFT_LOCATIONS, LEGACY_DEFAULT_ALCHEMY_CRAFT_LOCATION
from recipe_domain import DomainError, positive_integer
from storage.types import SCHEMA_VERSION

if TYPE_CHECKING:
    from database import Database


class SchemaRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def _migrate_schema(self) -> None:
        """Upgrade legacy owner rows atomically without assigning user identities."""
        async with self.db.transaction():
            version = sql_int((await self.db.execute_query("PRAGMA user_version"))[0]["user_version"])
            if version >= SCHEMA_VERSION:
                return
            columns = {row["name"] for row in await self.db.execute_query("PRAGMA table_info(recipe_owners)")}
            if not {"owner_id", "user_id"}.issubset(columns):
                await self.db.execute_query("""
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
                await self.db.execute_query("""
                    INSERT INTO recipe_owners_v1 (recipe_id, player_username)
                    SELECT recipe_id, MIN(player_username)
                    FROM recipe_owners
                    GROUP BY recipe_id, player_username COLLATE NOCASE
                """)
                await self.db.execute_query("DROP TABLE recipe_owners")
                await self.db.execute_query("ALTER TABLE recipe_owners_v1 RENAME TO recipe_owners")
            await self.db.execute_query("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recipe_owners_user
                ON recipe_owners(recipe_id, user_id) WHERE user_id IS NOT NULL
            """)
            await self.db.execute_query("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_recipe_owners_manual
                ON recipe_owners(recipe_id, player_username COLLATE NOCASE)
                WHERE user_id IS NULL
            """)
            if version < 2:
                await self.db._migrate_learning_and_drafts()
            await self.db._migrate_catalog_metadata()
            await self.db.execute_query(f"PRAGMA user_version = {SCHEMA_VERSION}")

    async def _migrate_learning_and_drafts(self) -> None:
        recipe_columns = {sql_text(row["name"]) for row in await self.db.execute_query("PRAGMA table_info(recipes)")}
        if "craft_location" not in recipe_columns:
            await self.db.execute_query("ALTER TABLE recipes ADD COLUMN craft_location TEXT NOT NULL DEFAULT ''")
            for recipe in await self.db.execute_query(
                "SELECT rec.id,s.name FROM recipes rec JOIN resources s ON rec.result_type='resource' AND s.id=rec.result_id",
            ):
                craft_location = LEGACY_ALCHEMY_CRAFT_LOCATIONS.get(
                    sql_text(recipe["name"]).casefold(), LEGACY_DEFAULT_ALCHEMY_CRAFT_LOCATION
                )
                await self.db.execute_query(
                    "UPDATE recipes SET craft_location=? WHERE id=?", (craft_location, recipe["id"])
                )
        # Validate before moving any legacy edge: one scroll teaches one formula.
        legacy = await self.db.execute_query("""
            SELECT ri.recipe_id, ri.resource_id, ri.quantity, rec.result_type, g.id AS gear_id
            FROM recipe_ingredients ri
            JOIN resources s ON s.id = ri.resource_id AND s.type = 'scroll_recipe'
            JOIN recipes rec ON rec.id = ri.recipe_id
            LEFT JOIN gear g ON rec.result_type = 'gear' AND g.id = rec.result_id
        """)
        if (
            any(row["quantity"] != 1 or row["result_type"] != "gear" or row["gear_id"] is None for row in legacy)
            or len({row["recipe_id"] for row in legacy}) != len(legacy)
            or len({row["resource_id"] for row in legacy}) != len(legacy)
        ):
            raise DomainError("Неоднозначные старые связи изучаемых свитков; миграция отменена.")
        if await self.db.execute_query("""
            SELECT 1 FROM recipes rec JOIN resources s
            ON rec.result_type = 'resource' AND rec.result_id = s.id
            WHERE s.type = 'scroll_recipe' LIMIT 1
        """):
            raise DomainError("Найден рецепт изготовления изучаемого свитка; требуется проверка данных.")
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
            await self.db.execute_query(sql)
        for row in legacy:
            await self.db.execute_query(
                "INSERT INTO recipe_learning_requirements(recipe_id,scroll_resource_id) VALUES (?,?)",
                (row["recipe_id"], row["resource_id"]),
            )
            await self.db.execute_query(
                "DELETE FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?",
                (row["recipe_id"], row["resource_id"]),
            )
        await self.db._validate_resource_graph()
        for row in await self.db.execute_query("SELECT quantity FROM recipes"):
            positive_integer(row["quantity"], "Количество результата")
        # Existing resource foreign keys used CASCADE. A material must never disappear
        # silently when a resource is deleted, including through direct maintenance SQL.
        await self.db.execute_query("""CREATE TABLE recipe_ingredients_v2 (
            recipe_id INTEGER NOT NULL,
            resource_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
            PRIMARY KEY(recipe_id,resource_id),
            FOREIGN KEY(recipe_id) REFERENCES recipes(id) ON DELETE CASCADE,
            FOREIGN KEY(resource_id) REFERENCES resources(id) ON DELETE RESTRICT
        )""")
        await self.db.execute_query(
            "INSERT INTO recipe_ingredients_v2 SELECT recipe_id,resource_id,quantity FROM recipe_ingredients"
        )
        await self.db.execute_query("DROP TABLE recipe_ingredients")
        await self.db.execute_query("ALTER TABLE recipe_ingredients_v2 RENAME TO recipe_ingredients")

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
        connection = self.db._require_connection()
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
        connection = self.db._require_connection()
        for sql in indexes:
            await connection.execute(sql)
        await connection.commit()

    async def _migrate_catalog_metadata(self) -> None:
        resource_columns = {row["name"] for row in await self.db.execute_query("PRAGMA table_info(resources)")}
        if "code" not in resource_columns:
            await self.db.execute_query("ALTER TABLE resources ADD COLUMN code TEXT")
        location_columns = {row["name"] for row in await self.db.execute_query("PRAGMA table_info(locations)")}
        if "parent_id" not in location_columns:
            await self.db.execute_query(
                "ALTER TABLE locations ADD COLUMN parent_id INTEGER REFERENCES locations(id) ON DELETE RESTRICT"
            )
        draft_columns = {row["name"] for row in await self.db.execute_query("PRAGMA table_info(gear_drafts)")}
        if "published_reference_fingerprints_json" not in draft_columns:
            await self.db.execute_query(
                "ALTER TABLE gear_drafts ADD COLUMN published_reference_fingerprints_json TEXT NOT NULL DEFAULT '{}'"
            )
        for statement in (
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_resources_code ON resources(code) WHERE code IS NOT NULL",
            "CREATE INDEX IF NOT EXISTS idx_locations_parent ON locations(parent_id,id)",
            "CREATE TABLE IF NOT EXISTS catalog_metadata (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 0)",
            "INSERT OR IGNORE INTO catalog_metadata(id,revision) VALUES (1,0)",
            """CREATE TABLE IF NOT EXISTS catalog_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER, operation_id TEXT NOT NULL,
                source TEXT NOT NULL, table_name TEXT NOT NULL, entity_key TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('insert','update','delete')),
                before_json TEXT, after_json TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            "CREATE INDEX IF NOT EXISTS idx_catalog_changes_operation ON catalog_changes(operation_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_catalog_changes_created ON catalog_changes(created_at,id)",
            "CREATE TABLE IF NOT EXISTS fsm_sessions (key TEXT PRIMARY KEY,state TEXT,data_json TEXT NOT NULL DEFAULT '{}',expires_at REAL NOT NULL)",
            "CREATE INDEX IF NOT EXISTS idx_fsm_sessions_expiry ON fsm_sessions(expires_at)",
            """CREATE TABLE IF NOT EXISTS command_results (
                operation_key TEXT PRIMARY KEY, result_type TEXT NOT NULL, result_id INTEGER NOT NULL,
                request_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        ):
            await self.db.execute_query(statement)
        await self.db._install_catalog_audit()
        # Legacy IDs are migration evidence, never runtime identity. Check full labels
        # to avoid assigning semantics to unrelated catalogs that reuse these IDs.
        await self.db.execute_query(
            "UPDATE resources SET code='dust' WHERE id=71 AND name='Пыль' AND type='currency' AND code IS NULL AND NOT EXISTS(SELECT 1 FROM resources WHERE code='dust')"
        )
        for location_id, name in ((8, "Пещера"), (9, "Подземная пещера"), (10, "Темный грот")):
            await self.db.execute_query(
                "UPDATE locations SET parent_id=4 WHERE id=? AND name=? AND parent_id IS NULL AND EXISTS(SELECT 1 FROM locations WHERE id=4 AND name='Мертвый лес')",
                (location_id, name),
            )

    async def _install_catalog_audit(self) -> None:
        tables = (
            "locations",
            "mobs",
            "resources",
            "gear",
            "cards",
            "drops",
            "recipes",
            "recipe_ingredients",
            "recipe_learning_requirements",
            "recipe_owners",
            "gear_aliases",
        )
        for table in tables:
            columns = await self.db.execute_query(f"PRAGMA table_info({table})")
            names = [sql_text(column["name"]) for column in columns]
            keys = [sql_text(column["name"]) for column in columns if column["pk"]]

            def snapshot(prefix: str, fields: list[str]) -> str:
                arguments = ",".join(
                    "'" + field.replace("'", "''") + "'," + prefix + '."' + field.replace('"', '""') + '"'
                    for field in fields
                )
                return f"json_object({arguments})"

            for action in ("insert", "update", "delete"):
                before = "NULL" if action == "insert" else snapshot("OLD", names)
                after = "NULL" if action == "delete" else snapshot("NEW", names)
                key = snapshot("OLD" if action == "delete" else "NEW", keys)
                condition = ""
                if action == "update":
                    condition = " WHEN " + " OR ".join(
                        'OLD."' + field.replace('"', '""') + '" IS NOT NEW."' + field.replace('"', '""') + '"'
                        for field in names
                    )
                await self.db.execute_query(f"""CREATE TRIGGER IF NOT EXISTS audit_{table}_{action}
                    AFTER {action.upper()} ON {table}{condition} BEGIN
                    INSERT INTO catalog_changes(actor_user_id,operation_id,source,table_name,entity_key,action,before_json,after_json)
                    VALUES (catalog_actor(),catalog_operation_id(),catalog_source(),'{table}',{key},'{action}',{before},{after});
                    UPDATE catalog_metadata SET revision=revision+1 WHERE id=1;
                    END""")
