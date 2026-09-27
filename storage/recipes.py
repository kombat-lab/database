from __future__ import annotations
from storage.types import sql_int, sql_text
import json
from typing import Literal
from catalog_types import (
    ResourceRow,
    RecipeOwnerEntry,
    ResourceRecipeRow,
    RecipeDetailsRow,
)
from storage.rows import _resource_row, _recipe_ingredient, _owner_entry
from storage.types import DbRow
from recipe_domain import (
    DomainError,
    DraftConflictError,
    MaterialInput,
    positive_integer,
    validate_draft_payload,
    validate_craft_location,
    validate_name,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class RecipeRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def get_all_recipes(self, result_type: str, offset: int, limit: int) -> list[DbRow]:
        if result_type == "gear":
            order_by = f"{self.db._slot_order_case('g.slot')}, g.name COLLATE NOCASE, r.id"
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
        return await self.db.execute_query(query, (result_type, limit, offset))

    async def get_recipe_owners(self, recipe_id: int) -> list[str]:
        rows = await self.db.execute_query(
            "SELECT player_username FROM recipe_owners WHERE recipe_id = ? ORDER BY LOWER_UNICODE(player_username)",
            (recipe_id,),
        )
        return [sql_text(row["player_username"]) for row in rows if row["player_username"]]

    async def get_recipe_learning_scroll(self, recipe_id: int) -> ResourceRow | None:
        rows = await self.db.execute_query(
            "SELECT s.* FROM recipe_learning_requirements lr JOIN resources s ON s.id=lr.scroll_resource_id WHERE lr.recipe_id=?",
            (recipe_id,),
        )
        return _resource_row(rows[0]) if rows else None

    async def get_recipe_details(self, recipe_id: int) -> RecipeDetailsRow | None:
        recipe_rows = await self.db.execute_query("SELECT * FROM recipes WHERE id=?", (recipe_id,))
        if not recipe_rows:
            return None
        recipe = recipe_rows[0]
        ingredients = await self.db.execute_query(
            "SELECT ri.resource_id, r.name, r.emoji, r.code, ri.quantity FROM recipe_ingredients ri "
            "JOIN resources r ON ri.resource_id=r.id WHERE ri.recipe_id=? ORDER BY ri.resource_id",
            (recipe_id,),
        )
        entries = await self.db.get_recipe_owner_entries(recipe_id)
        scroll = await self.db.get_recipe_learning_scroll(recipe_id)
        return RecipeDetailsRow(
            id=sql_int(recipe["id"]),
            result_type=sql_text(recipe["result_type"]),
            result_id=sql_int(recipe["result_id"]),
            quantity=sql_int(recipe["quantity"]),
            ingredients=[_recipe_ingredient(row) for row in ingredients],
            owners=[entry["player_username"] for entry in entries if entry["player_username"]],
            owner_entries=entries,
            learning_scroll=scroll,
            can_learn=scroll is not None,
            craft_location=sql_text(recipe["craft_location"]),
        )

    async def create_recipe(self, result_type: str, result_id: int, quantity: int = 1) -> int:
        tables = {"gear": "gear", "resource": "resources"}
        if result_type not in tables:
            raise DomainError("Недопустимый тип результата рецепта.")
        positive_integer(quantity, "Количество результата")
        positive_integer(result_id, "Предмет результата")
        async with self.db.transaction():
            if result_type == "resource":
                resource = await self.db.get_resource_by_id(result_id)
                if resource is not None and resource["type"] == "scroll_recipe":
                    raise DomainError("Изучаемый свиток не может быть результатом изготовления.")
            if not await self.db.execute_query(f"SELECT 1 FROM {tables[result_type]} WHERE id = ?", (result_id,)):
                raise DomainError("Предмет результата уже удалён. Откройте список заново.")
            if await self.db.execute_query(
                "SELECT 1 FROM recipes WHERE result_type = ? AND result_id = ?",
                (result_type, result_id),
            ):
                raise DomainError("Для этого предмета рецепт уже существует.")
            return await self.db.execute_insert(
                "INSERT INTO recipes (result_type, result_id, quantity) VALUES (?, ?, ?)",
                (result_type, result_id, quantity),
            )

    async def update_recipe_quantity(self, recipe_id: int, quantity: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(quantity, "Количество результата")
        async with self.db.transaction():
            if not await self.db.execute_query("SELECT 1 FROM recipes WHERE id=?", (recipe_id,)):
                raise DomainError("Рецепт уже удалён.")
            await self.db.execute_query("UPDATE recipes SET quantity=? WHERE id=?", (quantity, recipe_id))

    async def update_recipe_craft_location(self, recipe_id: int, craft_location: str) -> None:
        positive_integer(recipe_id, "Рецепт")
        value = validate_craft_location(craft_location)
        async with self.db.transaction():
            if not await self.db.execute_query(
                "SELECT 1 FROM recipes WHERE id=? AND result_type='resource'", (recipe_id,)
            ):
                raise DomainError("Место изготовления задаётся для существующего рецепта ресурса.")
            await self.db.execute_query("UPDATE recipes SET craft_location=? WHERE id=?", (value, recipe_id))

    async def delete_recipe(self, recipe_id: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        async with self.db.transaction():
            await self.db.execute_query("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
            await self.db.execute_query("DELETE FROM recipe_owners WHERE recipe_id=?", (recipe_id,))
            await self.db.execute_query("DELETE FROM recipes WHERE id=?", (recipe_id,))

    async def add_ingredient(self, recipe_id: int, resource_id: int, quantity: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(resource_id, "Ресурс")
        positive_integer(quantity, "Количество ингредиента")
        async with self.db.transaction():
            if not await self.db.execute_query("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)):
                raise DomainError("Рецепт уже удалён. Откройте список заново.")
            resource = await self.db.get_resource_by_id(resource_id)
            if resource is None:
                raise DomainError("Ресурс уже удалён. Откройте список заново.")
            if resource["type"] == "scroll_recipe":
                raise DomainError("Свиток изучается один раз: укажите его в разделе изучения, а не материалов.")
            if await self.db.execute_query(
                "SELECT 1 FROM recipe_ingredients WHERE recipe_id = ? AND resource_id = ?",
                (recipe_id, resource_id),
            ):
                raise DomainError(
                    "Этот ресурс уже есть в рецепте. Измените его количество через редактирование ингредиентов."
                )
            await self.db.execute_query(
                "INSERT INTO recipe_ingredients (recipe_id, resource_id, quantity) VALUES (?, ?, ?)",
                (recipe_id, resource_id, quantity),
            )
            await self.db._validate_resource_graph()

    async def update_ingredient(self, recipe_id: int, resource_id: int, quantity: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(resource_id, "Ресурс")
        positive_integer(quantity, "Количество ингредиента")
        await self.db.execute_query(
            "UPDATE recipe_ingredients SET quantity=? WHERE recipe_id=? AND resource_id=?",
            (quantity, recipe_id, resource_id),
        )

    async def remove_ingredient(self, recipe_id: int, resource_id: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(resource_id, "Ресурс")
        async with self.db.transaction():
            if not await self.db.execute_query(
                "SELECT 1 FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?", (recipe_id, resource_id)
            ):
                return
            count = await self.db.execute_query(
                "SELECT COUNT(*) AS n FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,)
            )
            if sql_int(count[0]["n"]) <= 1:
                raise DomainError(
                    "Нельзя удалить последний материал из опубликованного рецепта. Удалите рецепт целиком или сначала добавьте другой материал."
                )
            await self.db.execute_query(
                "DELETE FROM recipe_ingredients WHERE recipe_id=? AND resource_id=?", (recipe_id, resource_id)
            )

    async def get_recipe_owner_entries(self, recipe_id: int) -> list[RecipeOwnerEntry]:
        rows = await self.db.execute_query(
            "SELECT owner_id, user_id, player_username FROM recipe_owners "
            "WHERE recipe_id = ? ORDER BY LOWER_UNICODE(player_username), owner_id",
            (recipe_id,),
        )
        return [_owner_entry(row) for row in rows]

    async def add_recipe_owner(self, recipe_id: int, player_username: str) -> None:
        positive_integer(recipe_id, "Рецепт")
        username = validate_name(player_username.strip().lstrip("@"))
        async with self.db.transaction():
            if not await self.db.execute_query(
                "SELECT 1 FROM recipe_learning_requirements WHERE recipe_id=?", (recipe_id,)
            ):
                raise DomainError("Изучение можно отметить только для рецепта с изучаемым свитком.")
            await self.db.execute_query(
                "INSERT OR IGNORE INTO recipe_owners (recipe_id,player_username) VALUES (?,?)", (recipe_id, username)
            )

    async def remove_recipe_owner(self, recipe_id: int, player_username: str) -> None:
        positive_integer(recipe_id, "Рецепт")
        await self.db.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id = ? AND user_id IS NULL AND player_username = ? COLLATE NOCASE",
            (recipe_id, player_username.strip().lstrip("@")),
        )

    async def remove_recipe_owner_entry(self, recipe_id: int, owner_id: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(owner_id, "Владелец")
        await self.db.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id = ? AND owner_id = ?",
            (recipe_id, owner_id),
        )

    async def claim_recipe_owner(
        self,
        recipe_id: int,
        user_id: int,
        username: str | None,
        *,
        expected_gear_id: int | None = None,
    ) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(user_id, "Игрок")
        async with self.db.transaction():
            rows = await self.db.execute_query(
                "SELECT g.id FROM recipes r JOIN gear g "
                "ON r.result_type = 'gear' AND g.id = r.result_id "
                "JOIN recipe_learning_requirements lr ON lr.recipe_id=r.id WHERE r.id = ?",
                (recipe_id,),
            )
            if expected_gear_id is not None:
                expected_gear_id = await self.db.resolve_gear_id(expected_gear_id)
            if not rows or (expected_gear_id is not None and rows[0]["id"] != expected_gear_id):
                raise ValueError("Рецепт изменился или недоступен. Откройте карточку заново.")
            await self.db.execute_query(
                "INSERT INTO users (user_id, username) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET username = excluded.username, "
                "last_activity = CURRENT_TIMESTAMP",
                (user_id, username),
            )
            await self.db.execute_query(
                "UPDATE recipe_owners SET player_username = ? WHERE user_id = ? AND player_username IS NOT ?",
                (username, user_id, username),
            )
            await self.db.execute_query(
                "INSERT INTO recipe_owners (recipe_id, user_id, player_username) VALUES (?, ?, ?) "
                "ON CONFLICT(recipe_id, user_id) WHERE user_id IS NOT NULL "
                "DO UPDATE SET player_username = excluded.player_username",
                (recipe_id, user_id, username),
            )

    async def relinquish_recipe_owner(self, recipe_id: int, user_id: int) -> None:
        positive_integer(recipe_id, "Рецепт")
        positive_integer(user_id, "Игрок")
        await self.db.execute_query(
            "DELETE FROM recipe_owners WHERE recipe_id = ? AND user_id = ?",
            (recipe_id, user_id),
        )

    async def get_recipe_for_resource(self, resource_id: int) -> ResourceRecipeRow | None:
        recipe_info = await self.db.execute_query(
            "SELECT id,quantity,craft_location FROM recipes WHERE result_type = 'resource' AND result_id = ?",
            (resource_id,),
        )
        if not recipe_info:
            return None
        recipe_id = recipe_info[0]["id"]
        ingredients = await self.db.execute_query(
            "SELECT ri.resource_id, r.name, r.emoji, r.code, ri.quantity "
            "FROM recipe_ingredients ri JOIN resources r ON ri.resource_id = r.id "
            "WHERE ri.recipe_id = ? ORDER BY ri.resource_id",
            (recipe_id,),
        )
        return ResourceRecipeRow(
            ingredients=[_recipe_ingredient(row) for row in ingredients],
            quantity=sql_int(recipe_info[0]["quantity"]),
            craft_location=sql_text(recipe_info[0]["craft_location"]),
        )

    async def save_resource_recipe(
        self,
        result_id: int,
        quantity: int,
        materials: list[MaterialInput],
        *,
        craft_location: str = "",
        operation_id: str | None = None,
    ) -> int:
        """Publish a complete resource formula atomically; identical retries reuse its ID."""
        positive_integer(result_id, "Результат")
        positive_integer(quantity, "Количество результата")
        craft_location = validate_craft_location(craft_location)
        validated = validate_draft_payload({"materials": materials})["materials"]
        if not validated:
            raise DomainError("Добавьте хотя бы один расходуемый материал.")
        request = json.dumps([result_id, quantity, validated, craft_location], ensure_ascii=False, sort_keys=True)
        async with self.db.transaction():
            replay = await self.db._catalog_command_result(operation_id, "resource_recipe", request)
            if replay is not None:
                return replay
            existing = await self.db.execute_query(
                "SELECT id,quantity,craft_location FROM recipes WHERE result_type='resource' AND result_id=?",
                (result_id,),
            )
            resolved: list[tuple[int, int]] = []
            for material in validated:
                resource_id = material.get("resource_id")
                if resource_id is None:
                    matching = await self.db.execute_query(
                        "SELECT id FROM resources WHERE NORMALIZE_IDENTITY(name)=NORMALIZE_IDENTITY(?) AND type='craft'",
                        (material["name"],),
                    )
                    if existing and len(matching) == 1:
                        resource_id = sql_int(matching[0]["id"])
                    else:
                        resource_id = await self.db._create_named_draft_resource(
                            material["name"],
                            material.get("emoji", ""),
                            "craft",
                            allow_duplicate=material.get("allow_duplicate", False),
                        )
                resolved.append((resource_id, material["quantity"]))
            if len({item[0] for item in resolved}) != len(resolved):
                raise DomainError("В рецепте повторяется материал.")
            if existing:
                recipe_id = sql_int(existing[0]["id"])
                previous = await self.db.execute_query(
                    "SELECT resource_id,quantity FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,)
                )
                if (
                    existing[0]["quantity"] != quantity
                    or existing[0]["craft_location"] != craft_location
                    or sorted(resolved)
                    != sorted((sql_int(item["resource_id"]), sql_int(item["quantity"])) for item in previous)
                ):
                    raise DraftConflictError(
                        "Для этого результата уже существует другая формула. Откройте редактирование."
                    )
                await self.db._record_catalog_command(operation_id, "resource_recipe", request, recipe_id)
                return recipe_id
            recipe_id = await self.db.create_recipe("resource", result_id, quantity)
            await self.db.update_recipe_craft_location(recipe_id, craft_location)
            for resource_id, amount in resolved:
                await self.db.add_ingredient(recipe_id, resource_id, amount)
            await self.db._record_catalog_command(operation_id, "resource_recipe", request, recipe_id)
            return recipe_id

    async def set_recipe_learning_scroll(self, recipe_id: int, scroll_resource_id: int | None) -> None:
        positive_integer(recipe_id, "Рецепт")
        async with self.db.transaction():
            if not await self.db.execute_query(
                "SELECT 1 FROM recipes r JOIN gear g ON r.result_type='gear' AND g.id=r.result_id WHERE r.id=?",
                (recipe_id,),
            ):
                raise DomainError("Изучение доступно только для существующего рецепта снаряжения.")
            old = await self.db.get_recipe_learning_scroll(recipe_id)
            old_id = old["id"] if old is not None else None
            if old_id == scroll_resource_id:
                return
            if await self.db.get_recipe_owner_entries(recipe_id):
                raise DomainError(
                    "У рецепта есть изучившие его игроки. Сначала явно проверьте и удалите эти записи перед сменой изучения."
                )
            if scroll_resource_id is not None:
                scroll = await self.db.get_resource_by_id(scroll_resource_id)
                if scroll is None or scroll["type"] != "scroll_recipe":
                    raise DomainError("Выберите существующий ресурс типа «рецепт» для изучения.")
                dependencies = await self.db.get_resource_dependencies(scroll_resource_id)
                if (
                    dependencies["ingredient_recipe_ids"]
                    or dependencies["result_recipe_ids"]
                    or any(item != recipe_id for item in dependencies["learning_recipe_ids"])
                ):
                    raise DomainError("Свиток уже связан с другим рецептом или расходуемыми материалами.")
            await self.db.execute_query("DELETE FROM recipe_learning_requirements WHERE recipe_id=?", (recipe_id,))
            if scroll_resource_id is not None:
                await self.db.execute_query(
                    "INSERT INTO recipe_learning_requirements(recipe_id,scroll_resource_id) VALUES (?,?)",
                    (recipe_id, scroll_resource_id),
                )

    async def delete_recipe_bundle(self, recipe_id: int, *, delete_scroll: bool = False) -> None:
        """Explicit formula deletion; optional scroll deletion includes its drop links."""
        positive_integer(recipe_id, "Рецепт")
        async with self.db.transaction():
            scroll = await self.db.get_recipe_learning_scroll(recipe_id)
            await self.db.delete_recipe(recipe_id)
            if delete_scroll and scroll is not None:
                dependencies = await self.db.get_resource_dependencies(scroll["id"])
                if (
                    dependencies["ingredient_recipe_ids"]
                    or dependencies["learning_recipe_ids"]
                    or dependencies["result_recipe_ids"]
                ):
                    raise DomainError("Свиток используется в других рецептах; удаление отменено.")
                await self.db.execute_query(
                    "DELETE FROM drops WHERE item_type='resource' AND item_id=?", (scroll["id"],)
                )
                await self.db.delete_resource(scroll["id"])

    async def get_recipe_resource_choices(
        self, kind: Literal["material", "alchemy_result", "scroll"], *, exclude_result_id: int | None = None
    ) -> list[ResourceRow]:
        filters = {
            "material": "r.type!='scroll_recipe'",
            "alchemy_result": "r.type='alchemy' AND NOT EXISTS(SELECT 1 FROM recipes rec WHERE rec.result_type='resource' AND rec.result_id=r.id)",
            "scroll": "r.type='scroll_recipe'",
        }
        if kind not in filters:
            raise DomainError("Неизвестный набор ресурсов.")
        rows = await self.db.execute_query(
            f"SELECT r.id,r.name,r.emoji,r.type,r.note,r.code FROM resources r WHERE {filters[kind]} AND r.id IS NOT ? ORDER BY NORMALIZE_IDENTITY(r.name),r.id",
            (exclude_result_id,),
        )
        return [_resource_row(row) for row in rows]

    async def _delete_recipes_by_result(self, result_type: str, result_id: int) -> None:
        recipes = await self.db.execute_query(
            "SELECT id FROM recipes WHERE result_type = ? AND result_id = ?",
            (result_type, result_id),
        )
        for recipe in recipes:
            recipe_id = recipe["id"]
            await self.db.execute_query("DELETE FROM recipe_ingredients WHERE recipe_id = ?", (recipe_id,))
            await self.db.execute_query("DELETE FROM recipe_owners WHERE recipe_id = ?", (recipe_id,))
        await self.db.execute_query(
            "DELETE FROM recipes WHERE result_type = ? AND result_id = ?",
            (result_type, result_id),
        )

    async def _validate_resource_graph(self) -> None:
        rows = await self.db.execute_query("""
            SELECT rec.result_id, ri.resource_id, ri.quantity
            FROM recipes rec JOIN recipe_ingredients ri ON ri.recipe_id=rec.id
            WHERE rec.result_type='resource'
        """)
        graph: dict[int, set[int]] = {}
        for row in rows:
            positive_integer(row["quantity"], "Количество материала")
            graph.setdefault(sql_int(row["result_id"]), set()).add(sql_int(row["resource_id"]))
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
                    raise DomainError("Рецепт содержит собственный результат или циклическую цепочку материалов.")
                elif node not in finished:
                    active.add(node)
                    stack.append((node, True))
                    stack.extend((child, False) for child in graph.get(node, set()))
