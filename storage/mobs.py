from __future__ import annotations
from storage.types import sql_optional_int, sql_int, sql_text
import json
from catalog_types import (
    MobRow,
    NavigationIds,
    GearDropRow,
    CardDropRow,
    MobCardRow,
)
from storage.rows import _item_row, _mob_row, _json_rows
from storage.types import DbRow
from recipe_domain import (
    DomainError,
    positive_integer,
    validate_name,
    validate_emoji,
    nonnegative_integer,
    validate_page,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class MobRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

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
        res = await self.db.execute_query(query, (mob_id,))
        if not res:
            return None
        row = res[0]
        return MobCardRow(
            **_item_row(row),
            hp=sql_int(row["hp"]),
            dust_min=sql_int(row["dust_min"]),
            dust_max=sql_int(row["dust_max"]),
            exp=sql_int(row["exp"]),
            location_id=sql_int(row["location_id"]),
            loc_name=sql_text(row["loc_name"]),
            loc_emoji=sql_text(row["loc_emoji"]),
            resource_drops=[_item_row(drop) for drop in _json_rows(row["resource_drops"])],
            gear_drops=[
                GearDropRow(**_item_row(drop), slot=sql_text(drop["slot"]), rarity=sql_text(drop["rarity"]))
                for drop in _json_rows(row["gear_drops"])
            ],
            card_drops=[
                CardDropRow(**_item_row(drop), slot=sql_text(drop["slot"])) for drop in _json_rows(row["card_drops"])
            ],
        )

    async def get_mobs_by_location_sorted_by_hp(self, location_id: int, offset: int, limit: int) -> list[DbRow]:
        return await self.db.execute_query(
            "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs "
            "WHERE location_id = ? ORDER BY hp ASC, id LIMIT ? OFFSET ?",
            (location_id, limit, offset),
        )

    async def get_prev_next_mob_by_hp(self, mob_id: int, location_id: int) -> NavigationIds:
        rows = await self.db.execute_query(
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
            prev_id=sql_optional_int(rows[0]["prev_id"]) if rows else None,
            next_id=sql_optional_int(rows[0]["next_id"]) if rows else None,
        )

    async def update_mob_field(self, mob_id: int, field: str, value: str | int) -> None:
        positive_integer(mob_id, "Моб")
        if field not in self.db.ALLOWED_MOB_FIELDS:
            raise ValueError(f"Invalid field: {field}")
        async with self.db.transaction():
            rows = await self.db.execute_query("SELECT dust_min, dust_max FROM mobs WHERE id = ?", (mob_id,))
            if not rows:
                raise ValueError("Моб не найден.")
            if field in {"hp", "dust_min", "dust_max", "exp", "location_id"}:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError("Значение должно быть целым числом.")
                nonnegative_integer(value, "Показатель моба")
                if field == "location_id":
                    positive_integer(value, "Локация")
                    if await self.db.get_location_by_id(value) is None:
                        raise DomainError("Локация уже удалена.")
                if field in {"dust_min", "dust_max"}:
                    minimum = value if field == "dust_min" else sql_int(rows[0]["dust_min"])
                    maximum = value if field == "dust_max" else sql_int(rows[0]["dust_max"])
                    if minimum > maximum:
                        raise ValueError("Минимум пыли не может быть больше максимума.")
            elif field == "name":
                value = validate_name(value)
            elif field == "emoji":
                value = validate_emoji(value)
            query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
            await self.db.execute_query(query, (value, mob_id))

    async def delete_mob(self, mob_id: int) -> None:
        positive_integer(mob_id, "Моб")
        async with self.db.transaction():
            await self.db.execute_query("DELETE FROM drops WHERE mob_id = ?", (mob_id,))
            await self.db.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))

    async def get_mob_by_id(self, mob_id: int) -> MobRow | None:
        rows = await self.db.execute_query(
            "SELECT m.*,l.name AS location_name,l.emoji AS location_emoji FROM mobs m JOIN locations l ON l.id=m.location_id WHERE m.id=?",
            (mob_id,),
        )
        return _mob_row(rows[0]) if rows else None

    async def get_mobs_page(self, location_id: int, offset: int = 0, limit: int = 11) -> list[MobRow]:
        validate_page(offset, limit)
        rows = await self.db.execute_query(
            "SELECT m.*,l.name AS location_name,l.emoji AS location_emoji FROM mobs m JOIN locations l ON l.id=m.location_id WHERE m.location_id=? ORDER BY m.id LIMIT ? OFFSET ?",
            (location_id, limit, offset),
        )
        return [_mob_row(row) for row in rows]

    async def add_mob(
        self,
        name: str,
        emoji: str,
        hp: int,
        dust_min: int,
        dust_max: int,
        exp: int,
        location_id: int,
        *,
        operation_id: str | None = None,
    ) -> int:
        name, emoji = validate_name(name), validate_emoji(emoji)
        for value, label in ((hp, "Здоровье"), (dust_min, "Пыль"), (dust_max, "Пыль"), (exp, "Опыт")):
            nonnegative_integer(value, label)
        positive_integer(location_id, "Локация")
        if dust_min > dust_max:
            raise DomainError("Минимум пыли не может быть больше максимума.")
        request = json.dumps([name, emoji, hp, dust_min, dust_max, exp, location_id], ensure_ascii=False)
        async with self.db.transaction():
            previous = await self.db._catalog_command_result(operation_id, "mob", request)
            if previous is not None:
                return previous
            if await self.db.get_location_by_id(location_id) is None:
                raise DomainError("Локация уже удалена.")
            result = await self.db.execute_insert(
                "INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES (?,?,?,?,?,?,?)",
                (name, emoji, hp, dust_min, dust_max, exp, location_id),
            )
            await self.db._record_catalog_command(operation_id, "mob", request, result)
            return result
