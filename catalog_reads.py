"""Bounded, batched catalog read models used by inline search.

A page is read in one SQLite snapshot. Builders consume the detached snapshot,
so rendering 50 results never executes a query per result.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from catalog_types import (
    CardRow,
    CardDropRow,
    GearCardRow,
    GearDropRow,
    GearIngredientRow,
    ItemRow,
    LearningRecipeRow,
    MobCardRow,
    RecipeIngredientRow,
    RecipeOwnerEntry,
    ResourceCardRow,
    ResourceDropMobRow,
    ResourceRecipeRow,
    ResourceRow,
    ResourceUsageRow,
    SearchItem,
)
from database import Database

EntityKind: TypeAlias = Literal["mob", "resource", "gear", "card"]


def integer(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Invalid integer in catalog column {key}")
    return value


def text(row: Mapping[str, object], key: str) -> str:
    value = row[key]
    if not isinstance(value, str):
        raise ValueError(f"Invalid text in catalog column {key}")
    return value


def item(row: Mapping[str, object]) -> ItemRow:
    return ItemRow(id=integer(row, "id"), name=text(row, "name"), emoji=text(row, "emoji"))


def resource(row: Mapping[str, object]) -> ResourceRow:
    code = row.get("code")
    return ResourceRow(
        **item(row), type=text(row, "type"), note=text(row, "note"), code=code if isinstance(code, str) else None
    )


def placeholders(ids: list[int]) -> str:
    return ",".join("?" for _ in ids) or "NULL"


@dataclass(frozen=True, slots=True)
class SearchEntry:
    kind: EntityKind
    item: SearchItem


@dataclass(slots=True)
class CatalogSnapshot:
    mobs: dict[int, MobCardRow] = field(default_factory=dict)
    resources: dict[int, ResourceCardRow] = field(default_factory=dict)
    gear: dict[int, GearCardRow] = field(default_factory=dict)
    cards: dict[int, CardRow] = field(default_factory=dict)
    resource_recipes: dict[int, ResourceRecipeRow] = field(default_factory=dict)
    card_sources: dict[int, list[ResourceDropMobRow]] = field(default_factory=dict)

    async def get_mob_full_card(self, mob_id: int) -> MobCardRow | None:
        return self.mobs.get(mob_id)

    async def get_resource_card(self, resource_id: int) -> ResourceCardRow | None:
        return self.resources.get(resource_id)

    async def get_recipe_for_resource(self, resource_id: int) -> ResourceRecipeRow | None:
        return self.resource_recipes.get(resource_id)

    async def get_gear_card(self, gear_id: int) -> GearCardRow | None:
        return self.gear.get(gear_id)

    async def get_card_by_id(self, card_id: int) -> CardRow | None:
        return self.cards.get(card_id)

    async def get_card_drop_mobs(self, card_id: int) -> list[ResourceDropMobRow]:
        return self.card_sources.get(card_id, [])


async def gear_slot_counts(database: Database, rarity: str) -> dict[str, int]:
    rows = await database.execute_query(
        "SELECT slot, COUNT(*) AS item_count FROM gear WHERE rarity = ? GROUP BY slot",
        (rarity,),
    )
    return {text(row, "slot"): integer(row, "item_count") for row in rows}


async def search_all(database: Database, query: str, *, offset: int = 0, limit: int = 51) -> list[SearchEntry]:
    if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 51 or not 2 <= len(query) <= 256:
        raise ValueError("Invalid catalog search page")
    rows = await database.execute_query(
        """
        WITH catalog AS (
            SELECT 'mob' kind,m.id,m.name,m.emoji,m.hp,m.exp,'' slot,'' rarity,
                   l.name location_name,l.emoji location_emoji
            FROM mobs m JOIN locations l ON l.id=m.location_id
            UNION ALL SELECT 'resource',id,name,emoji,0,0,'','',NULL,NULL FROM resources
            UNION ALL SELECT 'gear',id,name,emoji,0,0,slot,rarity,NULL,NULL FROM gear
            UNION ALL SELECT 'card',id,name,emoji,0,0,slot,'',NULL,NULL FROM cards
        )
        SELECT * FROM catalog WHERE INSTR(LOWER_UNICODE(name),LOWER_UNICODE(?))>0
        ORDER BY CASE WHEN LOWER_UNICODE(name)=LOWER_UNICODE(?) THEN 0
                      WHEN INSTR(LOWER_UNICODE(name),LOWER_UNICODE(?))=1 THEN 1 ELSE 2 END,
                 LOWER_UNICODE(name),kind,id LIMIT ? OFFSET ?
    """,
        (query, query, query, limit, offset),
    )
    result: list[SearchEntry] = []
    for row in rows:
        kind = text(row, "kind")
        if kind not in {"mob", "resource", "gear", "card"}:
            raise ValueError("Invalid catalog kind")
        record = SearchItem(
            id=integer(row, "id"),
            name=text(row, "name"),
            emoji=text(row, "emoji"),
            hp=integer(row, "hp"),
            exp=integer(row, "exp"),
            slot=text(row, "slot"),
            rarity=text(row, "rarity"),
        )
        if row["location_name"] is not None:
            record["location_name"] = text(row, "location_name")
            record["location_emoji"] = text(row, "location_emoji")
        if kind == "mob":
            result.append(SearchEntry("mob", record))
        elif kind == "resource":
            result.append(SearchEntry("resource", record))
        elif kind == "gear":
            result.append(SearchEntry("gear", record))
        else:
            result.append(SearchEntry("card", record))
    return result


async def load_snapshot(database: Database, entries: list[SearchEntry]) -> CatalogSnapshot:
    """Load at most a page and its links; caller owns the read transaction."""
    if len(entries) > 50:
        raise ValueError("A catalog snapshot is limited to 50 results")
    snapshot = CatalogSnapshot()
    mob_ids = [e.item["id"] for e in entries if e.kind == "mob"]
    resource_ids = [e.item["id"] for e in entries if e.kind == "resource"]
    gear_ids = [e.item["id"] for e in entries if e.kind == "gear"]
    card_ids = [e.item["id"] for e in entries if e.kind == "card"]
    if mob_ids:
        for row in await database.execute_query(
            f"""
            SELECT m.*,l.name loc_name,l.emoji loc_emoji FROM mobs m
            JOIN locations l ON l.id=m.location_id WHERE m.id IN ({placeholders(mob_ids)})
        """,
            tuple(mob_ids),
        ):
            snapshot.mobs[integer(row, "id")] = MobCardRow(
                **item(row),
                hp=integer(row, "hp"),
                exp=integer(row, "exp"),
                dust_min=integer(row, "dust_min"),
                dust_max=integer(row, "dust_max"),
                location_id=integer(row, "location_id"),
                loc_name=text(row, "loc_name"),
                loc_emoji=text(row, "loc_emoji"),
                resource_drops=[],
                gear_drops=[],
                card_drops=[],
            )
    if resource_ids:
        for row in await database.execute_query(
            f"SELECT * FROM resources WHERE id IN ({placeholders(resource_ids)})", tuple(resource_ids)
        ):
            snapshot.resources[integer(row, "id")] = ResourceCardRow(
                **resource(row), craft_location="", mobs=[], used_in=[], learning_recipes=[]
            )
    if gear_ids:
        for row in await database.execute_query(
            f"SELECT * FROM gear WHERE id IN ({placeholders(gear_ids)})", tuple(gear_ids)
        ):
            snapshot.gear[integer(row, "id")] = GearCardRow(
                **item(row),
                rarity=text(row, "rarity"),
                slot=text(row, "slot"),
                level=integer(row, "level"),
                classes=text(row, "classes"),
                note=text(row, "note"),
                craft_quantity=1,
                recipe_id=None,
                mobs=[],
                scroll_mobs=[],
                ingredients=[],
                owners=[],
                owner_entries=[],
                owner_user_ids=[],
                craftable=False,
                learning_scroll=None,
                can_learn=False,
            )
    if card_ids:
        for row in await database.execute_query(
            f"SELECT * FROM cards WHERE id IN ({placeholders(card_ids)})", tuple(card_ids)
        ):
            snapshot.cards[integer(row, "id")] = CardRow(
                **item(row),
                slot=text(row, "slot"),
                note=text(row, "note"),
                bonus1=text(row, "bonus1"),
                bonus2=text(row, "bonus2"),
                bonus3=text(row, "bonus3"),
                bonus4=text(row, "bonus4"),
            )
    recipes = (
        await database.execute_query(
            f"""
        SELECT rec.*,lr.scroll_resource_id,g.name result_name,g.emoji result_emoji,g.rarity result_rarity,
               s.name scroll_name,s.emoji scroll_emoji,s.note scroll_note,s.code scroll_code
        FROM recipes rec LEFT JOIN recipe_learning_requirements lr ON lr.recipe_id=rec.id
        LEFT JOIN resources s ON s.id=lr.scroll_resource_id
        LEFT JOIN gear g ON rec.result_type='gear' AND g.id=rec.result_id
        WHERE (rec.result_type='gear' AND rec.result_id IN ({placeholders(gear_ids)}))
           OR (rec.result_type='resource' AND rec.result_id IN ({placeholders(resource_ids)}))
           OR lr.scroll_resource_id IN ({placeholders(resource_ids)}) ORDER BY rec.id
    """,
            tuple([*gear_ids, *resource_ids, *resource_ids]),
        )
        if gear_ids or resource_ids
        else []
    )
    recipe_ids = [integer(row, "id") for row in recipes]
    ingredients: dict[int, list[RecipeIngredientRow]] = {}
    gear_ingredients: dict[int, list[GearIngredientRow]] = {}
    owners: dict[int, list[RecipeOwnerEntry]] = {}
    if recipe_ids:
        for row in await database.execute_query(
            f"""
            SELECT ri.recipe_id,ri.resource_id,r.name,r.emoji,r.type,r.code,ri.quantity
            FROM recipe_ingredients ri JOIN resources r ON r.id=ri.resource_id
            WHERE ri.recipe_id IN ({placeholders(recipe_ids)}) ORDER BY ri.resource_id
        """,
            tuple(recipe_ids),
        ):
            recipe_id = integer(row, "recipe_id")
            code = row["code"]
            ingredients.setdefault(recipe_id, []).append(
                RecipeIngredientRow(
                    resource_id=integer(row, "resource_id"),
                    name=text(row, "name"),
                    emoji=text(row, "emoji"),
                    quantity=integer(row, "quantity"),
                    code=code if isinstance(code, str) else None,
                )
            )
            gear_ingredients.setdefault(recipe_id, []).append(
                GearIngredientRow(
                    id=integer(row, "resource_id"),
                    name=text(row, "name"),
                    emoji=text(row, "emoji"),
                    type=text(row, "type"),
                    quantity=integer(row, "quantity"),
                )
            )
        for row in await database.execute_query(
            f"SELECT * FROM recipe_owners WHERE recipe_id IN ({placeholders(recipe_ids)}) ORDER BY LOWER_UNICODE(player_username),owner_id",
            tuple(recipe_ids),
        ):
            owners.setdefault(integer(row, "recipe_id"), []).append(
                RecipeOwnerEntry(
                    owner_id=integer(row, "owner_id"),
                    user_id=integer(row, "user_id") if row["user_id"] is not None else None,
                    player_username=text(row, "player_username") if row["player_username"] is not None else None,
                )
            )
    scroll_to_gear: dict[int, list[int]] = {}
    for row in recipes:
        recipe_id, result_id = integer(row, "id"), integer(row, "result_id")
        entries_owners = owners.get(recipe_id, [])
        user_ids = [owner["user_id"] for owner in entries_owners if owner["user_id"] is not None]
        scroll_id = integer(row, "scroll_resource_id") if row["scroll_resource_id"] is not None else None
        if row["result_type"] == "resource" and result_id in snapshot.resources:
            snapshot.resource_recipes[result_id] = ResourceRecipeRow(
                quantity=integer(row, "quantity"),
                craft_location=text(row, "craft_location"),
                ingredients=ingredients.get(recipe_id, []),
            )
            snapshot.resources[result_id]["craft_location"] = text(row, "craft_location")
        if row["result_type"] == "gear" and result_id in snapshot.gear:
            gear = snapshot.gear[result_id]
            gear["recipe_id"] = recipe_id
            gear["craft_quantity"] = integer(row, "quantity")
            gear["craftable"] = True
            gear["ingredients"] = sorted(
                gear_ingredients.get(recipe_id, []), key=lambda i: (i["name"].lower(), i["id"])
            )
            gear["owner_entries"] = entries_owners
            gear["owner_user_ids"] = user_ids
            gear["owners"] = [owner["player_username"] for owner in entries_owners if owner["player_username"]]
            if scroll_id is not None:
                code = row["scroll_code"]
                gear["learning_scroll"] = ResourceRow(
                    id=scroll_id,
                    name=text(row, "scroll_name"),
                    emoji=text(row, "scroll_emoji"),
                    type="scroll_recipe",
                    note=text(row, "scroll_note"),
                    code=code if isinstance(code, str) else None,
                )
                gear["can_learn"] = True
                scroll_to_gear.setdefault(scroll_id, []).append(result_id)
        if scroll_id is not None and scroll_id in snapshot.resources and row["result_type"] == "gear":
            snapshot.resources[scroll_id]["learning_recipes"].append(
                LearningRecipeRow(
                    recipe_id=recipe_id,
                    result_id=result_id,
                    result_name=text(row, "result_name"),
                    result_emoji=text(row, "result_emoji"),
                    result_rarity=text(row, "result_rarity"),
                    quantity=integer(row, "quantity"),
                    ingredients=ingredients.get(recipe_id, []),
                    owner_entries=entries_owners,
                    owner_user_ids=user_ids,
                )
            )
    drop_resources = sorted(set(resource_ids) | set(scroll_to_gear))
    if entries:
        for row in await database.execute_query(
            f"""
            SELECT d.mob_id,d.item_type,d.item_id,m.name,m.emoji,m.location_id,
                   l.name location_name,l.emoji location_emoji,
                   COALESCE(r.name,g.name,c.name) item_name,COALESCE(r.emoji,g.emoji,c.emoji) item_emoji,
                   COALESCE(g.slot,c.slot) slot,g.rarity
            FROM drops d JOIN mobs m ON m.id=d.mob_id JOIN locations l ON l.id=m.location_id
            LEFT JOIN resources r ON d.item_type='resource' AND r.id=d.item_id
            LEFT JOIN gear g ON d.item_type='gear' AND g.id=d.item_id
            LEFT JOIN cards c ON d.item_type='card' AND c.id=d.item_id
            WHERE d.mob_id IN ({placeholders(mob_ids)})
               OR (d.item_type='resource' AND d.item_id IN ({placeholders(drop_resources)}))
               OR (d.item_type='gear' AND d.item_id IN ({placeholders(gear_ids)}))
               OR (d.item_type='card' AND d.item_id IN ({placeholders(card_ids)}))
            ORDER BY d.item_id,d.mob_id
        """,
            tuple([*mob_ids, *drop_resources, *gear_ids, *card_ids]),
        ):
            mob_id, item_id, kind = integer(row, "mob_id"), integer(row, "item_id"), text(row, "item_type")
            source = ResourceDropMobRow(
                id=mob_id,
                name=text(row, "name"),
                emoji=text(row, "emoji"),
                location_id=integer(row, "location_id"),
                location_name=text(row, "location_name"),
                location_emoji=text(row, "location_emoji"),
            )
            drop = ItemRow(id=item_id, name=text(row, "item_name"), emoji=text(row, "item_emoji"))
            if mob_id in snapshot.mobs:
                mob = snapshot.mobs[mob_id]
                if kind == "resource":
                    mob["resource_drops"].append(drop)
                elif kind == "gear":
                    mob["gear_drops"].append(GearDropRow(**drop, slot=text(row, "slot"), rarity=text(row, "rarity")))
                elif kind == "card":
                    mob["card_drops"].append(CardDropRow(**drop, slot=text(row, "slot")))
            if kind == "resource":
                if item_id in snapshot.resources:
                    snapshot.resources[item_id]["mobs"].append(source)
                for gear_id in scroll_to_gear.get(item_id, []):
                    snapshot.gear[gear_id]["scroll_mobs"].append(item(source))
            elif kind == "gear" and item_id in snapshot.gear:
                snapshot.gear[item_id]["mobs"].append(item(source))
            elif kind == "card":
                snapshot.card_sources.setdefault(item_id, []).append(source)
    if resource_ids:
        for row in await database.execute_query(
            f"""
            SELECT ri.resource_id,ri.quantity,rec.id recipe_id,rec.result_type,rec.result_id,
                   COALESCE(g.name,r.name) result_name,COALESCE(g.emoji,r.emoji) result_emoji,g.rarity result_rarity
            FROM recipe_ingredients ri JOIN recipes rec ON rec.id=ri.recipe_id
            LEFT JOIN gear g ON rec.result_type='gear' AND g.id=rec.result_id
            LEFT JOIN resources r ON rec.result_type='resource' AND r.id=rec.result_id
            WHERE ri.resource_id IN ({placeholders(resource_ids)})
            ORDER BY rec.result_type,LOWER_UNICODE(COALESCE(g.name,r.name)),rec.result_id
        """,
            tuple(resource_ids),
        ):
            snapshot.resources[integer(row, "resource_id")]["used_in"].append(
                ResourceUsageRow(
                    recipe_id=integer(row, "recipe_id"),
                    result_type=text(row, "result_type"),
                    result_id=integer(row, "result_id"),
                    result_name=text(row, "result_name"),
                    result_emoji=text(row, "result_emoji"),
                    quantity=integer(row, "quantity"),
                    result_rarity=text(row, "result_rarity") if row["result_rarity"] is not None else None,
                )
            )
    for resource_card in snapshot.resources.values():
        resource_card["mobs"].sort(key=lambda m: (m["location_id"], m["name"].casefold(), m["id"]))
    for gear_card in snapshot.gear.values():
        for key in ("mobs", "scroll_mobs"):
            gear_card[key].sort(key=lambda m: (m["name"].lower(), m["id"]))
    return snapshot
