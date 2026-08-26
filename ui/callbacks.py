from __future__ import annotations

import re
from dataclasses import dataclass

from aiogram.filters.callback_data import CallbackData

from game_constants import GEAR_SLOTS, RARITY_KEYS


RESOURCE_TYPES = frozenset({
    "craft",
    "consumable",
    "scroll_recipe",
    "currency",
    "alchemy",
})


class EntityNavigateCallback(CallbackData, prefix="entity"):
    """Compact callback used by inline link-style RichMessage navigation."""

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
        if not value.startswith("view_mobs_"):
            return None
        try:
            mob_id, location_id, page = map(
                int,
                value.removeprefix("view_mobs_").split("_"),
            )
        except ValueError:
            return None
        if min(mob_id, location_id, page) < 1:
            return None
        return cls(mob_id, location_id, page)


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
        prefix = next(
            (
                candidate
                for candidate in ("view_resources_", "nav_resources_")
                if value.startswith(candidate)
            ),
            None,
        )
        if prefix is None:
            return None
        try:
            resource_id, location_id, page = map(
                int,
                value.removeprefix(prefix).split("_"),
            )
        except ValueError:
            return None
        if min(resource_id, location_id, page) < 1:
            return None
        return cls(resource_id, location_id, page)


@dataclass(frozen=True)
class CardViewCallback:
    card_id: int
    page: int = 1

    def pack(self) -> str:
        return f"view_card_{self.card_id}_{self.page}"

    @classmethod
    def parse(cls, value: str) -> CardViewCallback | None:
        if not value.startswith("view_card_"):
            return None
        try:
            card_id, page = map(int, value.removeprefix("view_card_").split("_"))
        except ValueError:
            return None
        if min(card_id, page) < 1:
            return None
        return cls(card_id, page)


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
            gear_id < 1
            or rarity not in RARITY_KEYS
            or page < 1
            or (slot_index is not None and not 0 <= slot_index < len(GEAR_SLOTS))
        ):
            return None
        return cls(gear_id, rarity, slot_index, page)


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
        return (
            f"recipe_{self.action}_{self.recipe_id}_{self.gear_id}_"
            f"{self.rarity}_{slot}_{self.page}"
        )

    @classmethod
    def parse(cls, value: str) -> RecipeOwnerCallback | None:
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
            recipe_id < 1
            or gear_id < 1
            or rarity not in RARITY_KEYS
            or page < 1
            or (slot_index is not None and not 0 <= slot_index < len(GEAR_SLOTS))
        ):
            return None
        return cls(action, recipe_id, gear_id, rarity, slot_index, page)


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
        prefix = next(
            (
                candidate
                for candidate in ("view_resource_", "nav_resource_")
                if value.startswith(candidate)
            ),
            None,
        )
        if prefix is None:
            return None
        try:
            raw_id, remainder = value[len(prefix):].split("_", 1)
            resource_type, raw_page = remainder.rsplit("_", 1)
            resource_id = int(raw_id)
            page = int(raw_page)
        except (ValueError, AttributeError):
            return None
        if resource_id < 1 or page < 1 or resource_type not in RESOURCE_TYPES:
            return None
        return cls(resource_id, resource_type, page)


def parse_resource_page(value: str, prefix: str) -> tuple[str, int] | None:
    if not value.startswith(prefix):
        return None
    try:
        resource_type, raw_page = value[len(prefix):].rsplit("_", 1)
        page = int(raw_page)
    except (ValueError, AttributeError):
        return None
    if resource_type not in RESOURCE_TYPES or page < 1:
        return None
    return resource_type, page


def parse_return_context(value: str | None) -> dict | None:
    """Parse and validate a deep-link return context."""
    if not value:
        return None

    patterns = (
        ("gear", r"gear_(\d+)_(common|rare|epic|legendary)_(\d+)"),
        ("mob", r"mob_(\d+)_(\d+)_(\d+)"),
        ("resource_loc", r"resource_loc_(\d+)_(\d+)_(\d+)"),
        (
            "resource_type",
            r"resource_type_(\d+)_(craft|consumable|scroll_recipe|currency|alchemy)_(\d+)",
        ),
    )
    for kind, pattern in patterns:
        match = re.fullmatch(pattern, value)
        if not match:
            continue
        groups = match.groups()
        page = int(groups[-1])
        item_id = int(groups[0])
        if page < 1 or item_id < 1:
            return None

        result = {"kind": kind, "item_id": item_id, "page": page}
        if kind == "gear":
            result.update(context_type="gear", context_id=item_id, rarity=groups[1])
        elif kind == "mob":
            location_id = int(groups[1])
            if location_id < 1:
                return None
            result.update(
                context_type="mob",
                context_id=item_id,
                location_id=location_id,
            )
        elif kind == "resource_loc":
            location_id = int(groups[1])
            if location_id < 1:
                return None
            result.update(context_type="location", context_id=location_id)
        else:
            result.update(context_type="type", context_id=groups[1])
        return result
    return None
