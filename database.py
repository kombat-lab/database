"""Typed catalog facade over domain repositories and one SQLite unit of work.

Explicit forwarding signatures retain compatibility without dynamic delegation.
Storage modules own SQL, validation, transaction mechanics, and schema upgrades.
"""

from typing import Literal


from catalog_types import (
    LocationRow as LocationRow,
    MobRow as MobRow,
    ResourceRow as ResourceRow,
    GearRow as GearRow,
    CardRow as CardRow,
    NavigationIds as NavigationIds,
    RecipeOwnerEntry as RecipeOwnerEntry,
    SearchItem as SearchItem,
    MobCardRow as MobCardRow,
    ResourceDropMobRow as ResourceDropMobRow,
    ResourceCardRow as ResourceCardRow,
    GearCardRow as GearCardRow,
    ResourceRecipeRow as ResourceRecipeRow,
    RecipeDetailsRow as RecipeDetailsRow,
    DropItemType as DropItemType,
    MobSourceRow as MobSourceRow,
)
from storage.schema import SchemaRepository
from storage.users import UserRepository
from storage.commands import CommandRepository
from storage.search import SearchRepository
from storage.locations import LocationRepository
from storage.drops import DropRepository
from storage.mobs import MobRepository
from storage.gear import GearRepository
from storage.recipes import RecipeRepository
from storage.drafts import GearDraftRepository
from storage.resources import ResourceRepository
from storage.cards import CardRepository
from storage.sqlite import SqliteStore
from storage.types import DbRow as DbRow
from game_constants import (
    GEAR_SLOTS,
    RARITY_KEYS,
)
from recipe_domain import (
    GearDraft,
    GearDraftPayload,
    GearSaveResult,
    MaterialInput,
    ResourceDependencies,
)


