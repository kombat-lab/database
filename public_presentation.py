from __future__ import annotations
import asyncio
import logging
import re
from aiogram import types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from catalog_types import (
    RecipeOwnerEntry,
    ItemRow,
    MobCardRow,
    ResourceCardRow,
    GearCardRow,
    ResourceUsageRow,
)
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputRichMessage,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from database import Database
from catalog_reads import gear_slot_counts, integer as catalog_integer, text as catalog_text
from game_constants import (
    GEAR_SLOT_ICONS as SLOT_ICONS,
    GEAR_SLOT_LABELS as SLOT_NAMES,
    GEAR_SLOTS as GEAR_SLOT_ORDER,
    RARITY_KEYS as RARITY_ORDER,
    RARITY_NAMES,
    RARITY_EMOJIS,
    LEGACY_ALCHEMY_CRAFT_LOCATIONS,
    LEGACY_DEFAULT_ALCHEMY_CRAFT_LOCATION,
)
from utils import clean_username, escape_html
from lifecycle import BackgroundTaskRegistry
from messaging import cleanup_card_fragments, replace_rich_card, upsert_rich_card
from telegram_helpers import get_bound_bot, get_callback_message
from navigation import (
    build_resource_return_param as build_resource_return_param,
    build_gear_return_param as build_gear_return_param,
    build_recipe_owner_callback as build_recipe_owner_callback,
)
from ui.callbacks import (
    CardViewCallback,
    EntityBackCallback,
    GearViewCallback,
    RecipeOwnerCallback,
    MobViewCallback,
    ResourceLocationViewCallback,
    ResourceViewCallback,
)
from ui.cards import build_card_card, build_gear_card, build_mob_card, build_resource_card
from ui.links import EntityLinkMode
from ui.navigation import EntityNavigationHistory, EntityRef
from ui.rich import CardView, present_rich_card

from dataclasses import dataclass, field
from analytics import AnalyticsService
from inline_search import InlineSearchService, LatestInlineQueries

ITEMS_PER_PAGE = 10

FETCH_EXTRA = 1

MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "🔍 Поиск"}

MAX_SEARCH_QUERY_LENGTH = 256

RESOURCE_TYPE_NAMES = {
    "craft": "⚒️ Крафтовый",
    "consumable": "✨ Расходуемый",
    "scroll_recipe": "📜 Рецепт экипировки",
    "currency": "💰 Валюта",
    "alchemy": "⚗️ Алхимия",
}

RESOURCE_TYPE_TITLES = {
    "craft": "Крафтовые",
    "consumable": "Расходуемые",
    "scroll_recipe": "Рецепты экипировки",
    "currency": "Валюта",
    "alchemy": "Алхимия",
}

DEFAULT_ALCHEMY_CRAFT_LOCATION = LEGACY_DEFAULT_ALCHEMY_CRAFT_LOCATION

MEREDITH_ALCHEMY_CRAFT_LOCATION = LEGACY_ALCHEMY_CRAFT_LOCATIONS["дубленая кожа"]

MEREDITH_ALCHEMY_RESOURCES = frozenset(LEGACY_ALCHEMY_CRAFT_LOCATIONS)

LOCATION_CONTENT_TITLES = {
    "mobs": "Мобы",
    "resources": "Ресурсы",
}

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PublicContext:
    db: Database
    bot_username: str | None = None
    analytics: AnalyticsService | None = None
    background_tasks: BackgroundTaskRegistry = field(default_factory=BackgroundTaskRegistry)
    entity_navigation: EntityNavigationHistory = field(default_factory=EntityNavigationHistory)


