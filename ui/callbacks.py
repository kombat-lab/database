from __future__ import annotations

from dataclasses import dataclass

from aiogram.filters.callback_data import CallbackData

from typing import NotRequired, TypedDict
import re
from game_constants import GEAR_SLOTS as GEAR_SLOT_ORDER, RARITY_KEYS as RARITY_ORDER, RESOURCE_TYPE_KEYS


RESOURCE_TYPES = frozenset(
    {
        "craft",
        "consumable",
        "scroll_recipe",
        "currency",
        "alchemy",
    }
)


class EntityNavigateCallback(CallbackData, prefix="entity"):
    """Compact callback used by inline link-style RichMessage navigation."""

    entity_type: str
    entity_id: int
    source_type: str
    source_id: int


class EntityBackCallback(CallbackData, prefix="entity_back"):
    """Back navigation with a stateless fallback for expired history."""

    entity_type: str
    entity_id: int


@dataclass(frozen=True)
class MobViewCallback:
    mob_id: int
    location_id: int
    page: int

    def pack(self) -> str:
        return f"view_mobs_{self.mob_id}_{self.location_id}_{self.page}"

    @classmethod
    def parse(cls, value: str) -> MobViewCallback | None:
        parsed = parse_location_callback(value)
        if parsed is None or parsed[0] != "mobs" or parsed[1] is None:
            return None
        return cls(parsed[1], parsed[2], parsed[3])


@dataclass(frozen=True)
class ResourceLocationViewCallback:
    resource_id: int
    location_id: int
    page: int

    def pack(self, *, navigation: bool = False) -> str:
        prefix = "nav_resources" if navigation else "view_resources"
        return f"{prefix}_{self.resource_id}_{self.location_id}_{self.page}"

    @classmethod
    def parse(cls, value: str) -> ResourceLocationViewCallback | None:
        parsed = parse_location_callback(value)
        if parsed is None or parsed[0] != "resources" or parsed[1] is None:
            return None
        return cls(parsed[1], parsed[2], parsed[3])


@dataclass(frozen=True)
class CardViewCallback:
    card_id: int
    page: int = 1

    def pack(self) -> str:
        return f"view_card_{self.card_id}_{self.page}"

    @classmethod
    def parse(cls, value: str) -> CardViewCallback | None:
        parsed = parse_card_callback(value)
        if parsed is None or parsed[0] is None:
            return None
        return cls(parsed[0], parsed[1])


@dataclass(frozen=True)
class GearViewCallback:
    gear_id: int
    rarity: str
    slot_index: int | None
    page: int

    def pack(self, *, navigation: bool = False) -> str:
        prefix = "nav_gear" if navigation else "view_gear"
        parts = [prefix, str(self.gear_id), self.rarity]
        if self.slot_index is not None:
            parts.append(str(self.slot_index))
        parts.append(str(self.page))
        return "_".join(parts)

    @classmethod
    def parse(cls, value: str) -> GearViewCallback | None:
        parsed = parse_gear_view_callback(value)
        return cls(*parsed) if parsed is not None else None


@dataclass(frozen=True)
class RecipeOwnerCallback:
    action: str
    recipe_id: int
    gear_id: int
    rarity: str
    slot_index: int | None
    page: int

    def pack(self) -> str:
        slot = str(self.slot_index) if self.slot_index is not None else "x"
        return f"recipe_{self.action}_{self.recipe_id}_{self.gear_id}_{self.rarity}_{slot}_{self.page}"

    @classmethod
    def parse(cls, value: str) -> RecipeOwnerCallback | None:
        parsed = parse_recipe_owner_callback(value)
        return cls(*parsed) if parsed is not None else None


@dataclass(frozen=True)
class ResourceViewCallback:
    resource_id: int
    resource_type: str
    page: int

    def pack(self, *, navigation: bool = False) -> str:
        prefix = "nav_resource" if navigation else "view_resource"
        return f"{prefix}_{self.resource_id}_{self.resource_type}_{self.page}"

    @classmethod
    def parse(cls, value: str) -> ResourceViewCallback | None:
        parsed = parse_resource_view_callback(value)
        return cls(*parsed) if parsed is not None else None


def parse_resource_page(value: str, prefix: str) -> tuple[str, int] | None:
    return parse_resource_page_callback(value, prefix)


def parse_return_context(value: str | None) -> ReturnContext | None:
    """Keep the component API aligned with the validated legacy links."""
    return parse_return_param(value)


# Canonical parsers also accept the historic Telegram payloads.
MAX_SQLITE_ID = 2**63 - 1
ITEMS_PER_PAGE = 10


