"""Explicit decoding at the SQL/catalog boundary."""

from storage.types import sql_int, sql_text

import json
from catalog_types import (
    ItemRow,
    ResourceRow,
    GearRow,
    CardRow,
    MobRow,
    RecipeIngredientRow,
    RecipeOwnerEntry,
    SearchItem,
)
from storage.types import DbRow, sql_row


def _item_row(row: DbRow) -> ItemRow:
    return ItemRow(id=sql_int(row["id"]), name=sql_text(row["name"]), emoji=sql_text(row["emoji"]))


def _gear_row(row: DbRow) -> GearRow:
    return GearRow(
        **_item_row(row),
        rarity=sql_text(row["rarity"]),
        slot=sql_text(row["slot"]),
        level=sql_int(row["level"]),
        classes=sql_text(row["classes"]),
        note=sql_text(row["note"]),
    )


def _resource_row(row: DbRow) -> ResourceRow:
    return ResourceRow(
        **_item_row(row),
        type=sql_text(row["type"]),
        note=sql_text(row["note"]),
        code=sql_text(row["code"]) if row.get("code") is not None else None,
    )


def _card_row(row: DbRow) -> CardRow:
    return CardRow(
        **_item_row(row),
        slot=sql_text(row["slot"]),
        bonus1=sql_text(row["bonus1"]),
        bonus2=sql_text(row["bonus2"]),
        bonus3=sql_text(row["bonus3"]),
        bonus4=sql_text(row["bonus4"]),
        note=sql_text(row["note"]),
    )


def _mob_row(row: DbRow) -> MobRow:
    return MobRow(
        **_item_row(row),
        hp=sql_int(row["hp"]),
        dust_min=sql_int(row["dust_min"]),
        dust_max=sql_int(row["dust_max"]),
        exp=sql_int(row["exp"]),
        location_id=sql_int(row["location_id"]),
        location_name=sql_text(row["location_name"]),
        location_emoji=sql_text(row["location_emoji"]),
    )


def _json_rows(value: object) -> list[DbRow]:
    decoded: object = json.loads(sql_text(value) if value is not None else "[]")
    if not isinstance(decoded, list):
        raise ValueError("Expected a JSON array from the catalog query")
    rows: list[DbRow] = []
    for item in decoded:
        if not isinstance(item, dict) or not all(isinstance(key, str) for key in item):
            raise ValueError("Expected JSON objects from the catalog query")
        rows.append(sql_row(item))
    return rows


def _recipe_ingredient(row: DbRow) -> RecipeIngredientRow:
    return RecipeIngredientRow(
        resource_id=sql_int(row["resource_id"]),
        name=sql_text(row["name"]),
        emoji=sql_text(row["emoji"]),
        quantity=sql_int(row["quantity"]),
        code=sql_text(row["code"]) if row.get("code") is not None else None,
    )


def _owner_entry(row: DbRow) -> RecipeOwnerEntry:
    return RecipeOwnerEntry(
        owner_id=sql_int(row["owner_id"]),
        user_id=sql_int(row["user_id"]) if row["user_id"] is not None else None,
        player_username=sql_text(row["player_username"]) if row["player_username"] is not None else None,
    )


def _search_item(row: DbRow) -> SearchItem:
    item: SearchItem = {
        "id": sql_int(row["id"]),
        "name": sql_text(row["name"]),
        "emoji": sql_text(row["emoji"]),
    }
    if "location_name" in row:
        item["location_name"] = sql_text(row["location_name"]) if row["location_name"] is not None else None
    if "location_emoji" in row:
        item["location_emoji"] = sql_text(row["location_emoji"]) if row["location_emoji"] is not None else None
    if "hp" in row:
        item["hp"] = sql_int(row["hp"])
    if "dust_min" in row:
        item["dust_min"] = sql_int(row["dust_min"])
    if "dust_max" in row:
        item["dust_max"] = sql_int(row["dust_max"])
    if "exp" in row:
        item["exp"] = sql_int(row["exp"])
    if "rarity" in row:
        item["rarity"] = sql_text(row["rarity"])
    if "slot" in row:
        item["slot"] = sql_text(row["slot"])
    if "type" in row:
        item["type"] = sql_text(row["type"])
    return item
