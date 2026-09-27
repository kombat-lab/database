from __future__ import annotations
from storage.types import sql_optional_int, sql_int, sql_text
import json
from catalog_types import (
    ResourceRow,
    NavigationIds,
    ResourceDropMobRow,
    ResourceUsageRow,
    ResourceCardRow,
    LearningRecipeRow,
)
from storage.rows import _item_row, _resource_row, _json_rows
from storage.types import DbRow
from game_constants import (
    RESOURCE_TYPE_KEYS,
)
from recipe_domain import (
    DomainError,
    DuplicateIdentityError,
    ResourceDependencies,
    positive_integer,
    normalize_identity,
    validate_name,
    validate_emoji,
    validate_note,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class ResourceRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

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
        res = await self.db.execute_query(query, (resource_id,))
        if not res:
            return None
        row = res[0]
        mobs = [
            ResourceDropMobRow(
                **_item_row(mob),
                location_id=sql_int(mob["location_id"]),
                location_name=sql_text(mob["location_name"]),
                location_emoji=sql_text(mob["location_emoji"]),
            )
            for mob in _json_rows(row["mobs"])
        ]
        mobs.sort(key=lambda mob: (mob["location_id"], mob["name"].casefold(), mob["id"]))
        usages = [
            ResourceUsageRow(
                recipe_id=sql_int(usage["recipe_id"]),
                result_type=sql_text(usage["result_type"]),
                result_id=sql_int(usage["result_id"]),
                result_name=sql_text(usage["result_name"]),
                result_emoji=sql_text(usage["result_emoji"]),
                quantity=sql_int(usage["quantity"]),
                result_rarity=sql_text(usage["result_rarity"]) if usage["result_rarity"] is not None else None,
            )
            for usage in _json_rows(row["used_in"])
        ]
        learning_recipes: list[LearningRecipeRow] = []
        for link in await self.db.execute_query(
            """
            SELECT rec.id, rec.result_id, rec.quantity, g.name, g.emoji, g.rarity
            FROM recipe_learning_requirements lr JOIN recipes rec ON rec.id=lr.recipe_id
            JOIN gear g ON rec.result_type='gear' AND g.id=rec.result_id
            WHERE lr.scroll_resource_id=? ORDER BY rec.id
        """,
            (resource_id,),
        ):
            details = await self.db.get_recipe_details(sql_int(link["id"]))
            if details is not None:
                entries = details["owner_entries"]
                learning_recipes.append(
                    LearningRecipeRow(
                        recipe_id=sql_int(link["id"]),
                        result_id=sql_int(link["result_id"]),
                        result_name=sql_text(link["name"]),
                        result_emoji=sql_text(link["emoji"]),
                        result_rarity=sql_text(link["rarity"]),
                        quantity=sql_int(link["quantity"]),
                        ingredients=details["ingredients"],
                        owner_entries=entries,
                        owner_user_ids=[entry["user_id"] for entry in entries if entry["user_id"] is not None],
                    )
                )
        return ResourceCardRow(
            **_resource_row(row),
            mobs=mobs,
            used_in=usages,
            learning_recipes=learning_recipes,
            craft_location=sql_text(row["craft_location"]),
        )

    async def get_resources_page(self, offset: int, limit: int) -> list[DbRow]:
        return await self.db.execute_query(
            f"SELECT id, name, emoji, type FROM resources ORDER BY {self.db.RESOURCE_NAME_ORDER} LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def get_resource_name_matches(self, name: str, resource_type: str | None = None) -> list[ResourceRow]:
        rows = await self.db.execute_query(
            "SELECT id,name,emoji,type,note,code FROM resources WHERE NORMALIZE_IDENTITY(name)=? AND (? IS NULL OR type=?) ORDER BY id",
            (normalize_identity(name), resource_type, resource_type),
        )
        return [_resource_row(row) for row in rows]

    async def get_resource_by_code(self, code: str) -> ResourceRow | None:
        rows = await self.db.execute_query("SELECT id,name,emoji,type,note,code FROM resources WHERE code=?", (code,))
        return _resource_row(rows[0]) if rows else None

    async def get_resources_by_location(self, location_id: int, offset: int, limit: int) -> list[DbRow]:
        query = """
            SELECT DISTINCT r.id, r.name, r.emoji, r.type
            FROM resources r
            JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
            JOIN mobs m ON d.mob_id = m.id
            WHERE m.location_id = ?
            ORDER BY r.id LIMIT ? OFFSET ?
        """
        return await self.db.execute_query(query, (location_id, limit, offset))

    async def get_resource_by_id(self, resource_id: int) -> ResourceRow | None:
        res = await self.db.execute_query(
            "SELECT id, name, emoji, type, note, code FROM resources WHERE id = ?", (resource_id,)
        )
        if not res:
            return None
        row = res[0]
        return ResourceRow(
            id=sql_int(row["id"]),
            name=sql_text(row["name"]),
            emoji=sql_text(row["emoji"]),
            type=sql_text(row["type"]),
            note=sql_text(row["note"]),
            code=sql_text(row["code"]) if row.get("code") is not None else None,
        )

    async def add_resource(
        self, name: str, emoji: str, resource_type: str = "craft", note: str = "", *, allow_duplicate: bool = False
    ) -> int:
        name, emoji, note = validate_name(name, resource=True), validate_emoji(emoji), validate_note(note)
        if resource_type not in RESOURCE_TYPE_KEYS:
            raise DomainError("Неизвестный тип ресурса.")
        if not isinstance(allow_duplicate, bool):
            raise DomainError("Подтвердите создание отдельного варианта.")
        async with self.db.transaction():
            if not allow_duplicate and await self.db.get_resource_name_matches(name, resource_type):
                raise DuplicateIdentityError(
                    "Такой ресурс уже существует. Выберите его или явно подтвердите отдельный вариант."
                )
            return await self.db.execute_insert(
                "INSERT INTO resources(name,emoji,type,note) VALUES (?,?,?,?)", (name, emoji, resource_type, note)
            )

    async def create_resource_with_sources(
        self,
        name: str,
        emoji: str,
        resource_type: str = "craft",
        note: str = "",
        *,
        mob_ids: list[int],
        allow_duplicate: bool = False,
        operation_id: str | None = None,
    ) -> int:
        """Publish a resource and its reviewed sources once, in one transaction."""
        request = json.dumps([name, emoji, resource_type, note, sorted(mob_ids), allow_duplicate], ensure_ascii=False)
        async with self.db.transaction():
            previous = await self.db._catalog_command_result(operation_id, "resource", request)
            if previous is not None:
                return previous
            resource_id = await self.db.add_resource(name, emoji, resource_type, note, allow_duplicate=allow_duplicate)
            await self.db.set_item_drop_sources("resource", resource_id, mob_ids)
            await self.db._record_catalog_command(operation_id, "resource", request, resource_id)
            return resource_id

    async def update_resource(
        self,
        resource_id: int,
        name: str | None = None,
        emoji: str | None = None,
        resource_type: str | None = None,
        note: str | None = None,
    ) -> None:
        async with self.db.transaction():
            current = await self.db.get_resource_by_id(resource_id)
            if not current:
                raise ValueError("Resource not found")
            positive_integer(resource_id, "Ресурс")
            new_name = validate_name(name, resource=True) if name is not None else current["name"]
            new_emoji = validate_emoji(emoji) if emoji is not None else current["emoji"]
            new_type = resource_type if resource_type is not None else current["type"]
            new_note = validate_note(note) if note is not None else current["note"]
            if (normalize_identity(new_name), new_type) != (normalize_identity(current["name"]), current["type"]):
                if any(row["id"] != resource_id for row in await self.db.get_resource_name_matches(new_name, new_type)):
                    raise DuplicateIdentityError("Такой ресурс уже существует.")
            if new_type not in RESOURCE_TYPE_KEYS:
                raise DomainError("Неизвестный тип ресурса.")
            if new_type != current["type"]:
                dependencies = await self.db.get_resource_dependencies(resource_id)
                if (dependencies["learning_recipe_ids"] and new_type != "scroll_recipe") or (
                    new_type == "scroll_recipe"
                    and (dependencies["ingredient_recipe_ids"] or dependencies["result_recipe_ids"])
                ):
                    raise DomainError("Тип ресурса несовместим с существующими материалами или изучением.")
            await self.db.execute_query(
                "UPDATE resources SET name=?, emoji=?, type=?, note=? WHERE id=?",
                (new_name, new_emoji, new_type, new_note, resource_id),
            )

    async def get_resource_dependencies(self, resource_id: int) -> ResourceDependencies:
        return ResourceDependencies(
            ingredient_recipe_ids=[
                sql_int(row["recipe_id"])
                for row in await self.db.execute_query(
                    "SELECT recipe_id FROM recipe_ingredients WHERE resource_id=? ORDER BY recipe_id", (resource_id,)
                )
            ],
            learning_recipe_ids=[
                sql_int(row["recipe_id"])
                for row in await self.db.execute_query(
                    "SELECT recipe_id FROM recipe_learning_requirements WHERE scroll_resource_id=?", (resource_id,)
                )
            ],
            result_recipe_ids=[
                sql_int(row["id"])
                for row in await self.db.execute_query(
                    "SELECT id FROM recipes WHERE result_type='resource' AND result_id=?", (resource_id,)
                )
            ],
            drop_mob_ids=[
                sql_int(row["mob_id"])
                for row in await self.db.execute_query(
                    "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id", (resource_id,)
                )
            ],
        )

    async def delete_resource(self, resource_id: int) -> None:
        positive_integer(resource_id, "Ресурс")
        async with self.db.transaction():
            dependencies = await self.db.get_resource_dependencies(resource_id)
            if any(dependencies.values()):
                raise DomainError(
                    "Ресурс используется в материалах, изучении, рецепте или источниках. Сначала явно удалите эти связи."
                )
            await self.db.execute_query("DELETE FROM resources WHERE id=?", (resource_id,))

    async def get_resources_by_type(self, resource_type: str, offset: int, limit: int) -> list[DbRow]:
        return await self.db.execute_query(
            "SELECT id, name, emoji, type FROM resources WHERE type = ? "
            f"ORDER BY {self.db.RESOURCE_NAME_ORDER} LIMIT ? OFFSET ?",
            (resource_type, limit, offset),
        )

    async def get_all_resources_simple(self) -> list[DbRow]:
        return await self.db.execute_query(
            f"SELECT id, name, emoji FROM resources ORDER BY {self.db.RESOURCE_NAME_ORDER}"
        )

    async def get_prev_next_resource_by_type(self, resource_id: int, resource_type: str) -> NavigationIds:
        rows = await self.db.execute_query(
            f"""
            WITH ordered AS (
                SELECT id,
                       LAG(id) OVER (ORDER BY {self.db.RESOURCE_NAME_ORDER}) AS prev_id,
                       LEAD(id) OVER (ORDER BY {self.db.RESOURCE_NAME_ORDER}) AS next_id
                FROM resources
                WHERE type = ?
            )
            SELECT prev_id, next_id FROM ordered WHERE id = ?
            """,
            (resource_type, resource_id),
        )
        return NavigationIds(
            prev_id=sql_optional_int(rows[0]["prev_id"]) if rows else None,
            next_id=sql_optional_int(rows[0]["next_id"]) if rows else None,
        )

    async def get_prev_next_resource_by_location(
        self,
        resource_id: int,
        location_id: int,
    ) -> NavigationIds:
        rows = await self.db.execute_query(
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
            prev_id=sql_optional_int(rows[0]["prev_id"]) if rows else None,
            next_id=sql_optional_int(rows[0]["next_id"]) if rows else None,
        )