def build_resource_return_param(
    resource_id: int,
    context_type: str | None,
    context_id: int | str | None,
    page: int,
) -> str | None:
    if context_id is None:
        return None
    if context_type == "location":
        return f"resource_loc_{resource_id}_{context_id}_{page}"
    if context_type == "type":
        return f"resource_type_{resource_id}_{context_id}_{page}"
    return None


def build_gear_return_param(
    gear_id: int,
    rarity: str | None,
    page: int,
    slot_index: int | None = None,
) -> str | None:
    if rarity is None:
        return None
    slot = f"{slot_index}_" if slot_index is not None else ""
    return f"gear_{gear_id}_{rarity}_{slot}{page}"


def parse_gear_view_callback(value: str) -> tuple[int, str, int | None, int] | None:
    for prefix in ("view_gear_", "nav_gear_"):
        if value.startswith(prefix):
            parts = value.removeprefix(prefix).split("_")
            break
    else:
        return None

    if len(parts) not in (3, 4):
        return None
    try:
        gear_id = int(parts[0])
        rarity = parts[1]
        slot_index = int(parts[2]) if len(parts) == 4 else None
        page = int(parts[-1])
    except ValueError:
        return None
    if (
        not 1 <= gear_id <= MAX_SQLITE_ID
        or rarity not in RARITY_ORDER
        or not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE
        or (slot_index is not None and not 0 <= slot_index < len(GEAR_SLOT_ORDER))
    ):
        return None
    return gear_id, rarity, slot_index, page


def build_recipe_owner_callback(
    action: str,
    recipe_id: int,
    gear_id: int,
    rarity: str,
    page: int,
    slot_index: int | None,
) -> str:
    slot = str(slot_index) if slot_index is not None else "x"
    return f"recipe_{action}_{recipe_id}_{gear_id}_{rarity}_{slot}_{page}"


def parse_recipe_owner_callback(
    value: str,
) -> tuple[str, int, int, str, int | None, int] | None:
    parts = value.split("_")
    if len(parts) not in (6, 7) or parts[0] != "recipe":
        return None
    action = parts[1]
    if action not in {"claim", "relinquish"}:
        return None
    try:
        recipe_id = int(parts[2])
        gear_id = int(parts[3])
        rarity = parts[4]
        if len(parts) == 7:
            slot_index = None if parts[5] == "x" else int(parts[5])
            page = int(parts[6])
        else:
            slot_index = None
            page = int(parts[5])
    except ValueError:
        return None
    if (
        not 1 <= recipe_id <= MAX_SQLITE_ID
        or not 1 <= gear_id <= MAX_SQLITE_ID
        or rarity not in RARITY_ORDER
        or not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE
        or (slot_index is not None and not 0 <= slot_index < len(GEAR_SLOT_ORDER))
    ):
        return None
    return action, recipe_id, gear_id, rarity, slot_index, page


class ReturnContext(TypedDict):
    kind: str
    item_id: int
    page: int
    context_type: str
    context_id: int | str
    rarity: NotRequired[str]
    slot_index: NotRequired[int]
    location_id: NotRequired[int]


def parse_return_param(value: str | None) -> ReturnContext | None:
    """Parse and validate the optional navigation context in a start payload."""
    if not value or len(value) > 64:
        return None
    if value.startswith("gear_"):
        parsed = parse_gear_view_callback(f"view_{value}")
        if not parsed:
            return None
        item_id, rarity, slot_index, page = parsed
        gear_context: ReturnContext = {
            "kind": "gear",
            "item_id": item_id,
            "page": page,
            "context_type": "gear",
            "context_id": item_id,
            "rarity": rarity,
        }
        if slot_index is not None:
            gear_context["slot_index"] = slot_index
        return gear_context

    if value.startswith("card_"):
        card = parse_card_callback(f"view_{value}")
        if card is None or card[0] is None:
            return None
        return {"kind": "card", "item_id": card[0], "page": card[1], "context_type": "card", "context_id": card[0]}

    patterns = (
        ("mob", r"mob_(\d+)_(\d+)_(\d+)"),
        ("resource_loc", r"resource_loc_(\d+)_(\d+)_(\d+)"),
        ("resource_type", r"resource_type_(\d+)_(craft|consumable|scroll_recipe|currency|alchemy)_(\d+)"),
    )
    for kind, pattern in patterns:
        match = re.fullmatch(pattern, value)
        if not match:
            continue
        groups = match.groups()
        page = int(groups[-1])
        if not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE:
            return None
        item_id = int(groups[0])
        if not 1 <= item_id <= MAX_SQLITE_ID:
            return None

        result: ReturnContext = {
            "kind": kind,
            "item_id": item_id,
            "page": page,
            "context_type": "",
            "context_id": item_id,
        }
        if kind == "mob":
            location_id = int(groups[1])
            if not 1 <= location_id <= MAX_SQLITE_ID:
                return None
            result["context_type"] = "mob"
            result["location_id"] = location_id
        elif kind == "resource_loc":
            location_id = int(groups[1])
            if not 1 <= location_id <= MAX_SQLITE_ID:
                return None
            result["context_type"] = "location"
            result["context_id"] = location_id
        else:
            result["context_type"] = "type"
            result["context_id"] = groups[1]
        return result
    return None