class Database(SqliteStore):
    ALLOWED_MOB_FIELDS = frozenset({"name", "emoji", "hp", "dust_min", "dust_max", "exp", "location_id"})
    RESOURCE_NAME_ORDER = "LOWER_UNICODE(name), id"

    def __init__(self, path: str | None = None) -> None:
        super().__init__(path)
        self.schema = SchemaRepository(self)
        self.users = UserRepository(self)
        self.commands = CommandRepository(self)
        self.search_repository = SearchRepository(self)
        self.locations = LocationRepository(self)
        self.drops = DropRepository(self)
        self.mobs = MobRepository(self)
        self.gear = GearRepository(self)
        self.recipes = RecipeRepository(self)
        self.drafts = GearDraftRepository(self)
        self.resources = ResourceRepository(self)
        self.cards = CardRepository(self)

    async def initialize_schema(self) -> None:
        await self.schema._ensure_schema()
        await self.schema._migrate_schema()
        await self.schema._ensure_indexes()

    @staticmethod
    def _slot_order_case(column: str = "slot") -> str:
        branches = " ".join(f"WHEN '{slot}' THEN {order}" for order, slot in enumerate(GEAR_SLOTS, start=1))
        return f"CASE {column} {branches} ELSE 99 END"

    @staticmethod
    def _rarity_order_case(column: str = "rarity") -> str:
        branches = " ".join(f"WHEN '{rarity}' THEN {order}" for order, rarity in enumerate(RARITY_KEYS, start=1))
        return f"CASE {column} {branches} ELSE 99 END"

    async def _migrate_schema(self) -> None:
        await self.schema._migrate_schema()

    async def _migrate_learning_and_drafts(self) -> None:
        await self.schema._migrate_learning_and_drafts()

    async def _ensure_schema(self) -> None:
        await self.schema._ensure_schema()

    async def _ensure_indexes(self) -> None:
        await self.schema._ensure_indexes()

    async def _load_locations_cache(self) -> None:
        return await self.locations._load_locations_cache()

    async def get_location_by_id(self, location_id: int) -> LocationRow | None:
        return await self.locations.get_location_by_id(location_id)

    async def get_locations(self) -> list[LocationRow]:
        return await self.locations.get_locations()

    async def register_user_if_not_exists(
        self, user_id: int, username: str | None = None, first_name: str | None = None, last_name: str | None = None
    ) -> None:
        return await self.users.register_user_if_not_exists(user_id, username, first_name, last_name)

    async def search(self, query: str, *, offset: int = 0, limit: int = 50) -> dict[str, list[SearchItem]]:
        return await self.search_repository.search(query, offset=offset, limit=limit)

    async def get_mob_full_card(self, mob_id: int) -> MobCardRow | None:
        return await self.mobs.get_mob_full_card(mob_id)

    async def get_mobs_by_location_sorted_by_hp(self, location_id: int, offset: int, limit: int) -> list[DbRow]:
        return await self.mobs.get_mobs_by_location_sorted_by_hp(location_id, offset, limit)

    async def get_prev_next_mob_by_hp(self, mob_id: int, location_id: int) -> NavigationIds:
        return await self.mobs.get_prev_next_mob_by_hp(mob_id, location_id)

    async def update_mob_field(self, mob_id: int, field: str, value: str | int) -> None:
        return await self.mobs.update_mob_field(mob_id, field, value)

    async def delete_mob(self, mob_id: int) -> None:
        return await self.mobs.delete_mob(mob_id)

    async def get_resource_card(self, resource_id: int) -> ResourceCardRow | None:
        return await self.resources.get_resource_card(resource_id)

    async def get_gear_by_id(self, gear_id: int) -> GearRow | None:
        return await self.gear.get_gear_by_id(gear_id)

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
        return await self.gear.add_gear(
            name, rarity, slot, emoji, level, classes, note, allow_duplicate=allow_duplicate
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
        return await self.gear.update_gear(gear_id, name, rarity, slot, emoji, level, classes, note)

    async def delete_gear(self, gear_id: int) -> None:
        return await self.gear.delete_gear(gear_id)

    async def get_gear_card(self, gear_id: int) -> GearCardRow | None:
        return await self.gear.get_gear_card(gear_id)

    async def get_prev_next_gear(
        self,
        gear_id: int,
        rarity: str,
        slot: str | None = None,
    ) -> NavigationIds:
        return await self.gear.get_prev_next_gear(gear_id, rarity, slot)

    async def get_all_gear_simple(self) -> list[DbRow]:
        return await self.gear.get_all_gear_simple()

    async def get_all_recipes(self, result_type: str, offset: int, limit: int) -> list[DbRow]:
        return await self.recipes.get_all_recipes(result_type, offset, limit)

    async def get_recipe_owners(self, recipe_id: int) -> list[str]:
        return await self.recipes.get_recipe_owners(recipe_id)

    async def get_recipe_learning_scroll(self, recipe_id: int) -> ResourceRow | None:
        return await self.recipes.get_recipe_learning_scroll(recipe_id)

    async def get_recipe_details(self, recipe_id: int) -> RecipeDetailsRow | None:
        return await self.recipes.get_recipe_details(recipe_id)

    async def create_recipe(self, result_type: str, result_id: int, quantity: int = 1) -> int:
        return await self.recipes.create_recipe(result_type, result_id, quantity)

    async def update_recipe_quantity(self, recipe_id: int, quantity: int) -> None:
        return await self.recipes.update_recipe_quantity(recipe_id, quantity)

    async def update_recipe_craft_location(self, recipe_id: int, craft_location: str) -> None:
        return await self.recipes.update_recipe_craft_location(recipe_id, craft_location)

    async def delete_recipe(self, recipe_id: int) -> None:
        return await self.recipes.delete_recipe(recipe_id)

    async def add_ingredient(self, recipe_id: int, resource_id: int, quantity: int) -> None:
        return await self.recipes.add_ingredient(recipe_id, resource_id, quantity)

    async def update_ingredient(self, recipe_id: int, resource_id: int, quantity: int) -> None:
        return await self.recipes.update_ingredient(recipe_id, resource_id, quantity)

    async def remove_ingredient(self, recipe_id: int, resource_id: int) -> None:
        return await self.recipes.remove_ingredient(recipe_id, resource_id)

    async def get_recipe_owner_entries(self, recipe_id: int) -> list[RecipeOwnerEntry]:
        return await self.recipes.get_recipe_owner_entries(recipe_id)

    async def add_recipe_owner(self, recipe_id: int, player_username: str) -> None:
        return await self.recipes.add_recipe_owner(recipe_id, player_username)

    async def remove_recipe_owner(self, recipe_id: int, player_username: str) -> None:
        return await self.recipes.remove_recipe_owner(recipe_id, player_username)

    async def remove_recipe_owner_entry(self, recipe_id: int, owner_id: int) -> None:
        return await self.recipes.remove_recipe_owner_entry(recipe_id, owner_id)

    async def claim_recipe_owner(
        self,
        recipe_id: int,
        user_id: int,
        username: str | None,
        *,
        expected_gear_id: int | None = None,
    ) -> None:
        return await self.recipes.claim_recipe_owner(recipe_id, user_id, username, expected_gear_id=expected_gear_id)

    async def relinquish_recipe_owner(self, recipe_id: int, user_id: int) -> None:
        return await self.recipes.relinquish_recipe_owner(recipe_id, user_id)

    async def get_recipe_for_resource(self, resource_id: int) -> ResourceRecipeRow | None:
        return await self.recipes.get_recipe_for_resource(resource_id)

    async def save_resource_recipe(
        self,
        result_id: int,
        quantity: int,
        materials: list[MaterialInput],
        *,
        craft_location: str = "",
        operation_id: str | None = None,
    ) -> int:
        return await self.recipes.save_resource_recipe(
            result_id, quantity, materials, craft_location=craft_location, operation_id=operation_id
        )

    async def _validate_resource_graph(self) -> None:
        await self.recipes._validate_resource_graph()

    async def set_recipe_learning_scroll(self, recipe_id: int, scroll_resource_id: int | None) -> None:
        return await self.recipes.set_recipe_learning_scroll(recipe_id, scroll_resource_id)

    async def delete_recipe_bundle(self, recipe_id: int, *, delete_scroll: bool = False) -> None:
        return await self.recipes.delete_recipe_bundle(recipe_id, delete_scroll=delete_scroll)

    async def resolve_gear_id(self, gear_id: int) -> int:
        return await self.gear.resolve_gear_id(gear_id)

    async def merge_gear(self, source_gear_id: int, target_gear_id: int) -> int:
        return await self.gear.merge_gear(source_gear_id, target_gear_id)

    async def get_gear_draft_payload(self, gear_id: int) -> GearDraftPayload | None:
        return await self.drafts.get_gear_draft_payload(gear_id)

    @staticmethod
    def _payload_fingerprint(payload: GearDraftPayload) -> str:
        return GearDraftRepository._payload_fingerprint(payload)

    async def _gear_owner_fingerprint(self, gear_id: int) -> str:
        return await self.drafts._gear_owner_fingerprint(gear_id)

    async def _scroll_reference(self, scroll_id: int) -> tuple[str, list[int]]:
        return await self.drafts._scroll_reference(scroll_id)

    async def _prepare_draft_references(self, payload: GearDraftPayload, previous: DbRow | None = None) -> str:
        return await self.drafts._prepare_draft_references(payload, previous)

    async def _check_draft_references(self, row: DbRow, payload: GearDraftPayload) -> None:
        return await self.drafts._check_draft_references(row, payload)

    @staticmethod
    def _decode_draft(row: DbRow) -> GearDraft:
        return GearDraftRepository._decode_draft(row)

    async def _context_draft(
        self, draft_id: str, owner_user_id: int, chat_id: int, message_id: int | None = None
    ) -> DbRow:
        return await self.drafts._context_draft(draft_id, owner_user_id, chat_id, message_id)

    @staticmethod
    def _check_draft_revision(row: DbRow, expected_revision: int) -> None:
        return GearDraftRepository._check_draft_revision(row, expected_revision)

    async def create_gear_draft(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        payload: GearDraftPayload | None = None,
        gear_id: int | None = None,
    ) -> GearDraft:
        return await self.drafts.create_gear_draft(
            owner_user_id=owner_user_id, chat_id=chat_id, message_id=message_id, payload=payload, gear_id=gear_id
        )

    async def get_gear_draft(self, draft_id: str, *, owner_user_id: int, chat_id: int) -> GearDraft | None:
        return await self.drafts.get_gear_draft(draft_id, owner_user_id=owner_user_id, chat_id=chat_id)

    async def list_gear_drafts(self, *, owner_user_id: int, chat_id: int) -> list[GearDraft]:
        return await self.drafts.list_gear_drafts(owner_user_id=owner_user_id, chat_id=chat_id)

    async def update_gear_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        payload: GearDraftPayload,
    ) -> GearDraft:
        return await self.drafts.update_gear_draft(
            draft_id,
            expected_revision=expected_revision,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            message_id=message_id,
            payload=payload,
        )

    async def bind_gear_draft_message(
        self,
        draft_id: str,
        *,
        old_message_id: int,
        new_message_id: int,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
    ) -> GearDraft:
        return await self.drafts.bind_gear_draft_message(
            draft_id,
            old_message_id=old_message_id,
            new_message_id=new_message_id,
            expected_revision=expected_revision,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
        )

    async def cancel_gear_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> GearDraft:
        return await self.drafts.cancel_gear_draft(
            draft_id,
            expected_revision=expected_revision,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def delete_gear_draft_target(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        delete_gear: bool,
        delete_scroll: bool = False,
    ) -> GearDraft:
        return await self.drafts.delete_gear_draft_target(
            draft_id,
            expected_revision=expected_revision,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            message_id=message_id,
            delete_gear=delete_gear,
            delete_scroll=delete_scroll,
        )

    async def _create_named_draft_resource(
        self, name: str, emoji: str, resource_type: str, note: str = "", *, allow_duplicate: bool = False
    ) -> int:
        return await self.drafts._create_named_draft_resource(
            name, emoji, resource_type, note, allow_duplicate=allow_duplicate
        )

    async def _replace_draft_drops(self, item_type: DropItemType, item_id: int, mob_ids: list[int]) -> None:
        return await self.drafts._replace_draft_drops(item_type, item_id, mob_ids)

    async def save_gear_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> GearSaveResult:
        return await self.drafts.save_gear_draft(
            draft_id,
            expected_revision=expected_revision,
            owner_user_id=owner_user_id,
            chat_id=chat_id,
            message_id=message_id,
        )

    async def get_cards_page(self, offset: int, limit: int) -> list[DbRow]:
        return await self.cards.get_cards_page(offset, limit)

    async def get_all_cards_sorted_by_slot(self, offset: int, limit: int) -> list[DbRow]:
        return await self.cards.get_all_cards_sorted_by_slot(offset, limit)

    async def get_card_by_id(self, card_id: int) -> CardRow | None:
        return await self.cards.get_card_by_id(card_id)

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
        return await self.cards.add_card(
            name, emoji, slot, bonus1, bonus2, bonus3, bonus4, note, allow_duplicate=allow_duplicate
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
        return await self.cards.create_card_with_sources(
            name,
            emoji,
            slot,
            bonus1,
            bonus2,
            bonus3,
            bonus4,
            note,
            mob_ids=mob_ids,
            allow_duplicate=allow_duplicate,
            operation_id=operation_id,
        )

    async def update_card(self, card_id: int, **kwargs: str) -> None:
        return await self.cards.update_card(card_id, **kwargs)

    async def delete_card(self, card_id: int) -> None:
        return await self.cards.delete_card(card_id)

    async def get_card_drop_mobs(self, card_id: int) -> list[ResourceDropMobRow]:
        return await self.cards.get_card_drop_mobs(card_id)

    async def get_prev_next_card_by_slot(self, card_id: int) -> NavigationIds:
        return await self.cards.get_prev_next_card_by_slot(card_id)

    @staticmethod
    def _validated_drop_mob_ids(mob_ids: object) -> list[int]:
        return DropRepository._validated_drop_mob_ids(mob_ids)

    async def get_drop_source_mobs(
        self,
        query: str = "",
        offset: int = 0,
        limit: int = 9,
        *,
        mob_ids: list[int] | None = None,
    ) -> list[MobSourceRow]:
        return await self.drops.get_drop_source_mobs(query, offset, limit, mob_ids=mob_ids)

    async def _resolve_drop_item(self, item_type: DropItemType, item_id: int) -> int:
        return await self.drops._resolve_drop_item(item_type, item_id)

    async def get_item_drop_mob_ids(self, item_type: DropItemType, item_id: int) -> list[int]:
        return await self.drops.get_item_drop_mob_ids(item_type, item_id)

    async def set_item_drop_sources(
        self,
        item_type: DropItemType,
        item_id: int,
        mob_ids: list[int],
        *,
        expected_mob_ids: list[int] | None = None,
    ) -> None:
        return await self.drops.set_item_drop_sources(item_type, item_id, mob_ids, expected_mob_ids=expected_mob_ids)

    async def search_drop_items(self, mob_id: int, query: str, limit: int = 20) -> list[DbRow]:
        return await self.drops.search_drop_items(mob_id, query, limit)

    async def get_enabled_drop_ids(
        self,
        mob_id: int,
        item_type: str,
        item_ids: list[int],
    ) -> set[int]:
        return await self.drops.get_enabled_drop_ids(mob_id, item_type, item_ids)

    async def get_drop_status(self, mob_id: int, item_type: str, item_id: int) -> bool:
        return await self.drops.get_drop_status(mob_id, item_type, item_id)

    async def add_drop(self, mob_id: int, item_type: str, item_id: int) -> None:
        return await self.drops.add_drop(mob_id, item_type, item_id)

    async def remove_drop(self, mob_id: int, item_type: str, item_id: int) -> None:
        return await self.drops.remove_drop(mob_id, item_type, item_id)

    async def get_resources_page(self, offset: int, limit: int) -> list[DbRow]:
        return await self.resources.get_resources_page(offset, limit)

    async def _migrate_catalog_metadata(self) -> None:
        await self.schema._migrate_catalog_metadata()

    async def _install_catalog_audit(self) -> None:
        await self.schema._install_catalog_audit()

    async def _check_published_draft_references(self, row: DbRow, current: GearDraftPayload) -> None:
        return await self.drafts._check_published_draft_references(row, current)

    async def get_catalog_revision(self) -> int:
        return await self.commands.get_catalog_revision()

    async def _catalog_command_result(
        self, operation_id: str | None, result_type: str, request_json: str
    ) -> int | None:
        return await self.commands._catalog_command_result(operation_id, result_type, request_json)

    async def _record_catalog_command(
        self, operation_id: str | None, result_type: str, request_json: str, result_id: int
    ) -> None:
        return await self.commands._record_catalog_command(operation_id, result_type, request_json, result_id)

    async def get_location_children(self, parent_id: int | None) -> list[LocationRow]:
        return await self.locations.get_location_children(parent_id)

    async def get_location_parent(self, location_id: int) -> LocationRow | None:
        return await self.locations.get_location_parent(location_id)

    async def get_resource_name_matches(self, name: str, resource_type: str | None = None) -> list[ResourceRow]:
        return await self.resources.get_resource_name_matches(name, resource_type)

    async def get_resource_by_code(self, code: str) -> ResourceRow | None:
        return await self.resources.get_resource_by_code(code)

    async def get_recipe_resource_choices(
        self, kind: Literal["material", "alchemy_result", "scroll"], *, exclude_result_id: int | None = None
    ) -> list[ResourceRow]:
        return await self.recipes.get_recipe_resource_choices(kind, exclude_result_id=exclude_result_id)

    async def get_card_name_matches(self, name: str, slot: str | None = None) -> list[CardRow]:
        return await self.cards.get_card_name_matches(name, slot)

    async def get_cards_by_slot(self, slot: str, offset: int = 0, limit: int = 100) -> list[CardRow]:
        return await self.cards.get_cards_by_slot(slot, offset, limit)

    async def set_drop_enabled(self, mob_id: int, item_type: DropItemType, item_id: int, enabled: bool) -> None:
        return await self.drops.set_drop_enabled(mob_id, item_type, item_id, enabled)

    async def get_mob_by_id(self, mob_id: int) -> MobRow | None:
        return await self.mobs.get_mob_by_id(mob_id)

    async def get_mobs_page(self, location_id: int, offset: int = 0, limit: int = 11) -> list[MobRow]:
        return await self.mobs.get_mobs_page(location_id, offset, limit)

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
        return await self.mobs.add_mob(name, emoji, hp, dust_min, dust_max, exp, location_id, operation_id=operation_id)

    async def get_resources_by_location(self, location_id: int, offset: int, limit: int) -> list[DbRow]:
        return await self.resources.get_resources_by_location(location_id, offset, limit)

    async def get_resource_by_id(self, resource_id: int) -> ResourceRow | None:
        return await self.resources.get_resource_by_id(resource_id)

    async def add_resource(
        self, name: str, emoji: str, resource_type: str = "craft", note: str = "", *, allow_duplicate: bool = False
    ) -> int:
        return await self.resources.add_resource(name, emoji, resource_type, note, allow_duplicate=allow_duplicate)

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
        return await self.resources.create_resource_with_sources(
            name,
            emoji,
            resource_type,
            note,
            mob_ids=mob_ids,
            allow_duplicate=allow_duplicate,
            operation_id=operation_id,
        )

    async def update_resource(
        self,
        resource_id: int,
        name: str | None = None,
        emoji: str | None = None,
        resource_type: str | None = None,
        note: str | None = None,
    ) -> None:
        return await self.resources.update_resource(resource_id, name, emoji, resource_type, note)

    async def get_resource_dependencies(self, resource_id: int) -> ResourceDependencies:
        return await self.resources.get_resource_dependencies(resource_id)

    async def delete_resource(self, resource_id: int) -> None:
        return await self.resources.delete_resource(resource_id)

    async def get_resources_by_type(self, resource_type: str, offset: int, limit: int) -> list[DbRow]:
        return await self.resources.get_resources_by_type(resource_type, offset, limit)

    async def get_all_resources_simple(self) -> list[DbRow]:
        return await self.resources.get_all_resources_simple()

    async def get_prev_next_resource_by_type(self, resource_id: int, resource_type: str) -> NavigationIds:
        return await self.resources.get_prev_next_resource_by_type(resource_id, resource_type)

    async def get_prev_next_resource_by_location(
        self,
        resource_id: int,
        location_id: int,
    ) -> NavigationIds:
        return await self.resources.get_prev_next_resource_by_location(resource_id, location_id)

    async def _delete_recipes_by_result(self, result_type: str, result_id: int) -> None:
        return await self.recipes._delete_recipes_by_result(result_type, result_id)

    async def get_all_gear(self, offset: int, limit: int) -> list[DbRow]:
        return await self.gear.get_all_gear(offset, limit)

    async def get_gear_by_slot(
        self,
        slot: str,
        offset: int,
        limit: int,
    ) -> list[DbRow]:
        return await self.gear.get_gear_by_slot(slot, offset, limit)

    async def get_gear_by_rarity_slot(
        self,
        rarity: str,
        slot: str,
        offset: int,
        limit: int,
    ) -> list[DbRow]:
        return await self.gear.get_gear_by_rarity_slot(rarity, slot, offset, limit)
