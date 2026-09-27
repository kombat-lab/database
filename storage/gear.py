from __future__ import annotations
from storage.types import sql_optional_int, sql_int, sql_text
from catalog_types import (
    GearRow,
    NavigationIds,
    GearIngredientRow,
    GearCardRow,
)
from storage.rows import _item_row, _gear_row, _json_rows, _owner_entry
from storage.types import DbRow
from recipe_domain import (
    positive_integer,
    DomainError,
    DuplicateIdentityError,
    validate_draft_payload,
    normalize_identity,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class GearRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def get_gear_by_id(self, gear_id: int) -> GearRow | None:
        gear_id = await self.db.resolve_gear_id(gear_id)
        res = await self.db.execute_query("SELECT * FROM gear WHERE id = ?", (gear_id,))
        if not res:
            return None
        row = res[0]
        return GearRow(
            id=sql_int(row["id"]),
            name=sql_text(row["name"]),
            emoji=sql_text(row["emoji"]),
            rarity=sql_text(row["rarity"]),
            slot=sql_text(row["slot"]),
            level=sql_int(row["level"]),
            classes=sql_text(row["classes"]),
            note=sql_text(row["note"]),
        )

    async def add_gear(
        self,
        name: str,
        rarity: str,
        slot: str,
        emoji: str,
        level: int = 1,
        classes: str = "",
        note: str = "",
        *,
        allow_duplicate: bool = False,
    ) -> int:
        value = validate_draft_payload(
            dict(name=name, rarity=rarity, slot=slot, emoji=emoji, level=level, classes=classes, note=note),
            complete=True,
        )
        if not isinstance(allow_duplicate, bool):
            raise DomainError("Подтвердите создание отдельного варианта.")
        async with self.db.transaction():
            if not allow_duplicate and await self.db.execute_query(
                "SELECT 1 FROM gear WHERE NORMALIZE_IDENTITY(name)=? AND rarity=? AND slot=? AND level=?",
                (normalize_identity(value["name"]), rarity, slot, level),
            ):
                raise DuplicateIdentityError("Такое снаряжение этого уровня уже существует.")
            return await self.db.execute_insert(
                "INSERT INTO gear(name,rarity,slot,emoji,level,classes,note) VALUES (?,?,?,?,?,?,?)",
                (value["name"], rarity, slot, value["emoji"], level, value["classes"], value["note"]),
            )

    async def update_gear(
        self,
        gear_id: int,
        name: str | None = None,
        rarity: str | None = None,
        slot: str | None = None,
        emoji: str | None = None,
        level: int | None = None,
        classes: str | None = None,
        note: str | None = None,
    ) -> None:
        async with self.db.transaction():
            gear_id = await self.db.resolve_gear_id(gear_id)
            current = await self.db.get_gear_by_id(gear_id)
            if not current:
                raise ValueError("Gear not found")
            new_name = name if name is not None else current["name"]
            new_rarity = rarity if rarity is not None else current["rarity"]
            new_slot = slot if slot is not None else current["slot"]
            new_emoji = emoji if emoji is not None else current["emoji"]
            new_level = level if level is not None else current.get("level", 1)
            new_classes = classes if classes is not None else current.get("classes", "")
            new_note = note if note is not None else current.get("note", "")
            value = validate_draft_payload(
                dict(
                    name=new_name,
                    rarity=new_rarity,
                    slot=new_slot,
                    emoji=new_emoji,
                    level=new_level,
                    classes=new_classes,
                    note=new_note,
                ),
                complete=True,
            )
            new_name, new_classes = value["name"], value["classes"]
            if (normalize_identity(new_name), new_rarity, new_slot, new_level) != (
                normalize_identity(current["name"]),
                current["rarity"],
                current["slot"],
                current["level"],
            ):
                if await self.db.execute_query(
                    "SELECT 1 FROM gear WHERE NORMALIZE_IDENTITY(name)=? AND rarity=? AND slot=? AND level=? AND id<>?",
                    (normalize_identity(new_name), new_rarity, new_slot, new_level, gear_id),
                ):
                    raise DuplicateIdentityError("Такое снаряжение этого уровня уже существует.")
            await self.db.execute_query(
                "UPDATE gear SET name=?, rarity=?, slot=?, emoji=?, level=?, classes=?, note=? WHERE id=?",
                (new_name, new_rarity, new_slot, new_emoji, new_level, new_classes, new_note, gear_id),
            )

    async def delete_gear(self, gear_id: int) -> None:
        async with self.db.transaction():
            gear_id = await self.db.resolve_gear_id(gear_id)
            if await self.db.execute_query("SELECT 1 FROM gear_aliases WHERE canonical_gear_id=?", (gear_id,)):
                raise DomainError(
                    "Предмет сохраняет старые ссылки после объединения. Его удаление требует проверки алиасов."
                )
            await self.db.execute_query("DELETE FROM drops WHERE item_type='gear' AND item_id=?", (gear_id,))
            await self.db._delete_recipes_by_result("gear", gear_id)
            await self.db.execute_query("DELETE FROM gear WHERE id=?", (gear_id,))

    async def get_gear_card(self, gear_id: int) -> GearCardRow | None:
        gear_id = await self.db.resolve_gear_id(gear_id)
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
        res = await self.db.execute_query(query, (gear_id,))
        if not res:
            return None
        row = res[0]
        owners = [_owner_entry(owner) for owner in _json_rows(row["owner_entries"])]
        learning_scroll = (
            await self.db.get_recipe_learning_scroll(sql_int(row["recipe_id"]))
            if row["recipe_id"] is not None
            else None
        )
        return GearCardRow(
            **_gear_row(row),
            recipe_id=sql_int(row["recipe_id"]) if row["recipe_id"] is not None else None,
            craft_quantity=sql_int(row["craft_quantity"]) if row["craft_quantity"] is not None else 1,
            mobs=[_item_row(mob) for mob in _json_rows(row["mobs"])],
            scroll_mobs=[_item_row(mob) for mob in _json_rows(row["scroll_mobs"])],
            ingredients=[
                GearIngredientRow(
                    **_item_row(ingredient),
                    type=sql_text(ingredient["type"]),
                    quantity=sql_int(ingredient["quantity"]),
                )
                for ingredient in _json_rows(row["ingredients"])
            ],
            owners=[owner["player_username"] for owner in owners if owner["player_username"]],
            owner_entries=owners,
            owner_user_ids=[owner["user_id"] for owner in owners if owner["user_id"] is not None],
            craftable=row["recipe_id"] is not None,
            learning_scroll=learning_scroll,
            can_learn=learning_scroll is not None,
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
            else f"{self.db._slot_order_case()}, LOWER_UNICODE(name), id"
        )
        slot_filter = " AND slot = ?" if slot is not None else ""
        params = (rarity, slot, gear_id) if slot is not None else (rarity, gear_id)
        rows = await self.db.execute_query(
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
            prev_id=sql_optional_int(rows[0]["prev_id"]) if rows else None,
            next_id=sql_optional_int(rows[0]["next_id"]) if rows else None,
        )

    async def get_all_gear_simple(self) -> list[DbRow]:
        return await self.db.execute_query(
            f"SELECT id, name, emoji FROM gear ORDER BY {self.db._slot_order_case()}, LOWER_UNICODE(name), id"
        )

    async def resolve_gear_id(self, gear_id: int) -> int:
        positive_integer(gear_id, "Снаряжение")
        rows = await self.db.execute_query("SELECT canonical_gear_id FROM gear_aliases WHERE alias_id=?", (gear_id,))
        return sql_int(rows[0]["canonical_gear_id"]) if rows else gear_id

    async def merge_gear(self, source_gear_id: int, target_gear_id: int) -> int:
        """Merge verified duplicate profiles, preserving old public IDs as aliases."""
        async with self.db.transaction():
            source_id = await self.db.resolve_gear_id(source_gear_id)
            target_id = await self.db.resolve_gear_id(target_gear_id)
            if source_id == target_id:
                return target_id
            source = await self.db.get_gear_by_id(source_id)
            target = await self.db.get_gear_by_id(target_id)
            if source is None or target is None:
                raise DomainError("Один из объединяемых предметов уже удалён.")
            left = validate_draft_payload({key: value for key, value in source.items() if key != "id"})
            right = validate_draft_payload({key: value for key, value in target.items() if key != "id"})
            if source["name"].strip().casefold() != target["name"].strip().casefold() or any(
                left.get(key) != right.get(key) for key in ("rarity", "slot", "level", "classes", "note")
            ):
                raise DomainError("Предметы отличаются уровнем, редкостью, слотом, классами или описанием.")
            source_recipes = await self.db.execute_query(
                "SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (source_id,)
            )
            target_recipes = await self.db.execute_query(
                "SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (target_id,)
            )
            if source_recipes and target_recipes:
                raise DomainError(
                    "Оба предмета имеют рецепты. Требуется отдельное разрешение конфликта формул и изучения."
                )
            await self.db.execute_query(
                "UPDATE recipes SET result_id=? WHERE result_type='gear' AND result_id=?", (target_id, source_id)
            )
            await self.db.execute_query(
                "INSERT OR IGNORE INTO drops(mob_id,item_type,item_id) SELECT mob_id,'gear',? FROM drops WHERE item_type='gear' AND item_id=?",
                (target_id, source_id),
            )
            await self.db.execute_query("DELETE FROM drops WHERE item_type='gear' AND item_id=?", (source_id,))
            await self.db.execute_query(
                "UPDATE gear_aliases SET canonical_gear_id=? WHERE canonical_gear_id=?", (target_id, source_id)
            )
            await self.db.execute_query(
                "INSERT INTO gear_aliases(alias_id,canonical_gear_id) VALUES (?,?)", (source_id, target_id)
            )
            await self.db.execute_query("DELETE FROM gear WHERE id=?", (source_id,))
            return target_id

    async def get_all_gear(self, offset: int, limit: int) -> list[DbRow]:
        slot_order = self.db._slot_order_case()
        rarity_order = self.db._rarity_order_case()
        return await self.db.execute_query(
            "SELECT id, name, rarity, slot, emoji, level, classes, note "
            f"FROM gear ORDER BY {slot_order}, {rarity_order}, level, "
            "LOWER_UNICODE(name), id LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def get_gear_by_slot(
        self,
        slot: str,
        offset: int,
        limit: int,
    ) -> list[DbRow]:
        rarity_order = self.db._rarity_order_case()
        return await self.db.execute_query(
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
        return await self.db.execute_query(
            "SELECT id, name, rarity, slot, emoji, level, classes, note "
            "FROM gear WHERE rarity = ? AND slot = ? "
            "ORDER BY level, LOWER_UNICODE(name), id LIMIT ? OFFSET ?",
            (rarity, slot, limit, offset),
        )
