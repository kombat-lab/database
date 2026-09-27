from __future__ import annotations
from storage.types import sql_int, sql_text
from catalog_types import (
    DropItemType,
    MobSourceRow,
)
from storage.rows import _item_row
from storage.types import DbRow, SqlParams
from recipe_domain import (
    DomainError,
    MAX_SQLITE_ID,
    positive_integer,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class DropRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    @staticmethod
    def _validated_drop_mob_ids(mob_ids: object) -> list[int]:
        if not isinstance(mob_ids, list) or len(mob_ids) > 1000:
            raise DomainError("Источники дропа: требуется список не более 1000 мобов.")
        result = [positive_integer(mob_id, "Источник дропа") for mob_id in mob_ids]
        if len(result) != len(set(result)):
            raise DomainError("Источники дропа: один моб указан несколько раз.")
        return sorted(result)

    async def get_drop_source_mobs(
        self,
        query: str = "",
        offset: int = 0,
        limit: int = 9,
        *,
        mob_ids: list[int] | None = None,
    ) -> list[MobSourceRow]:
        """Search mob and location names literally, with deterministic bounded pages."""
        if not isinstance(query, str):
            raise DomainError("Поисковый запрос должен быть текстом.")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_SQLITE_ID:
            raise DomainError("Некорректная страница источников дропа.")
        limit = min(positive_integer(limit, "Размер страницы"), 100)
        source_ids = None if mob_ids is None else self.db._validated_drop_mob_ids(mob_ids)
        if source_ids == []:
            return []
        conditions = [
            "(INSTR(LOWER_UNICODE(m.name), LOWER_UNICODE(?)) > 0 OR INSTR(LOWER_UNICODE(l.name), LOWER_UNICODE(?)) > 0)"
        ]
        params: SqlParams = (query, query)
        if source_ids is not None:
            placeholders = ",".join("?" for _ in source_ids)
            conditions.append(f"m.id IN ({placeholders})")
            params += tuple(source_ids)
        rows = await self.db.execute_query(
            "SELECT m.id,m.name,m.emoji,m.location_id,l.name AS location_name,l.emoji AS location_emoji "
            "FROM mobs m JOIN locations l ON l.id=m.location_id "
            f"WHERE {' AND '.join(conditions)} "
            "ORDER BY LOWER_UNICODE(l.name),LOWER_UNICODE(m.name),m.id LIMIT ? OFFSET ?",
            params + (limit, offset),
        )
        return [
            MobSourceRow(
                **_item_row(row),
                location_id=sql_int(row["location_id"]),
                location_name=sql_text(row["location_name"]),
                location_emoji=sql_text(row["location_emoji"]),
            )
            for row in rows
        ]

    async def _resolve_drop_item(self, item_type: DropItemType, item_id: int) -> int:
        tables = {"resource": "resources", "gear": "gear", "card": "cards"}
        if item_type not in tables:
            raise DomainError("Неизвестный тип предмета для дропа.")
        item_id = positive_integer(item_id, "Предмет")
        if item_type == "gear":
            item_id = await self.db.resolve_gear_id(item_id)
        if not await self.db.execute_query(f"SELECT 1 FROM {tables[item_type]} WHERE id=?", (item_id,)):
            raise DomainError("Предмет уже удалён. Откройте актуальный список.")
        return item_id

    async def get_item_drop_mob_ids(self, item_type: DropItemType, item_id: int) -> list[int]:
        async with self.db._connection_guard():
            item_id = await self.db._resolve_drop_item(item_type, item_id)
            rows = await self.db.execute_query(
                "SELECT mob_id FROM drops WHERE item_type=? AND item_id=? ORDER BY mob_id",
                (item_type, item_id),
            )
            return [sql_int(row["mob_id"]) for row in rows]

    async def set_item_drop_sources(
        self,
        item_type: DropItemType,
        item_id: int,
        mob_ids: list[int],
        *,
        expected_mob_ids: list[int] | None = None,
    ) -> None:
        """Atomically replace sources, rejecting a changed baseline before any write."""
        selected = self.db._validated_drop_mob_ids(mob_ids)
        expected = None if expected_mob_ids is None else self.db._validated_drop_mob_ids(expected_mob_ids)
        async with self.db.transaction():
            item_id = await self.db._resolve_drop_item(item_type, item_id)
            current = await self.db.get_item_drop_mob_ids(item_type, item_id)
            if expected is not None and current != expected:
                raise DomainError("Источники дропа изменены другим редактором. Откройте их заново перед сохранением.")
            if selected:
                placeholders = ",".join("?" for _ in selected)
                existing = await self.db.execute_query(
                    f"SELECT id FROM mobs WHERE id IN ({placeholders})", tuple(selected)
                )
                if {sql_int(row["id"]) for row in existing} != set(selected):
                    raise DomainError("Один из выбранных мобов уже удалён. Обновите список источников.")
            for mob_id in set(current) - set(selected):
                await self.db.execute_query(
                    "DELETE FROM drops WHERE mob_id=? AND item_type=? AND item_id=?",
                    (mob_id, item_type, item_id),
                )
            for mob_id in set(selected) - set(current):
                await self.db.execute_query(
                    "INSERT INTO drops(mob_id,item_type,item_id) VALUES (?,?,?)",
                    (mob_id, item_type, item_id),
                )

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
        return await self.db.execute_query(
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
        rows = await self.db.execute_query(
            f"SELECT item_id FROM drops WHERE mob_id = ? AND item_type = ? AND item_id IN ({placeholders})",
            (mob_id, item_type, *item_ids),
        )
        return {sql_int(row["item_id"]) for row in rows}

    async def get_drop_status(self, mob_id: int, item_type: str, item_id: int) -> bool:
        return item_id in await self.db.get_enabled_drop_ids(mob_id, item_type, [item_id])

    async def add_drop(self, mob_id: int, item_type: str, item_id: int) -> None:
        if item_type not in ("resource", "gear", "card"):
            raise DomainError("Неизвестный тип дропа.")
        kind: DropItemType = "resource" if item_type == "resource" else "gear" if item_type == "gear" else "card"
        await self.db.set_drop_enabled(mob_id, kind, item_id, True)

    async def remove_drop(self, mob_id: int, item_type: str, item_id: int) -> None:
        if item_type not in ("resource", "gear", "card"):
            raise DomainError("Неизвестный тип дропа.")
        kind: DropItemType = "resource" if item_type == "resource" else "gear" if item_type == "gear" else "card"
        await self.db.set_drop_enabled(mob_id, kind, item_id, False)

    async def set_drop_enabled(self, mob_id: int, item_type: DropItemType, item_id: int, enabled: bool) -> None:
        positive_integer(mob_id, "Моб")
        if not isinstance(enabled, bool):
            raise DomainError("Состояние дропа должно быть да/нет.")
        async with self.db.transaction():
            item_id = await self.db._resolve_drop_item(item_type, item_id)
            if not await self.db.execute_query("SELECT 1 FROM mobs WHERE id=?", (mob_id,)):
                raise DomainError("Моб уже удалён.")
            if enabled:
                await self.db.execute_query(
                    "INSERT OR IGNORE INTO drops(mob_id,item_type,item_id) VALUES (?,?,?)", (mob_id, item_type, item_id)
                )
            else:
                await self.db.execute_query(
                    "DELETE FROM drops WHERE mob_id=? AND item_type=? AND item_id=?", (mob_id, item_type, item_id)
                )
