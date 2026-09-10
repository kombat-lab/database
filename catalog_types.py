"""Shared typed shapes for catalog records and presentation boundaries."""

from typing import NotRequired, TypedDict


class ItemRow(TypedDict):
    id: int
    name: str
    emoji: str


class ResourceRow(ItemRow):
    type: str
    note: str


class GearRow(ItemRow):
    rarity: str
    slot: str
    level: int
    classes: str
    note: str


class CardRow(ItemRow):
    slot: str
    bonus1: str
    bonus2: str
    bonus3: str
    bonus4: str
    note: str


class NavigationIds(TypedDict):
    prev_id: int | None
    next_id: int | None



class RecipeOwnerEntry(TypedDict):
    owner_id: int
    user_id: int | None
    player_username: str | None


class SearchItem(ItemRow):
    location_name: NotRequired[str | None]
    location_emoji: NotRequired[str | None]
    hp: NotRequired[int]
    dust_min: NotRequired[int]
    dust_max: NotRequired[int]
    exp: NotRequired[int]
    rarity: NotRequired[str]
    slot: NotRequired[str]
    type: NotRequired[str]


class GearDropRow(ItemRow):
    slot: str
    rarity: str


class CardDropRow(ItemRow):
    slot: str


class MobCardRow(ItemRow):
    hp: int
    dust_min: int
    dust_max: int
    exp: int
    location_id: int
    loc_name: str
    loc_emoji: str
    resource_drops: list[ItemRow]
    gear_drops: list[GearDropRow]
    card_drops: list[CardDropRow]


class ResourceDropMobRow(ItemRow):
    location_id: int
    location_name: str
    location_emoji: str


class ResourceUsageRow(TypedDict):
    recipe_id: int
    result_type: str
    result_id: int
    result_name: str
    result_emoji: str
    result_rarity: str | None
    quantity: int


class ResourceCardRow(ResourceRow):
    mobs: list[ResourceDropMobRow]
    used_in: list[ResourceUsageRow]


class GearIngredientRow(ItemRow):
    type: str
    quantity: int


class GearCardRow(GearRow):
    recipe_id: int | None
    mobs: list[ItemRow]
    scroll_mobs: list[ItemRow]
    ingredients: list[GearIngredientRow]
    owners: list[str]
    owner_entries: NotRequired[list[RecipeOwnerEntry]]
    owner_user_ids: list[int]
    craftable: bool


class RecipeIngredientRow(TypedDict):
    resource_id: int
    name: str
    emoji: str
    quantity: int


class ResourceRecipeRow(TypedDict):
    ingredients: list[RecipeIngredientRow]