class PublicPresentation:
    """Catalog views with explicit storage, analytics and navigation dependencies."""

    def __init__(self, context: PublicContext) -> None:
        self.db = context.db
        self.BOT_USERNAME = context.bot_username
        self.analytics = context.analytics or AnalyticsService(context.db)
        self.background_tasks = context.background_tasks
        self.entity_navigation = context.entity_navigation
        self.inline_log_tasks: dict[int, asyncio.Task[None]] = {}
        self.inline_search = InlineSearchService(context.db)
        self.latest_inline_queries = LatestInlineQueries()

    build_card_card = staticmethod(build_card_card)
    build_gear_card = staticmethod(build_gear_card)
    build_mob_card = staticmethod(build_mob_card)
    build_resource_card = staticmethod(build_resource_card)
    cleanup_card_fragments = staticmethod(cleanup_card_fragments)
    present_rich_card = staticmethod(present_rich_card)
    replace_rich_card = staticmethod(replace_rich_card)
    upsert_rich_card = staticmethod(upsert_rich_card)

    def get_rarity_emoji(self, rarity: str | None) -> str:
        return RARITY_EMOJIS.get(rarity or "common", RARITY_EMOJIS["common"])

    def get_resource_type_name(self, resource_type: str | None) -> str:
        return RESOURCE_TYPE_NAMES.get(resource_type or "craft", "📦 Крафтовый")

    def get_alchemy_craft_location(self, resource_name: str) -> str:
        if resource_name.strip().casefold() in MEREDITH_ALCHEMY_RESOURCES:
            return MEREDITH_ALCHEMY_CRAFT_LOCATION
        return DEFAULT_ALCHEMY_CRAFT_LOCATION

    def build_resource_usage_rows(
        self,
        usages: list[ResourceUsageRow],
        return_param: str | None,
    ) -> list[tuple[str, int]]:
        rows = []
        sorted_usages = sorted(
            usages,
            key=lambda usage: (
                str(usage.get("result_name") or "").casefold(),
                int(usage.get("result_id") or 0),
            ),
        )
        for usage in sorted_usages:
            result_type = usage.get("result_type")
            result_id = usage.get("result_id")
            if result_type not in {"gear", "resource"} or not result_id:
                continue
            link = self.make_deep_link(result_type, result_id, return_param)
            visual_parts = [self.get_rarity_emoji(usage.get("result_rarity"))] if result_type == "gear" else []
            visual_parts.append(escape_html(usage.get("result_emoji", "")))
            visual = " ".join(part for part in visual_parts if part)
            name_link = f"<a href='{link}'>{escape_html(usage.get('result_name', ''))}</a>"
            rows.append((f"{visual} {name_link}".strip(), int(usage.get("quantity", 1))))
        return rows

    def get_location_emoji(self, location: ItemRow) -> str:
        """Возвращает emoji локации с безопасным fallback на значение из БД."""
        return location.get("emoji") or "📍"

    def get_location_button_text(self, location: ItemRow) -> str:
        return f"{self.get_location_emoji(location)} {location['name']}"

    def get_location_list_title(self, location: ItemRow, category: str, page: int) -> str:
        category_title = LOCATION_CONTENT_TITLES.get(category, category)
        return f"{self.get_location_emoji(location)} {location['name']} - {category_title}\nСтраница {page}"

    def make_deep_link(self, item_type: str, item_id: int, return_param: str | None = None) -> str:
        """Формирует корректный Telegram start payload длиной до 64 символов."""
        payload = f"{item_type}_{item_id}"
        if return_param:
            candidate = f"{payload}-r-{return_param}"
            if len(candidate) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
                payload = candidate
        return f"https://t.me/{self.BOT_USERNAME}?start={payload}"

    def get_card_link_mode(self, chat: types.Chat) -> EntityLinkMode:
        return EntityLinkMode.CALLBACK if chat.type == ChatType.PRIVATE else EntityLinkMode.DEEP_LINK

    def get_navigation_key(self, callback: types.CallbackQuery) -> tuple[int, int, int]:
        message = get_callback_message(callback)
        return (
            callback.from_user.id,
            message.chat.id,
            message.message_id,
        )

    async def build_interactive_entity_card(self, entity: EntityRef) -> CardView:
        if entity.entity_type == "mob":
            return await self.build_mob_card(
                self.db, entity.entity_id, bot_username=self.BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK
            )
        if entity.entity_type == "resource":
            return await self.build_resource_card(
                self.db, entity.entity_id, bot_username=self.BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK
            )
        if entity.entity_type == "gear":
            return await self.build_gear_card(
                self.db, entity.entity_id, bot_username=self.BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK
            )
        if entity.entity_type == "card":
            return await self.build_card_card(
                self.db, entity.entity_id, bot_username=self.BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK
            )
        raise ValueError("Unsupported entity type")

    def build_interactive_navigation_keyboard(
        self,
        previous: EntityRef | None,
    ) -> InlineKeyboardMarkup:
        keyboard = []
        if previous:
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text="↩️ Назад",
                        callback_data=EntityBackCallback(
                            entity_type=previous.entity_type,
                            entity_id=previous.entity_id,
                        ).pack(),
                    )
                ]
            )
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🏠 В главное меню",
                    callback_data="back_to_main_menu",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def log_interactive_entity_view(self, user_id: int, entity: EntityRef) -> None:
        loggers = {
            "mob": self.analytics.log_view_mob,
            "resource": self.analytics.log_view_resource,
            "gear": self.analytics.log_view_gear,
            "card": self.analytics.log_view_card,
        }
        await loggers[entity.entity_type](user_id, entity.entity_id)

    async def present_interactive_entity(
        self,
        callback: types.CallbackQuery,
        entity: EntityRef,
        previous: EntityRef | None,
        root_markup: InlineKeyboardMarkup | None = None,
    ) -> types.Message:
        keyboard = (
            root_markup
            if previous is None and root_markup is not None
            else self.build_interactive_navigation_keyboard(previous)
        )
        if entity.entity_type == "gear":
            data = await self.db.get_gear_card(entity.entity_id)
            card_view = await self.build_gear_card(
                self.db,
                entity.entity_id,
                data=data,
                bot_username=self.BOT_USERNAME,
                link_mode=EntityLinkMode.CALLBACK,
            )
            if data:
                gear_keyboard = await self.build_gear_card_keyboard(data, callback.from_user.id, 1, None)
                owner_rows = [
                    row
                    for row in gear_keyboard.inline_keyboard
                    if any((button.callback_data or "").startswith("recipe_") for button in row)
                ]
                other_rows = [
                    row
                    for row in keyboard.inline_keyboard
                    if not any((button.callback_data or "").startswith("recipe_") for button in row)
                ]
                keyboard = InlineKeyboardMarkup(inline_keyboard=[*owner_rows, *other_rows])
        else:
            card_view = await self.build_interactive_entity_card(entity)
        sent = await self.present_rich_card(
            bot=get_bound_bot(callback),
            chat_id=get_callback_message(callback).chat.id,
            card=card_view,
            reply_markup=keyboard,
            current_message=get_callback_message(callback),
        )
        return sent

    async def get_gear_slots_keyboard(self, rarity: str) -> InlineKeyboardMarkup:
        count_by_slot = await gear_slot_counts(self.db, rarity)

        rows = []
        for index, slot in enumerate(GEAR_SLOT_ORDER):
            item_count = count_by_slot.get(slot, 0)
            if item_count <= 0:
                continue
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{SLOT_NAMES[slot]} ({item_count})",
                        callback_data=f"gear_slot_{rarity}_{index}",
                    )
                ]
            )

        if not rows:
            rows.append(
                [
                    InlineKeyboardButton(
                        text="В этой категории пока нет предметов",
                        callback_data="gear_empty_category",
                    )
                ]
            )

        rows.append([InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def format_mob_card_plain(
        self, mob_id: int, location_id: int | None = None, page: int = 1, *, data: MobCardRow | None = None
    ) -> str:
        return (
            await self.build_mob_card(self.db, mob_id, location_id, page, data=data, bot_username=self.BOT_USERNAME)
        ).fallback_html

    async def format_mob_card(
        self, mob_id: int, location_id: int | None = None, page: int = 1, *, data: MobCardRow | None = None
    ) -> InputRichMessage:
        return (
            await self.build_mob_card(self.db, mob_id, location_id, page, data=data, bot_username=self.BOT_USERNAME)
        ).rich_message

    async def format_resource_card(
        self,
        resource_id: int,
        context_type: str | None = None,
        context_id: int | str | None = None,
        page: int = 1,
        *,
        data: ResourceCardRow | None = None,
    ) -> str:
        return (
            await self.build_resource_card(
                self.db, resource_id, context_type, context_id, page, data=data, bot_username=self.BOT_USERNAME
            )
        ).fallback_html

    async def format_resource_card_rich(
        self,
        resource_id: int,
        context_type: str | None = None,
        context_id: int | str | None = None,
        page: int = 1,
        *,
        data: ResourceCardRow | None = None,
    ) -> InputRichMessage:
        return (
            await self.build_resource_card(
                self.db, resource_id, context_type, context_id, page, data=data, bot_username=self.BOT_USERNAME
            )
        ).rich_message

    def format_recipe_owner(self, owner: RecipeOwnerEntry) -> str:
        username = owner["player_username"]
        if username:
            return f"@{escape_html(clean_username(username))}"
        user_id = owner["user_id"]
        if user_id is not None:
            return f"<a href='tg://user?id={user_id}'>Игрок {user_id}</a>"
        return "Неизвестный владелец"

    def recipe_owner_labels(self, data: GearCardRow) -> list[str]:
        if "owner_entries" in data:
            return [self.format_recipe_owner(owner) for owner in data["owner_entries"]]
        return [f"@{escape_html(clean_username(username))}" for username in data.get("owners", [])]

    async def format_gear_card_plain(
        self,
        gear_id: int,
        rarity: str | None = None,
        page: int = 1,
        *,
        data: GearCardRow | None = None,
        slot_index: int | None = None,
    ) -> str:
        return (
            await self.build_gear_card(
                self.db, gear_id, rarity, page, data=data, slot_index=slot_index, bot_username=self.BOT_USERNAME
            )
        ).fallback_html

    async def format_gear_card_rich(
        self,
        gear_id: int,
        rarity: str | None = None,
        page: int = 1,
        *,
        data: GearCardRow | None = None,
        slot_index: int | None = None,
    ) -> InputRichMessage:
        return (
            await self.build_gear_card(
                self.db, gear_id, rarity, page, data=data, slot_index=slot_index, bot_username=self.BOT_USERNAME
            )
        ).rich_message

    async def format_card_card(
        self, card_id: int, page: int = 1, context_type: str | None = None, context_id: int | None = None
    ) -> str:
        return (
            await self.build_card_card(self.db, card_id, page, context_type, context_id, bot_username=self.BOT_USERNAME)
        ).fallback_html

    async def format_card_card_rich(
        self, card_id: int, page: int = 1, context_type: str | None = None, context_id: int | None = None
    ) -> InputRichMessage:
        return (
            await self.build_card_card(self.db, card_id, page, context_type, context_id, bot_username=self.BOT_USERNAME)
        ).rich_message

    def get_main_menu_reply_keyboard(
        self,
    ) -> ReplyKeyboardMarkup:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🐾 Мобы"), KeyboardButton(text="📦 Ресурсы")],
                [KeyboardButton(text="⚔️ Снаряжение"), KeyboardButton(text="🔍 Поиск")],
            ],
            resize_keyboard=True,
        )

    def get_main_menu_inline_keyboard(
        self,
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🐾 Мобы", callback_data="main_section_mobs"),
                    InlineKeyboardButton(text="📦 Ресурсы", callback_data="main_section_resources"),
                ],
                [
                    InlineKeyboardButton(text="⚔️ Снаряжение", callback_data="main_section_gear"),
                    InlineKeyboardButton(text="🔍 Поиск", callback_data="main_section_search"),
                ],
            ]
        )

    async def get_locations_keyboard(self, category: str) -> InlineKeyboardMarkup:
        locations = await self.db.get_locations()
        keyboard = []
        grouped_ids = {loc["parent_id"] for loc in locations if loc.get("parent_id") is not None}

        for loc in locations:
            location_id = loc["id"]

            # Nested mob locations are reached through their parent group.
            if category == "mobs" and loc.get("parent_id") is not None:
                continue

            callback_data = (
                f"mobs_location_group_{location_id}"
                if category == "mobs" and location_id in grouped_ids
                else f"list_{category}_{location_id}_1"
            )
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=self.get_location_button_text(loc),
                        callback_data=callback_data,
                    )
                ]
            )

        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def get_location_group_keyboard(self, parent_id: int) -> InlineKeyboardMarkup:
        """Show the selected location and its immediate child groups."""
        locations = await self.db.get_locations()
        locations_by_id = {loc["id"]: loc for loc in locations}
        grouped_ids = {loc["parent_id"] for loc in locations if loc.get("parent_id") is not None}
        selected = locations_by_id.get(parent_id)
        ancestor_id = selected["parent_id"] if selected is not None else None
        keyboard = []

        for location_id in [parent_id, *(loc["id"] for loc in locations if loc.get("parent_id") == parent_id)]:
            location = locations_by_id.get(location_id)
            if not location:
                logger.warning("Локация id=%s отсутствует в списке locations", location_id)
                continue

            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=self.get_location_button_text(location),
                        callback_data=(
                            f"mobs_location_group_{location_id}"
                            if location_id != parent_id and location_id in grouped_ids
                            else f"list_mobs_{location_id}_1"
                        ),
                    )
                ]
            )

        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🔙 Назад к локациям",
                    callback_data=f"mobs_location_group_{ancestor_id}"
                    if ancestor_id is not None
                    else "back_to_locations_mobs",
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    def get_rarities_keyboard(
        self,
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"{self.get_rarity_emoji(rarity)} {RARITY_NAMES[rarity]}",
                        callback_data=f"gear_slots_{rarity}",
                    )
                ]
                for rarity in RARITY_ORDER
            ]
        )

    def get_inline_search_button(
        self,
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"🔍 Искать через @{self.BOT_USERNAME}" if self.BOT_USERNAME else "🔍 Искать по каталогу",
                        switch_inline_query_current_chat="",
                    )
                ]
            ]
        )

    async def get_items_keyboard(self, category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
        offset = (page - 1) * ITEMS_PER_PAGE
        if category == "mobs":
            items = await self.db.get_mobs_by_location_sorted_by_hp(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
        else:
            items = await self.db.get_resources_by_location(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
        has_next = len(items) > ITEMS_PER_PAGE
        items = items[:ITEMS_PER_PAGE]
        keyboard = []
        for item in items:
            name = f"{catalog_text(item, 'emoji')} {catalog_text(item, 'name')}"
            callback_data = (
                MobViewCallback(catalog_integer(item, "id"), location_id, page).pack()
                if category == "mobs"
                else ResourceLocationViewCallback(catalog_integer(item, "id"), location_id, page).pack()
            )
            keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_{category}_{location_id}_{page - 1}"))
        if has_next:
            nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_{category}_{location_id}_{page + 1}"))
        if nav:
            keyboard.append(nav)
        parent = await self.db.get_location_parent(location_id) if category == "mobs" else None
        children = await self.db.get_location_children(location_id) if category == "mobs" else []
        if category == "mobs" and (parent is not None or children):
            group_id = location_id if children else parent["id"] if parent is not None else location_id
            back_text = "🔙 Назад к группе локаций"
            back_callback = f"mobs_location_group_{group_id}"
        else:
            back_text = "🔙 Назад к локациям"
            back_callback = f"back_to_locations_{category}"

        keyboard.append([InlineKeyboardButton(text=back_text, callback_data=back_callback)])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def get_gear_by_slot_keyboard(self, rarity: str, slot_index: int, page: int) -> InlineKeyboardMarkup:
        slot = GEAR_SLOT_ORDER[slot_index]
        offset = (page - 1) * ITEMS_PER_PAGE
        items = await self.db.get_gear_by_rarity_slot(
            rarity,
            slot,
            offset,
            ITEMS_PER_PAGE + FETCH_EXTRA,
        )
        has_next = len(items) > ITEMS_PER_PAGE
        items = items[:ITEMS_PER_PAGE]
        keyboard = []
        for item in items:
            name = f"{catalog_text(item, 'emoji')} {catalog_text(item, 'name')}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=name,
                        callback_data=GearViewCallback(catalog_integer(item, "id"), rarity, slot_index, page).pack(),
                    )
                ]
            )
        nav = []
        if page > 1:
            nav.append(
                InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_gear_{rarity}_{slot_index}_{page - 1}")
            )
        if has_next:
            nav.append(
                InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_gear_{rarity}_{slot_index}_{page + 1}")
            )
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton(text="🔙 Назад к слотам", callback_data=f"gear_slots_{rarity}")])
        keyboard.append([InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")])
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def show_cards_list(self, target: types.Message | types.CallbackQuery, page: int) -> None:
        offset = (page - 1) * ITEMS_PER_PAGE
        cards = await self.db.get_all_cards_sorted_by_slot(offset, ITEMS_PER_PAGE + FETCH_EXTRA)
        has_next = len(cards) > ITEMS_PER_PAGE
        cards = cards[:ITEMS_PER_PAGE]
        keyboard = []
        for card in cards:
            slot_icon = SLOT_ICONS.get(catalog_text(card, "slot"), "❓")
            text = f"🃏{catalog_text(card, 'emoji')} {catalog_text(card, 'name')} {slot_icon}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=text,
                        callback_data=CardViewCallback(catalog_integer(card, "id"), page).pack(),
                    )
                ]
            )

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"cards_page_{page - 1}"))
        if has_next:
            nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"cards_page_{page + 1}"))
        if nav:
            keyboard.append(nav)

        keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_resource_cats")])

        if isinstance(target, types.Message):
            await target.answer("🃏 Список карт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        else:
            await self.replace_callback_message_text(
                target, "🃏 Список карт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )

    def get_resource_categories_keyboard(
        self,
    ) -> InlineKeyboardMarkup:
        keyboard = [
            [InlineKeyboardButton(text="📦 Крафтовые", callback_data="resource_cat_craft")],
            [InlineKeyboardButton(text="✨ Расходуемые", callback_data="resource_cat_consumable")],
            [InlineKeyboardButton(text="📜 Рецепты экипировки", callback_data="resource_cat_scroll_recipe")],
            [InlineKeyboardButton(text="💰 Валюта", callback_data="resource_cat_currency")],
            [InlineKeyboardButton(text="⚗️ Алхимия", callback_data="resource_cat_alchemy")],
            [InlineKeyboardButton(text="🃏 Карты", callback_data="resource_cat_cards")],
        ]
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def show_resources_by_type(
        self, target: types.Message | types.CallbackQuery, resource_type: str, page: int
    ) -> None:
        offset = (page - 1) * ITEMS_PER_PAGE
        items = await self.db.get_resources_by_type(resource_type, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
        has_next = len(items) > ITEMS_PER_PAGE
        items = items[:ITEMS_PER_PAGE]

        type_display = RESOURCE_TYPE_TITLES.get(resource_type, resource_type)

        keyboard = []
        for res in items:
            text = f"{catalog_text(res, 'emoji')} {catalog_text(res, 'name')}"
            callback_data = ResourceViewCallback(catalog_integer(res, "id"), resource_type, page).pack()
            keyboard.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"res_page_{resource_type}_{page - 1}"))
        if has_next:
            nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"res_page_{resource_type}_{page + 1}"))
        if nav:
            keyboard.append(nav)

        keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_resource_cats")])

        if isinstance(target, types.Message):
            await target.answer(
                f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
        else:
            await self.replace_callback_message_text(
                target, f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )

    async def delayed_log_inline_search(self, user_id: int, query: str, delay: float = 0.8) -> None:
        try:
            await asyncio.sleep(delay)
            if query.strip():
                await self.analytics.log_inline_search(user_id, query)
        finally:
            if self.inline_log_tasks.get(user_id) is asyncio.current_task():
                self.inline_log_tasks.pop(user_id, None)

    async def edit_callback_window(
        self,
        callback: types.CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None:
        """Показывает следующий экран независимо от типа исходного сообщения.

        Telegram не позволяет вызывать edit_text() для сообщения с фотографией.
        Поэтому пост с картой удаляется и заменяется обычным текстовым сообщением.
        Обычные текстовые сообщения по-прежнему редактируются на месте.
        """
        message = get_callback_message(callback)
        if message.photo:
            await message.answer(
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            await self.cleanup_card_fragments(get_bound_bot(callback), message.chat.id, message.message_id)
            try:
                await message.delete()
            except TelegramAPIError:
                logger.debug("Could not delete world-map message", exc_info=True)
            return

        try:
            await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except TelegramBadRequest as error:
            if "message is not modified" not in error.message.lower():
                raise
        await self.cleanup_card_fragments(get_bound_bot(callback), message.chat.id, message.message_id)

    async def replace_callback_message_text(
        self,
        callback: types.CallbackQuery,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> None:
        await self.edit_callback_window(callback, text, reply_markup, parse_mode)
        self.entity_navigation.clear(self.get_navigation_key(callback))

    async def build_gear_card_keyboard(
        self,
        data: GearCardRow,
        user_id: int,
        page: int,
        slot_index: int | None,
        *,
        personal: bool = True,
    ) -> InlineKeyboardMarkup:
        gear_id = data["id"]
        rarity = data["rarity"]

        if slot_index is not None:
            try:
                slot_index = GEAR_SLOT_ORDER.index(data["slot"])
            except ValueError:
                slot_index = None

        slot = GEAR_SLOT_ORDER[slot_index] if slot_index is not None else None
        neighbours = await self.db.get_prev_next_gear(gear_id, rarity, slot)
        nav_buttons = []
        for neighbour_id, text_label in (
            (neighbours["prev_id"], "◀️ Предыдущий"),
            (neighbours["next_id"], "Следующий ▶️"),
        ):
            if not neighbour_id:
                continue
            if slot_index is None:
                callback_data = GearViewCallback(neighbour_id, rarity, None, page).pack(navigation=True)
            else:
                callback_data = GearViewCallback(neighbour_id, rarity, slot_index, page).pack(navigation=True)
            nav_buttons.append(
                InlineKeyboardButton(
                    text=text_label,
                    callback_data=callback_data,
                )
            )

        keyboard = [nav_buttons] if nav_buttons else []
        recipe_id = data.get("recipe_id")
        if data.get("can_learn", False) and recipe_id:
            if personal:
                is_owner = user_id in data.get("owner_user_ids", [])
                actions = [("relinquish", "❌ Я не изучал рецепт")] if is_owner else [("claim", "✅ Я изучил рецепт")]
            else:
                # Shared group messages must not represent another reader's state.
                actions = [("claim", "✅ Я изучил рецепт"), ("relinquish", "❌ Снять мою отметку")]
            keyboard.append(
                [
                    InlineKeyboardButton(
                        text=label,
                        callback_data=RecipeOwnerCallback(
                            action=action,
                            recipe_id=recipe_id,
                            gear_id=gear_id,
                            rarity=rarity,
                            page=page,
                            slot_index=slot_index,
                        ).pack(),
                    )
                    for action, label in actions
                ]
            )

        back_callback = f"page_gear_{rarity}_{slot_index}_{page}" if slot_index is not None else "gear_rarities"
        keyboard.append(
            [
                InlineKeyboardButton(
                    text="🔙 Назад к списку",
                    callback_data=back_callback,
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

    async def render_gear_card(
        self,
        callback: types.CallbackQuery,
        gear_id: int,
        rarity: str,
        page: int,
        slot_index: int | None = None,
        *,
        replace: bool = False,
    ) -> bool:
        data = await self.db.get_gear_card(gear_id)
        if not data:
            await self.edit_callback_window(callback, "Предмет не найден.")
            return False

        # Данные карточки являются источником истины: старые кнопки могут содержать
        # редкость, которая уже изменилась в админке.
        rarity = data["rarity"]
        if slot_index is not None:
            try:
                slot_index = GEAR_SLOT_ORDER.index(data["slot"])
            except ValueError:
                slot_index = None
        card_view = await self.build_gear_card(
            self.db,
            data["id"],
            rarity,
            page,
            data=data,
            slot_index=slot_index,
            bot_username=self.BOT_USERNAME,
            link_mode=self.get_card_link_mode(get_callback_message(callback).chat),
        )
        reply_markup = await self.build_gear_card_keyboard(
            data,
            callback.from_user.id,
            page,
            slot_index,
            personal=get_callback_message(callback).chat.type == ChatType.PRIVATE,
        )
        render_card = self.replace_rich_card if replace else self.upsert_rich_card
        await render_card(
            bot=get_bound_bot(callback),
            chat_id=get_callback_message(callback).chat.id,
            rich_message=card_view.rich_message,
            plain_text=card_view.fallback_html,
            reply_markup=reply_markup,
            current_message=get_callback_message(callback),
        )
        return True
