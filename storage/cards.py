from __future__ import annotations
from storage.types import sql_optional_int, sql_int, sql_text
import json
from catalog_types import (
    CardRow,
    NavigationIds,
    ResourceDropMobRow,
)
from storage.rows import _item_row, _card_row
from storage.types import DbRow
from game_constants import (
    GEAR_SLOTS,
)
from recipe_domain import (
    DomainError,
    DuplicateIdentityError,
    positive_integer,
    normalize_identity,
    validate_name,
    validate_emoji,
    validate_note,
    validate_bonus,
    validate_page,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class CardRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def get_cards_page(self, offset: int, limit: int) -> list[DbRow]:
        return await self.db.execute_query(
            "SELECT id, name, emoji, slot FROM cards ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
        )

    async def get_all_cards_sorted_by_slot(self, offset: int, limit: int) -> list[DbRow]:
        case_expression = self.db._slot_order_case()

        query = f"""
            SELECT id, name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note
            FROM cards
            ORDER BY {case_expression}, name COLLATE NOCASE
            LIMIT ? OFFSET ?
        """
        return await self.db.execute_query(query, (limit, offset))

    async def get_card_by_id(self, card_id: int) -> CardRow | None:
        res = await self.db.execute_query("SELECT * FROM cards WHERE id = ?", (card_id,))
        if not res:
            return None
        row = res[0]
        return CardRow(
            id=sql_int(row["id"]),
            name=sql_text(row["name"]),
            emoji=sql_text(row["emoji"]),
            slot=sql_text(row["slot"]),
            bonus1=sql_text(row["bonus1"]),
            bonus2=sql_text(row["bonus2"]),
            bonus3=sql_text(row["bonus3"]),
            bonus4=sql_text(row["bonus4"]),
            note=sql_text(row["note"]),
        )

    async def add_card(
        self,
        name: str,
        emoji: str,
        slot: str,
        bonus1: str = "",
        bonus2: str = "",
        bonus3: str = "",
        bonus4: str = "",
        note: str = "",
        *,
        allow_duplicate: bool = False,
    ) -> int:
        name, emoji, note = validate_name(name), validate_emoji(emoji), validate_note(note)
        bonuses = tuple(validate_bonus(value) for value in (bonus1, bonus2, bonus3, bonus4))
        if slot not in GEAR_SLOTS:
            raise DomainError("Неизвестный слот карты.")
        if not isinstance(allow_duplicate, bool):
            raise DomainError("Подтвердите создание отдельного варианта.")
        async with self.db.transaction():
            if not allow_duplicate and await self.db.get_card_name_matches(name, slot):
                raise DuplicateIdentityError(
                    "Такая карта уже существует. Выберите её или явно подтвердите отдельный вариант."
                )
            return await self.db.execute_insert(
                "INSERT INTO cards(name,emoji,slot,bonus1,bonus2,bonus3,bonus4,note) VALUES (?,?,?,?,?,?,?,?)",
                (name, emoji, slot, *bonuses, note),
            )

    async def create_card_with_sources(
        self,
        name: str,
        emoji: str,
        slot: str,
        bonus1: str = "",
        bonus2: str = "",
        bonus3: str = "",
        bonus4: str = "",
        note: str = "",
        *,
        mob_ids: list[int],
        allow_duplicate: bool = False,
        operation_id: str | None = None,
    ) -> int:
        request = json.dumps(
            [name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note, sorted(mob_ids), allow_duplicate],
            ensure_ascii=False,
        )
        async with self.db.transaction():
            previous = await self.db._catalog_command_result(operation_id, "card", request)
            if previous is not None:
                return previous
            card_id = await self.db.add_card(
                name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note, allow_duplicate=allow_duplicate
            )
            await self.db.set_item_drop_sources("card", card_id, mob_ids)
            await self.db._record_catalog_command(operation_id, "card", request, card_id)
            return card_id

    async def update_card(self, card_id: int, **kwargs: str) -> None:
        positive_integer(card_id, "Карта")
        allowed = {"name", "emoji", "slot", "bonus1", "bonus2", "bonus3", "bonus4", "note"}
        if set(kwargs) - allowed:
            raise DomainError("Неизвестное поле карты.")
        if not kwargs:
            return
        async with self.db.transaction():
            current = await self.db.get_card_by_id(card_id)
            if current is None:
                raise DomainError("Карта уже удалена.")
            updates: dict[str, str] = {}
            for field, value in kwargs.items():
                if field == "name":
                    updates[field] = validate_name(value)
                elif field == "emoji":
                    updates[field] = validate_emoji(value)
                elif field == "note":
                    updates[field] = validate_note(value)
                elif field == "slot":
                    if value not in GEAR_SLOTS:
                        raise DomainError("Неизвестный слот карты.")
                    updates[field] = value
                else:
                    updates[field] = validate_bonus(value)
            new_name, new_slot = updates.get("name", current["name"]), updates.get("slot", current["slot"])
            if (normalize_identity(new_name), new_slot) != (normalize_identity(current["name"]), current["slot"]):
                if any(row["id"] != card_id for row in await self.db.get_card_name_matches(new_name, new_slot)):
                    raise DuplicateIdentityError("Такая карта уже существует.")
            assignments = ",".join(f"{field}=?" for field in updates)
            await self.db.execute_query(f"UPDATE cards SET {assignments} WHERE id=?", (*updates.values(), card_id))

    async def delete_card(self, card_id: int) -> None:
        positive_integer(card_id, "Карта")
        async with self.db.transaction():
            await self.db.execute_query("DELETE FROM drops WHERE item_type='card' AND item_id=?", (card_id,))
            await self.db._delete_recipes_by_result("card", card_id)
            await self.db.execute_query("DELETE FROM cards WHERE id=?", (card_id,))

    async def get_card_drop_mobs(self, card_id: int) -> list[ResourceDropMobRow]:
        rows = await self.db.execute_query(
            """SELECT m.id, m.name, m.emoji, l.id as location_id, l.name as location_name, l.emoji as location_emoji
               FROM drops d
               JOIN mobs m ON d.mob_id = m.id
               JOIN locations l ON m.location_id = l.id
               WHERE d.item_type='card' AND d.item_id=?
               ORDER BY m.id""",
            (card_id,),
        )
        return [
            ResourceDropMobRow(
                **_item_row(row),
                location_id=sql_int(row["location_id"]),
                location_name=sql_text(row["location_name"]),
                location_emoji=sql_text(row["location_emoji"]),
            )
            for row in rows
        ]

    async def get_prev_next_card_by_slot(self, card_id: int) -> NavigationIds:
        case_expression = self.db._slot_order_case()

        rows = await self.db.execute_query(
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
            prev_id=sql_optional_int(rows[0]["prev_id"]) if rows else None,
            next_id=sql_optional_int(rows[0]["next_id"]) if rows else None,
        )

    async def get_card_name_matches(self, name: str, slot: str | None = None) -> list[CardRow]:
        rows = await self.db.execute_query(
            "SELECT * FROM cards WHERE NORMALIZE_IDENTITY(name)=? AND (? IS NULL OR slot=?) ORDER BY id",
            (normalize_identity(name), slot, slot),
        )
        return [_card_row(row) for row in rows]

    async def get_cards_by_slot(self, slot: str, offset: int = 0, limit: int = 100) -> list[CardRow]:
        validate_page(offset, limit)
        rows = await self.db.execute_query(
            "SELECT * FROM cards WHERE slot=? ORDER BY NORMALIZE_IDENTITY(name),id LIMIT ? OFFSET ?",
            (slot, limit, offset),
        )
        return [_card_row(row) for row in rows]