def parse_resource_page_callback(data: str, prefix: str) -> tuple[str, int] | None:
    """Parse callbacks whose resource type may itself contain underscores."""
    if not data.startswith(prefix):
        return None
    try:
        resource_type, raw_page = data[len(prefix) :].rsplit("_", 1)
        page = int(raw_page)
    except (ValueError, AttributeError):
        return None
    if resource_type not in RESOURCE_TYPE_KEYS or not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE:
        return None
    return resource_type, page


def parse_resource_view_callback(data: str) -> tuple[int, str, int] | None:
    prefix = next(
        (candidate for candidate in ("view_resource_", "nav_resource_") if data.startswith(candidate)),
        None,
    )
    if prefix is None:
        return None
    try:
        raw_id, remainder = data[len(prefix) :].split("_", 1)
        resource_type, raw_page = remainder.rsplit("_", 1)
        resource_id = int(raw_id)
        page = int(raw_page)
    except (ValueError, AttributeError):
        return None
    if (
        not 1 <= resource_id <= MAX_SQLITE_ID
        or not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE
        or resource_type not in RESOURCE_TYPE_KEYS
    ):
        return None
    return resource_id, resource_type, page


def parse_location_callback(value: str) -> tuple[str, int | None, int, int] | None:
    """Return category, optional item ID, location and page for location screens."""
    match = re.fullmatch(r"(?:list|page)_(mobs|resources)_(\d+)_(\d+)", value)
    item_id: int | None = None
    if match:
        category, raw_location, raw_page = match.groups()
    else:
        match = re.fullmatch(r"(?:view|nav)_(mobs|resources)_(\d+)_(\d+)_(\d+)", value)
        if not match:
            return None
        category, raw_item, raw_location, raw_page = match.groups()
        item_id = int(raw_item)
        if not 1 <= item_id <= MAX_SQLITE_ID:
            return None
    location, page = int(raw_location), int(raw_page)
    if not 1 <= location <= MAX_SQLITE_ID or not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE:
        return None
    return category, item_id, location, page


def parse_gear_list_callback(value: str) -> tuple[str, int, int] | None:
    match = re.fullmatch(r"gear_slot_(common|rare|epic|legendary)_(\d+)", value)
    if match:
        rarity, raw_slot = match.groups()
        page = 1
    else:
        match = re.fullmatch(r"page_gear_(common|rare|epic|legendary)_(\d+)_(\d+)", value)
        if not match:
            return None
        rarity, raw_slot, raw_page = match.groups()
        page = int(raw_page)
    slot_index = int(raw_slot)
    if not 0 <= slot_index < len(GEAR_SLOT_ORDER) or not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE:
        return None
    return rarity, slot_index, page


def parse_card_callback(value: str) -> tuple[int | None, int] | None:
    match = re.fullmatch(r"cards_page_(\d+)", value)
    card_id: int | None = None
    if match:
        page = int(match.group(1))
    else:
        match = re.fullmatch(r"view_card_(\d+)(?:_(\d+))?", value)
        if not match:
            return None
        card_id = int(match.group(1))
        page = int(match.group(2) or "1")
        if not 1 <= card_id <= MAX_SQLITE_ID:
            return None
    if not 1 <= page <= MAX_SQLITE_ID // ITEMS_PER_PAGE:
        return None
    return card_id, page


def return_button_data(context: ReturnContext) -> tuple[str, str]:
    item_id, page = context["item_id"], context["page"]
    kind = context["kind"]
    if kind == "gear":
        slot_index = context.get("slot_index")
        slot = f"{slot_index}_" if slot_index is not None else ""
        return f"view_gear_{item_id}_{context['rarity']}_{slot}{page}", "🔙 Вернуться к снаряжению"
    if kind == "mob":
        return f"view_mobs_{item_id}_{context['location_id']}_{page}", "🔙 Вернуться к мобу"
    if kind == "resource_loc":
        return f"view_resources_{item_id}_{context['context_id']}_{page}", "🔙 Вернуться к ресурсу"
    if kind == "card":
        return f"view_card_{item_id}_{page}", "🔙 Вернуться к карте"
    return f"view_resource_{item_id}_{context['context_id']}_{page}", "🔙 Вернуться к ресурсу"
