from __future__ import annotations

import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import SimpleEventIsolation
from catalog_types import (
    RecipeOwnerEntry, ItemRow, MobCardRow, ResourceCardRow, GearCardRow, ResourceUsageRow,
)
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InlineQueryResultUnion,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageContent,
    InputRichMessageMedia,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from admin_handlers import admin_router
from analytics import (
    AnalyticsMiddleware,
    log_inline_search,
    log_search,
    log_start,
    log_view_card,
    log_view_gear,
    log_view_mob,
    log_view_resource,
)
from database import db
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
from lifecycle import BackgroundTaskRegistry, UpdateTaskTracker, install_update_tracker
from messaging import cleanup_card_fragments, replace_rich_card, upsert_rich_card
from routing import CallbackMessageGuard
from telegram_helpers import get_bound_bot, get_callback_data, get_callback_message
from telegram_text import split_formatted_text
from search_rendering import build_search_content, ranked_inline_items
from navigation import (
    MAX_SQLITE_ID,
    build_resource_return_param as build_resource_return_param,
    build_gear_return_param as build_gear_return_param,
    build_recipe_owner_callback as build_recipe_owner_callback,
    parse_gear_view_callback, parse_recipe_owner_callback,
    parse_return_param, parse_resource_page_callback, parse_resource_view_callback,
    parse_location_callback, parse_gear_list_callback, parse_card_callback, return_button_data,
)
from ui.callbacks import (
    CardViewCallback, EntityBackCallback, EntityNavigateCallback,
    GearViewCallback, RecipeOwnerCallback, MobViewCallback,
    ResourceLocationViewCallback, ResourceViewCallback,
)
from ui.cards import build_card_card, build_gear_card, build_mob_card, build_resource_card
from ui.links import EntityLinkMode
from ui.navigation import EntityNavigationHistory, EntityRef
from ui.rich import CardView, present_rich_card

ITEMS_PER_PAGE = 10
FETCH_EXTRA = 1
MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "🔍 Поиск"}
BOT_USERNAME: str | None = None
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

def get_rarity_emoji(rarity: str | None) -> str:
    return RARITY_EMOJIS.get(rarity or "common", RARITY_EMOJIS["common"])


def get_resource_type_name(resource_type: str | None) -> str:
    return RESOURCE_TYPE_NAMES.get(resource_type or "craft", "📦 Крафтовый")


def get_alchemy_craft_location(resource_name: str) -> str:
    if resource_name.strip().casefold() in MEREDITH_ALCHEMY_RESOURCES:
        return MEREDITH_ALCHEMY_CRAFT_LOCATION
    return DEFAULT_ALCHEMY_CRAFT_LOCATION


def build_resource_usage_rows(
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
        link = make_deep_link(result_type, result_id, return_param)
        visual_parts = (
            [get_rarity_emoji(usage.get("result_rarity"))]
            if result_type == "gear"
            else []
        )
        visual_parts.append(escape_html(usage.get("result_emoji", "")))
        visual = " ".join(part for part in visual_parts if part)
        name_link = f"<a href='{link}'>{escape_html(usage.get('result_name', ''))}</a>"
        rows.append(
            (f"{visual} {name_link}".strip(), int(usage.get("quantity", 1)))
        )
    return rows


# Группа вложенных локаций во вкладке «Мобы».
DEAD_FOREST_LOCATION_ID = 4
DEAD_FOREST_CHILD_LOCATION_IDS = (8, 9, 10)
DEAD_FOREST_GROUP_LOCATION_IDS = (
    DEAD_FOREST_LOCATION_ID,
    *DEAD_FOREST_CHILD_LOCATION_IDS,
)

# Значения используются в интерфейсе даже до обновления emoji в БД.
LOCATION_EMOJI_OVERRIDES = {
    8: "🪨",   # Пещера
    9: "⛏️",  # Подземная пещера
    10: "🦇",  # Темный грот
}
LOCATION_CONTENT_TITLES = {
    "mobs": "Мобы",
    "resources": "Ресурсы",
}


def get_location_emoji(location: ItemRow) -> str:
    """Возвращает emoji локации с безопасным fallback на значение из БД."""
    return LOCATION_EMOJI_OVERRIDES.get(location["id"], location.get("emoji") or "📍")


def get_location_button_text(location: ItemRow) -> str:
    return f"{get_location_emoji(location)} {location['name']}"


def get_location_list_title(location: ItemRow, category: str, page: int) -> str:
    category_title = LOCATION_CONTENT_TITLES.get(category, category)
    return f"{get_location_emoji(location)} {location['name']} - {category_title}\nСтраница {page}"


def make_deep_link(item_type: str, item_id: int, return_param: str | None = None) -> str:
    """Формирует корректный Telegram start payload длиной до 64 символов."""
    payload = f"{item_type}_{item_id}"
    if return_param:
        candidate = f"{payload}-r-{return_param}"
        if len(candidate) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            payload = candidate
    return f"https://t.me/{BOT_USERNAME}?start={payload}"

logger = logging.getLogger(__name__)
inline_log_tasks: dict[int, asyncio.Task[None]] = {}
background_tasks = BackgroundTaskRegistry()
update_tasks = UpdateTaskTracker()
entity_navigation = EntityNavigationHistory()
dp = Dispatcher(events_isolation=SimpleEventIsolation())
install_update_tracker(dp, update_tasks)
dp.callback_query.outer_middleware(CallbackMessageGuard())


def get_card_link_mode(chat: types.Chat) -> EntityLinkMode:
    return (
        EntityLinkMode.CALLBACK
        if chat.type == ChatType.PRIVATE
        else EntityLinkMode.DEEP_LINK
    )


def get_navigation_key(callback: types.CallbackQuery) -> tuple[int, int, int]:
    message = get_callback_message(callback)
    return (
        callback.from_user.id,
        message.chat.id,
        message.message_id,
    )


async def build_interactive_entity_card(entity: EntityRef) -> CardView:
    if entity.entity_type == "mob":
        return await build_mob_card(db, entity.entity_id, bot_username=BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK)
    if entity.entity_type == "resource":
        return await build_resource_card(db, entity.entity_id, bot_username=BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK)
    if entity.entity_type == "gear":
        return await build_gear_card(db, entity.entity_id, bot_username=BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK)
    if entity.entity_type == "card":
        return await build_card_card(db, entity.entity_id, bot_username=BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK)
    raise ValueError("Unsupported entity type")


def build_interactive_navigation_keyboard(
    previous: EntityRef | None,
) -> InlineKeyboardMarkup:
    keyboard = []
    if previous:
        keyboard.append([InlineKeyboardButton(
            text="↩️ Назад",
            callback_data=EntityBackCallback(
                entity_type=previous.entity_type,
                entity_id=previous.entity_id,
            ).pack(),
        )])
    keyboard.append([InlineKeyboardButton(
        text="🏠 В главное меню",
        callback_data="back_to_main_menu",
    )])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def log_interactive_entity_view(user_id: int, entity: EntityRef) -> None:
    loggers = {
        "mob": log_view_mob,
        "resource": log_view_resource,
        "gear": log_view_gear,
        "card": log_view_card,
    }
    await loggers[entity.entity_type](user_id, entity.entity_id)


async def present_interactive_entity(
    callback: types.CallbackQuery,
    entity: EntityRef,
    previous: EntityRef | None,
    root_markup: InlineKeyboardMarkup | None = None,
) -> types.Message:
    keyboard = (
        root_markup if previous is None and root_markup is not None
        else build_interactive_navigation_keyboard(previous)
    )
    if entity.entity_type == "gear":
        data = await db.get_gear_card(entity.entity_id)
        card_view = await build_gear_card(
            db, entity.entity_id, data=data,
            bot_username=BOT_USERNAME, link_mode=EntityLinkMode.CALLBACK,
        )
        if data:
            gear_keyboard = await build_gear_card_keyboard(data, callback.from_user.id, 1, None)
            owner_rows = [
                row for row in gear_keyboard.inline_keyboard
                if any((button.callback_data or "").startswith("recipe_") for button in row)
            ]
            other_rows = [
                row for row in keyboard.inline_keyboard
                if not any((button.callback_data or "").startswith("recipe_") for button in row)
            ]
            keyboard = InlineKeyboardMarkup(inline_keyboard=[*owner_rows, *other_rows])
    else:
        card_view = await build_interactive_entity_card(entity)
    sent = await present_rich_card(
        bot=get_bound_bot(callback),
        chat_id=get_callback_message(callback).chat.id,
        card=card_view,
        reply_markup=keyboard,
        current_message=get_callback_message(callback),
    )
    return sent

async def get_gear_slots_keyboard(rarity: str) -> InlineKeyboardMarkup:
    counts = await db.execute_query(
        "SELECT slot, COUNT(*) AS item_count FROM gear WHERE rarity = ? GROUP BY slot",
        (rarity,),
    )
    count_by_slot = {row["slot"]: int(row["item_count"]) for row in counts}

    rows = []
    for index, slot in enumerate(GEAR_SLOT_ORDER):
        item_count = count_by_slot.get(slot, 0)
        if item_count <= 0:
            continue
        rows.append([InlineKeyboardButton(
            text=f"{SLOT_NAMES[slot]} ({item_count})",
            callback_data=f"gear_slot_{rarity}_{index}",
        )])

    if not rows:
        rows.append([InlineKeyboardButton(
            text="В этой категории пока нет предметов",
            callback_data="gear_empty_category",
        )])

    rows.append([InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ---------- Формирование карточек ----------
async def format_mob_card_plain(mob_id: int, location_id: int | None = None, page: int = 1, *, data: MobCardRow | None = None) -> str:
    return (await build_mob_card(db, mob_id, location_id, page, data=data, bot_username=BOT_USERNAME)).fallback_html


async def format_mob_card(mob_id: int, location_id: int | None = None, page: int = 1, *, data: MobCardRow | None = None) -> InputRichMessage:
    return (await build_mob_card(db, mob_id, location_id, page, data=data, bot_username=BOT_USERNAME)).rich_message

async def format_resource_card(resource_id: int, context_type: str | None = None, context_id: int | str | None = None, page: int = 1, *, data: ResourceCardRow | None = None) -> str:
    return (await build_resource_card(db, resource_id, context_type, context_id, page, data=data, bot_username=BOT_USERNAME)).fallback_html


async def format_resource_card_rich(resource_id: int, context_type: str | None = None, context_id: int | str | None = None, page: int = 1, *, data: ResourceCardRow | None = None) -> InputRichMessage:
    return (await build_resource_card(db, resource_id, context_type, context_id, page, data=data, bot_username=BOT_USERNAME)).rich_message

def format_recipe_owner(owner: RecipeOwnerEntry) -> str:
    username = owner["player_username"]
    if username:
        return f"@{escape_html(clean_username(username))}"
    user_id = owner["user_id"]
    if user_id is not None:
        return f"<a href='tg://user?id={user_id}'>Игрок {user_id}</a>"
    return "Неизвестный владелец"


def recipe_owner_labels(data: GearCardRow) -> list[str]:
    if "owner_entries" in data:
        return [format_recipe_owner(owner) for owner in data["owner_entries"]]
    return [f"@{escape_html(clean_username(username))}" for username in data.get("owners", [])]


async def format_gear_card_plain(
    gear_id: int, rarity: str | None = None, page: int = 1, *,
    data: GearCardRow | None = None, slot_index: int | None = None,
) -> str:
    return (await build_gear_card(db, gear_id, rarity, page, data=data, slot_index=slot_index, bot_username=BOT_USERNAME)).fallback_html


async def format_gear_card_rich(
    gear_id: int, rarity: str | None = None, page: int = 1, *,
    data: GearCardRow | None = None, slot_index: int | None = None,
) -> InputRichMessage:
    return (await build_gear_card(db, gear_id, rarity, page, data=data, slot_index=slot_index, bot_username=BOT_USERNAME)).rich_message

async def format_card_card(card_id: int, page: int = 1, context_type: str | None = None, context_id: int | None = None) -> str:
    return (await build_card_card(db, card_id, page, context_type, context_id, bot_username=BOT_USERNAME)).fallback_html


async def format_card_card_rich(card_id: int, page: int = 1, context_type: str | None = None, context_id: int | None = None) -> InputRichMessage:
    return (await build_card_card(db, card_id, page, context_type, context_id, bot_username=BOT_USERNAME)).rich_message


# ---------- Клавиатуры ----------
def get_main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🐾 Мобы"), KeyboardButton(text="📦 Ресурсы")],
            [KeyboardButton(text="⚔️ Снаряжение"), KeyboardButton(text="🔍 Поиск")]
        ],
        resize_keyboard=True
    )

def get_main_menu_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🐾 Мобы", callback_data="main_section_mobs"),
         InlineKeyboardButton(text="📦 Ресурсы", callback_data="main_section_resources")],
        [InlineKeyboardButton(text="⚔️ Снаряжение", callback_data="main_section_gear"),
         InlineKeyboardButton(text="🔍 Поиск", callback_data="main_section_search")],
    ])


async def get_locations_keyboard(category: str) -> InlineKeyboardMarkup:
    locations = await db.get_locations()
    keyboard = []

    for loc in locations:
        location_id = loc["id"]

        # Во вкладке «Мобы» пещерные локации доступны только через
        # подменю «Мертвого леса». Для остальных категорий список не меняется.
        if category == "mobs" and location_id in DEAD_FOREST_CHILD_LOCATION_IDS:
            continue

        callback_data = (
            "mobs_dead_forest_locations"
            if category == "mobs" and location_id == DEAD_FOREST_LOCATION_ID
            else f"list_{category}_{location_id}_1"
        )
        keyboard.append([
            InlineKeyboardButton(
                text=get_location_button_text(loc),
                callback_data=callback_data,
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def get_dead_forest_locations_keyboard() -> InlineKeyboardMarkup:
    """Формирует подменю Мертвого леса для выбора мобов."""
    locations = await db.get_locations()
    locations_by_id = {loc["id"]: loc for loc in locations}
    keyboard = []

    for location_id in DEAD_FOREST_GROUP_LOCATION_IDS:
        location = locations_by_id.get(location_id)
        if not location:
            logger.warning("Локация id=%s отсутствует в списке locations", location_id)
            continue

        keyboard.append([
            InlineKeyboardButton(
                text=get_location_button_text(location),
                callback_data=f"list_mobs_{location_id}_1",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            text="🔙 Назад к локациям",
            callback_data="back_to_locations_mobs",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_rarities_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{get_rarity_emoji(rarity)} {RARITY_NAMES[rarity]}",
            callback_data=f"gear_slots_{rarity}",
        )]
        for rarity in RARITY_ORDER
    ])

def get_inline_search_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Искать через @fog_database_bot", switch_inline_query_current_chat="")]
    ])

async def get_items_keyboard(category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ITEMS_PER_PAGE
    if category == "mobs":
        items = await db.get_mobs_by_location_sorted_by_hp(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    else:
        items = await db.get_resources_by_location(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]
    keyboard = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback_data = (
            MobViewCallback(item['id'], location_id, page).pack()
            if category == "mobs"
            else ResourceLocationViewCallback(item['id'], location_id, page).pack()
        )
        keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_{category}_{location_id}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_{category}_{location_id}_{page+1}"))
    if nav:
        keyboard.append(nav)
    if category == "mobs" and location_id in DEAD_FOREST_GROUP_LOCATION_IDS:
        back_text = "🔙 Назад к Мертвому лесу"
        back_callback = "mobs_dead_forest_locations"
    else:
        back_text = "🔙 Назад к локациям"
        back_callback = f"back_to_locations_{category}"

    keyboard.append([
        InlineKeyboardButton(text=back_text, callback_data=back_callback)
    ])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def get_gear_by_slot_keyboard(rarity: str, slot_index: int, page: int) -> InlineKeyboardMarkup:
    slot = GEAR_SLOT_ORDER[slot_index]
    offset = (page - 1) * ITEMS_PER_PAGE
    items = await db.get_gear_by_rarity_slot(
        rarity,
        slot,
        offset,
        ITEMS_PER_PAGE + FETCH_EXTRA,
    )
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]
    keyboard = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        keyboard.append([InlineKeyboardButton(
            text=name,
            callback_data=GearViewCallback(
                item['id'], rarity, slot_index, page
            ).pack(),
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_gear_{rarity}_{slot_index}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_gear_{rarity}_{slot_index}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к слотам", callback_data=f"gear_slots_{rarity}")])
    keyboard.append([InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def show_cards_list(target: types.Message | types.CallbackQuery, page: int) -> None:
    offset = (page - 1) * ITEMS_PER_PAGE
    cards = await db.get_all_cards_sorted_by_slot(offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(cards) > ITEMS_PER_PAGE
    cards = cards[:ITEMS_PER_PAGE]
    keyboard = []
    for card in cards:
        slot_icon = SLOT_ICONS.get(card['slot'], '❓')
        text = f"🃏{card['emoji']} {card['name']} {slot_icon}"
        keyboard.append([InlineKeyboardButton(
            text=text,
            callback_data=CardViewCallback(card['id'], page).pack(),
        )])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"cards_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"cards_page_{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_resource_cats")])

    if isinstance(target, types.Message):
        await target.answer("🃏 Список карт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await replace_callback_message_text(target, "🃏 Список карт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

def get_resource_categories_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(text="📦 Крафтовые", callback_data="resource_cat_craft")],
        [InlineKeyboardButton(text="✨ Расходуемые", callback_data="resource_cat_consumable")],
        [InlineKeyboardButton(text="📜 Рецепты экипировки", callback_data="resource_cat_scroll_recipe")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="resource_cat_currency")],
        [InlineKeyboardButton(text="⚗️ Алхимия", callback_data="resource_cat_alchemy")],
        [InlineKeyboardButton(text="🃏 Карты", callback_data="resource_cat_cards")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def show_resources_by_type(target: types.Message | types.CallbackQuery, resource_type: str, page: int) -> None:
    offset = (page - 1) * ITEMS_PER_PAGE
    items = await db.get_resources_by_type(resource_type, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]

    type_display = RESOURCE_TYPE_TITLES.get(resource_type, resource_type)

    keyboard = []
    for res in items:
        text = f"{res['emoji']} {res['name']}"
        callback_data = ResourceViewCallback(
            res['id'], resource_type, page
        ).pack()
        keyboard.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"res_page_{resource_type}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"res_page_{resource_type}_{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_resource_cats")])

    if isinstance(target, types.Message):
        await target.answer(f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await replace_callback_message_text(target, f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

async def delayed_log_inline_search(user_id: int, query: str, delay: float = 0.8) -> None:
    try:
        await asyncio.sleep(delay)
        if query.strip():
            await log_inline_search(user_id, query)
    finally:
        if inline_log_tasks.get(user_id) is asyncio.current_task():
            inline_log_tasks.pop(user_id, None)

# ---------- Обработчики ----------

@dp.message(Command("start", "menu"))
async def send_menu(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    args = (message.text or "").split(maxsplit=1)
    match = None
    return_context = None
    if len(args) > 1 and len(args[1]) <= 64:
        payload, separator, return_param = args[1].partition("-r-")
        match = re.fullmatch(r"(resource|mob|gear|card)_(\d+)", payload)
        return_context = parse_return_param(return_param if separator else None)
    if match and 1 <= int(match.group(2)) <= MAX_SQLITE_ID:
        target_type, target_id = match.group(1), int(match.group(2))
        link_mode = get_card_link_mode(message.chat)
        keyboard: InlineKeyboardMarkup | None = None
        if target_type == "mob":
            mob_data = await db.get_mob_full_card(target_id)
            if mob_data is None:
                await message.answer("Моб не найден.")
                return
            location_id = mob_data["location_id"]
            card_view = await build_mob_card(
                db, target_id, location_id, data=mob_data,
                bot_username=BOT_USERNAME, link_mode=link_mode,
            )
            target_callback = f"view_mobs_{target_id}_{location_id}_1"
        elif target_type == "resource":
            resource_data = await db.get_resource_card(target_id)
            if resource_data is None:
                await message.answer("Ресурс не найден.")
                return
            resource_type = resource_data["type"]
            card_view = await build_resource_card(
                db, target_id, "type", resource_type, data=resource_data,
                bot_username=BOT_USERNAME, link_mode=link_mode,
            )
            target_callback = f"view_resource_{target_id}_{resource_type}_1"
        elif target_type == "gear":
            gear_data = await db.get_gear_card(target_id)
            if gear_data is None:
                await message.answer("Предмет не найден.")
                return
            rarity = gear_data["rarity"]
            target_id = gear_data["id"]
            slot_index = GEAR_SLOT_ORDER.index(gear_data["slot"]) if gear_data["slot"] in GEAR_SLOT_ORDER else None
            card_view = await build_gear_card(
                db, target_id, rarity, data=gear_data, slot_index=slot_index,
                bot_username=BOT_USERNAME, link_mode=link_mode,
            )
            if message.from_user:
                keyboard = await build_gear_card_keyboard(gear_data, message.from_user.id, 1, slot_index)
            slot = f"{slot_index}_" if slot_index is not None else ""
            target_callback = f"view_gear_{target_id}_{rarity}_{slot}1"
        else:
            card_data = await db.get_card_by_id(target_id)
            if card_data is None:
                await message.answer("Карта не найдена.")
                return
            card_view = await build_card_card(
                db, target_id, data=card_data, bot_username=BOT_USERNAME, link_mode=link_mode,
            )
            target_callback = f"view_card_{target_id}_1"
        if keyboard is None:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📋 Открыть в каталоге", callback_data=target_callback),
            ]])
        if return_context:
            callback_data, button_text = return_button_data(return_context)
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=button_text, callback_data=callback_data)],
                *keyboard.inline_keyboard,
            ])
        await upsert_rich_card(
            bot=get_bound_bot(message), chat_id=message.chat.id, rich_message=card_view.rich_message,
            plain_text=card_view.fallback_html, reply_markup=keyboard, message_thread_id=message.message_thread_id,
        )
        if message.from_user:
            view_loggers = {
                "mob": log_view_mob, "resource": log_view_resource,
                "gear": log_view_gear, "card": log_view_card,
            }
            await view_loggers[target_type](message.from_user.id, target_id)
        try:
            await message.delete()
        except TelegramAPIError:
            logger.debug("Deep-link command could not be deleted", exc_info=True)
        return
    if message.from_user:
        await log_start(message.from_user.id)
    await message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())

@dp.message(Command("search"))
async def search_command(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🔎 Напиши название моба, ресурса, снаряжения или карты.")

@dp.message(F.text == "🐾 Мобы")
async def mobs_button(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    map_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "assets",
        "world_map.png",
    )

    if not os.path.isfile(map_path):
        logger.error("World map image not found: %s", map_path)
        await message.answer(
            "Выбери локацию мобов:",
            reply_markup=await get_locations_keyboard("mobs"),
        )
        return

    keyboard = await get_locations_keyboard("mobs")
    rich_message = InputRichMessage(
        html=(
            '<figure><img src="tg://photo?id=world_map"/>'
            '<figcaption>🐾 <b>Выбери локацию мобов:</b></figcaption>'
            '</figure>'
        ),
        media=[InputRichMessageMedia(
            id="world_map",
            media=InputMediaPhoto(media=FSInputFile(map_path)),
        )],
    )
    try:
        await get_bound_bot(message).send_rich_message(
            chat_id=message.chat.id,
            rich_message=rich_message,
            reply_markup=keyboard,
        )
    except TelegramAPIError as error:
        logger.warning("World map RichMessage failed, using text fallback: %s", error)
        await message.answer(
            "🐾 <b>Выбери локацию мобов:</b>",
            reply_markup=keyboard,
            parse_mode=ParseMode.HTML,
        )

@dp.message(F.text == "📦 Ресурсы")
async def resources_button(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выбери категорию ресурсов:", reply_markup=get_resource_categories_keyboard())

@dp.message(F.text == "⚔️ Снаряжение")
async def gear_button(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())

@dp.message(F.text == "🔍 Поиск")
async def search_button(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "Нажми на кнопку ниже, чтобы включить поиск.\nЗатем просто введи запрос (например, <b>бронзовик</b> или <b>хитин</b>).",
        reply_markup=get_inline_search_button(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "resource_cat_alchemy")
async def resource_cat_alchemy(callback: types.CallbackQuery, state: FSMContext) -> None:
    await show_resources_by_type(callback, 'alchemy', 1)
    await callback.answer()

@dp.callback_query(F.data == "resource_cat_cards")
async def resource_cat_cards(callback: types.CallbackQuery) -> None:
    await show_cards_list(callback, 1)
    await callback.answer()

# ---------- Текстовый поиск ----------
@dp.message(StateFilter(None), F.text & ~F.text.startswith('/') & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.via_bot)
async def handle_search(message: types.Message, state: FSMContext) -> None:
    query_text = (message.text or "").strip()
    if len(query_text) < 2:
        await message.answer("Введи хотя бы 2 символа для поиска.")
        return

    if len(query_text) > MAX_SEARCH_QUERY_LENGTH:
        await message.answer(f"Запрос слишком длинный. Максимум {MAX_SEARCH_QUERY_LENGTH} символов.")
        return
    if message.from_user:
        await log_search(message.from_user.id, query_text)

    results = await db.search(query_text)
    if not any(results.values()):
        await message.answer("Ничего не найдено.")
        return

    content = build_search_content(results, BOT_USERNAME)
    for chunk in split_formatted_text(content):
        await message.answer(chunk.text, entities=list(chunk.entities), parse_mode=None)

# ---------- Инлайн-поиск ----------
@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery) -> None:
    query = inline_query.query.strip()
    try:
        offset = int(inline_query.offset or "0")
    except ValueError:
        offset = -1
    if offset == 0:
        previous = inline_log_tasks.pop(inline_query.from_user.id, None)
        if previous is not None:
            previous.cancel()
    if not 2 <= len(query) <= MAX_SEARCH_QUERY_LENGTH:
        await inline_query.answer(
            [], cache_time=5, is_personal=True,
            button=InlineQueryResultsButton(text="Введи от 2 до 256 символов", start_parameter="start"),
        )
        return
    if offset < 0 or offset > 150 or offset % 50:
        await inline_query.answer([], cache_time=0, is_personal=True)
        return
    if offset == 0:
        inline_log_tasks[inline_query.from_user.id] = background_tasks.create_task(
            delayed_log_inline_search(inline_query.from_user.id, query),
            name=f"inline-search-{inline_query.from_user.id}",
        )
    entries = ranked_inline_items(await db.search(query), query)
    inline_results: list[InlineQueryResultUnion] = []
    for item_type, item in entries[offset:offset + 50]:
        if item_type == "mob":
            rich_message = await format_mob_card(item["id"])
            description = f"❤️ HP: {item.get('hp', 0)} | ⭐ Опыт: {item.get('exp', 0)}"
        elif item_type == "resource":
            rich_message = await format_resource_card_rich(item["id"])
            description = "Ресурс"
        elif item_type == "gear":
            rich_message = await format_gear_card_rich(item["id"], item.get("rarity"))
            description = f"{item.get('slot', '')} | {item.get('rarity', '')}"
        else:
            rich_message = await format_card_card_rich(item["id"])
            description = f"Карта · {item.get('slot', '')}"
        prefix = "res" if item_type == "resource" else item_type
        inline_results.append(InlineQueryResultArticle(
            id=f"{prefix}_{item['id']}", title=item["name"], description=description,
            input_message_content=InputRichMessageContent(rich_message=rich_message),
        ))
    next_offset = str(offset + 50) if offset + 50 < len(entries) else ""
    await inline_query.answer(inline_results, cache_time=0, is_personal=True, next_offset=next_offset)

@dp.chosen_inline_result()
async def chosen_inline_result_handler(chosen_result: types.ChosenInlineResult) -> None:
    from analytics import log_inline_result_chosen
    await log_inline_result_chosen(
        chosen_result.from_user.id,
        result_id=chosen_result.result_id,
        query=chosen_result.query
    )


async def edit_callback_window(
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
        await cleanup_card_fragments(get_bound_bot(callback), message.chat.id, message.message_id)
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
    await cleanup_card_fragments(get_bound_bot(callback), message.chat.id, message.message_id)

async def replace_callback_message_text(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    await edit_callback_window(callback, text, reply_markup, parse_mode)
    entity_navigation.clear(get_navigation_key(callback))

# ---------- Callback-обработчики ----------
@dp.callback_query(EntityNavigateCallback.filter())
async def navigate_related_entity(
    callback: types.CallbackQuery,
    callback_data: EntityNavigateCallback,
) -> None:
    message = callback.message
    if not isinstance(message, types.Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Открой карточку в личном чате с ботом.", show_alert=True)
        return
    source = EntityRef(callback_data.source_type, callback_data.source_id)
    target = EntityRef(callback_data.entity_type, callback_data.entity_id)
    if not source.is_valid or not target.is_valid:
        await callback.answer("Некорректная ссылка.", show_alert=True)
        return
    key = get_navigation_key(callback)
    await callback.answer()
    sent = await present_interactive_entity(callback, target, source)
    entity_navigation.visit(key, source, target, root_state=message.reply_markup)
    entity_navigation.transfer(key, (callback.from_user.id, sent.chat.id, sent.message_id))
    await log_interactive_entity_view(callback.from_user.id, target)


@dp.callback_query(EntityBackCallback.filter())
async def navigate_related_entity_back(
    callback: types.CallbackQuery,
    callback_data: EntityBackCallback,
) -> None:
    message = callback.message
    if not isinstance(message, types.Message) or message.chat.type != ChatType.PRIVATE:
        await callback.answer("Открой карточку в личном чате с ботом.", show_alert=True)
        return
    fallback = EntityRef(callback_data.entity_type, callback_data.entity_id)
    if not fallback.is_valid:
        await callback.answer("История переходов устарела.", show_alert=True)
        return
    key = get_navigation_key(callback)
    target = entity_navigation.previous(key) or fallback
    previous = entity_navigation.previous_after_back(key)
    root_markup = entity_navigation.root_state(key) if previous is None else None
    await callback.answer()
    sent = await present_interactive_entity(callback, target, previous, root_markup=root_markup)
    entity_navigation.back(key)
    entity_navigation.transfer(key, (callback.from_user.id, sent.chat.id, sent.message_id))
    await log_interactive_entity_view(callback.from_user.id, target)


@dp.callback_query(F.data == "gear_rarities")
async def gear_rarities_callback(callback: types.CallbackQuery) -> None:
    await replace_callback_message_text(callback, "Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "mobs_dead_forest_locations")
async def mobs_dead_forest_locations(callback: types.CallbackQuery) -> None:
    keyboard = await get_dead_forest_locations_keyboard()
    await edit_callback_window(
        callback,
        "🪾 <b>Мертвый лес</b>\nВыбери локацию мобов:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("back_to_locations_"))
async def back_to_locations(callback: types.CallbackQuery) -> None:
    category = get_callback_data(callback).split("_")[3]
    text = "Выбери локацию для мобов:" if category == "mobs" else "Выбери локацию для ресурсов:"
    keyboard = await get_locations_keyboard(category)
    await edit_callback_window(callback, text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith(("list_mobs_", "list_resources_", "page_mobs_", "page_resources_")))
async def list_or_page_callback(callback: types.CallbackQuery) -> None:
    parsed = parse_location_callback(get_callback_data(callback))
    if not parsed:
        await callback.answer("Некорректная ссылка на локацию.", show_alert=True)
        return
    category, _, loc_id, page = parsed
    location = await db.get_location_by_id(loc_id)
    if location is None:
        await callback.answer("Локация удалена. Открой меню заново.", show_alert=True)
        return
    keyboard = await get_items_keyboard(category, loc_id, page)
    title = get_location_list_title(location, category, page)
    await edit_callback_window(callback, title, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("gear_slots_"))
async def gear_slots_callback(callback: types.CallbackQuery) -> None:
    rarity = get_callback_data(callback).split("_", 2)[2]
    keyboard = await get_gear_slots_keyboard(rarity)
    await replace_callback_message_text(callback, "Выбери слот снаряжения:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "gear_empty_category")
async def gear_empty_category_callback(callback: types.CallbackQuery) -> None:
    await callback.answer("В этой категории пока нет предметов.", show_alert=False)

@dp.callback_query(F.data.startswith(("gear_slot_", "page_gear_")))
async def gear_list_or_page_callback(callback: types.CallbackQuery) -> None:
    parsed = parse_gear_list_callback(get_callback_data(callback))
    if not parsed:
        await callback.answer("Некорректная ссылка на снаряжение.", show_alert=True)
        return
    rarity, slot_index, page = parsed
    slot = GEAR_SLOT_ORDER[slot_index]
    keyboard = await get_gear_by_slot_keyboard(rarity, slot_index, page)
    text = f"⚔️ <b>{RARITY_NAMES.get(rarity, rarity)} · {SLOT_NAMES[slot]}</b>\nСтраница {page}"
    await replace_callback_message_text(callback, text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_mobs_"))
async def view_mob(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parsed = parse_location_callback(get_callback_data(callback))
    if not parsed or parsed[1] is None:
        return
    _, mob_id, location_id, page = parsed
    if mob_id is None:
        return

    await log_view_mob(callback.from_user.id, mob_id)

    card_view = await build_mob_card(
        db,
        mob_id,
        location_id,
        page,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(get_callback_message(callback).chat),
    )

    # Формируем клавиатуру
    neighbours = await db.get_prev_next_mob_by_hp(
        mob_id,
        location_id,
    )
    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=MobViewCallback(
                neighbours['prev_id'], location_id, page
            ).pack()
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=MobViewCallback(
                neighbours['next_id'], location_id, page
            ).pack()
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_mobs_{location_id}_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await upsert_rich_card(
        bot=get_bound_bot(callback),
        chat_id=get_callback_message(callback).chat.id,
        rich_message=card_view.rich_message,
        plain_text=card_view.fallback_html,
        reply_markup=reply_markup,
        current_message=get_callback_message(callback),
    )

@dp.callback_query(F.data.startswith(("view_resources_", "nav_resources_")))
async def view_resource(callback: types.CallbackQuery) -> None:
    await callback.answer()
    is_navigation = get_callback_data(callback).startswith("nav_resources_")
    parsed = parse_location_callback(get_callback_data(callback))
    if not parsed or parsed[1] is None:
        return
    _, res_id, location_id, page = parsed
    if res_id is None:
        return
    await log_view_resource(callback.from_user.id, res_id)

    card_view = await build_resource_card(
        db,
        res_id,
        context_type='location',
        context_id=location_id,
        page=page,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(get_callback_message(callback).chat),
    )

    neighbours = await db.get_prev_next_resource_by_location(
        res_id,
        location_id,
    )

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=ResourceLocationViewCallback(
                neighbours['prev_id'], location_id, page
            ).pack(navigation=True)
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=ResourceLocationViewCallback(
                neighbours['next_id'], location_id, page
            ).pack(navigation=True)
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_resources_{location_id}_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    render_card = replace_rich_card if is_navigation else upsert_rich_card
    await render_card(
        bot=get_bound_bot(callback),
        chat_id=get_callback_message(callback).chat.id,
        rich_message=card_view.rich_message,
        plain_text=card_view.fallback_html,
        reply_markup=reply_markup,
        current_message=get_callback_message(callback),
    )

async def build_gear_card_keyboard(
    data: GearCardRow,
    user_id: int,
    page: int,
    slot_index: int | None,
) -> InlineKeyboardMarkup:
    gear_id = data['id']
    rarity = data['rarity']

    if slot_index is not None:
        try:
            slot_index = GEAR_SLOT_ORDER.index(data['slot'])
        except ValueError:
            slot_index = None

    slot = GEAR_SLOT_ORDER[slot_index] if slot_index is not None else None
    neighbours = await db.get_prev_next_gear(gear_id, rarity, slot)
    nav_buttons = []
    for neighbour_id, text_label in (
        (neighbours['prev_id'], '◀️ Предыдущий'),
        (neighbours['next_id'], 'Следующий ▶️'),
    ):
        if not neighbour_id:
            continue
        if slot_index is None:
            callback_data = GearViewCallback(
                neighbour_id, rarity, None, page
            ).pack(navigation=True)
        else:
            callback_data = GearViewCallback(
                neighbour_id, rarity, slot_index, page
            ).pack(navigation=True)
        nav_buttons.append(InlineKeyboardButton(
            text=text_label,
            callback_data=callback_data,
        ))

    keyboard = [nav_buttons] if nav_buttons else []
    recipe_id = data.get('recipe_id')
    if data.get('can_learn', False) and recipe_id:
        is_owner = user_id in data.get('owner_user_ids', [])
        action = 'relinquish' if is_owner else 'claim'
        keyboard.append([InlineKeyboardButton(
            text="❌ Я не изучал рецепт" if is_owner else "✅ Я изучил рецепт",
            callback_data=RecipeOwnerCallback(
                action=action,
                recipe_id=recipe_id,
                gear_id=gear_id,
                rarity=rarity,
                page=page,
                slot_index=slot_index,
            ).pack(),
        )])

    back_callback = (
        f"page_gear_{rarity}_{slot_index}_{page}"
        if slot_index is not None
        else "gear_rarities"
    )
    keyboard.append([InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=back_callback,
    )])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def render_gear_card(
    callback: types.CallbackQuery,
    gear_id: int,
    rarity: str,
    page: int,
    slot_index: int | None = None,
    *,
    replace: bool = False,
) -> bool:
    data = await db.get_gear_card(gear_id)
    if not data:
        await edit_callback_window(callback, "Предмет не найден.")
        return False

    # Данные карточки являются источником истины: старые кнопки могут содержать
    # редкость, которая уже изменилась в админке.
    rarity = data['rarity']
    if slot_index is not None:
        try:
            slot_index = GEAR_SLOT_ORDER.index(data['slot'])
        except ValueError:
            slot_index = None
    card_view = await build_gear_card(
        db, data['id'], rarity, page, data=data, slot_index=slot_index,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(get_callback_message(callback).chat),
    )
    reply_markup = await build_gear_card_keyboard(
        data,
        callback.from_user.id,
        page,
        slot_index,
    )
    render_card = replace_rich_card if replace else upsert_rich_card
    await render_card(
        bot=get_bound_bot(callback),
        chat_id=get_callback_message(callback).chat.id,
        rich_message=card_view.rich_message,
        plain_text=card_view.fallback_html,
        reply_markup=reply_markup,
        current_message=get_callback_message(callback),
    )
    return True


@dp.callback_query(F.data.startswith(("view_gear_", "nav_gear_")))
async def view_gear(callback: types.CallbackQuery) -> None:
    parsed = parse_gear_view_callback(get_callback_data(callback))
    if not parsed:
        await callback.answer("Некорректная ссылка на снаряжение.", show_alert=True)
        return
    gear_id, rarity, slot_index, page = parsed
    await callback.answer()
    await log_view_gear(callback.from_user.id, gear_id)
    await render_gear_card(
        callback,
        gear_id,
        rarity,
        page,
        slot_index,
        replace=get_callback_data(callback).startswith("nav_gear_"),
    )

# ---------- Карты ----------
@dp.callback_query(F.data.startswith("cards_page_"))
async def cards_page_callback(callback: types.CallbackQuery) -> None:
    parsed = parse_card_callback(get_callback_data(callback))
    if not parsed:
        await callback.answer("Некорректная страница.", show_alert=True)
        return
    _, page = parsed
    await show_cards_list(callback, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_card_"))
async def view_card(callback: types.CallbackQuery) -> None:
    await callback.answer()
    parsed = parse_card_callback(get_callback_data(callback))
    if not parsed or parsed[0] is None:
        return
    card_id, page = parsed
    if card_id is None:
        return
    await log_view_card(callback.from_user.id, card_id)
    card_view = await build_card_card(
        db, card_id, page, bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(get_callback_message(callback).chat),
    )

    neighbours = await db.get_prev_next_card_by_slot(card_id)

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущая",
            callback_data=CardViewCallback(
                neighbours['prev_id'], page
            ).pack()
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующая ▶️",
            callback_data=CardViewCallback(
                neighbours['next_id'], page
            ).pack()
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"cards_page_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])

    await upsert_rich_card(
        bot=get_bound_bot(callback),
        chat_id=get_callback_message(callback).chat.id,
        rich_message=card_view.rich_message,
        plain_text=card_view.fallback_html,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        current_message=get_callback_message(callback),
    )

@dp.callback_query(F.data.startswith(("recipe_claim_", "recipe_relinquish_")))
async def update_recipe_owner(callback: types.CallbackQuery) -> None:
    parsed = parse_recipe_owner_callback(get_callback_data(callback))
    if not parsed:
        await callback.answer("Некорректная кнопка рецепта.", show_alert=True)
        return
    action, recipe_id, gear_id, rarity, slot_index, page = parsed
    gear = await db.get_gear_card(gear_id)
    if not gear or gear.get('recipe_id') != recipe_id:
        await callback.answer(
            "Рецепт изменился или был удалён. Открой карточку заново.",
            show_alert=True,
        )
        return
    if not gear.get('can_learn', False):
        await callback.answer(
            "Для этого снаряжения учёт владельцев рецепта недоступен.",
            show_alert=True,
        )
        return

    try:
        if action == 'claim':
            await db.claim_recipe_owner(
                recipe_id, callback.from_user.id, callback.from_user.username,
                expected_gear_id=gear_id,
            )
            result_text = "✅ Отмечено: ты изучил рецепт."
        else:
            await db.relinquish_recipe_owner(recipe_id, callback.from_user.id)
            result_text = "Отметка об изучении рецепта удалена."
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        return
    await callback.answer(result_text, show_alert=False)

    key = get_navigation_key(callback)
    entity = entity_navigation.current(key)
    if entity and entity.entity_type == "gear" and entity.entity_id in {gear_id, gear['id']}:
        previous = entity_navigation.previous(key)
        sent = await present_interactive_entity(
            callback, EntityRef("gear", gear['id']), previous,
            root_markup=entity_navigation.root_state(key) if previous is None else None,
        )
        entity_navigation.transfer(key, (callback.from_user.id, sent.chat.id, sent.message_id))
        return
    await render_gear_card(
        callback,
        gear_id,
        rarity,
        page,
        slot_index,
    )

# ---------- Ресурсы по категориям ----------
@dp.callback_query(F.data.startswith("resource_cat_"))
async def resource_category_callback(callback: types.CallbackQuery) -> None:
    resource_type = get_callback_data(callback).removeprefix("resource_cat_")
    if resource_type not in RESOURCE_TYPE_NAMES:
        await callback.answer("Неверная категория.", show_alert=True)
        return
    await show_resources_by_type(callback, resource_type, 1)
    await callback.answer()

@dp.callback_query(F.data.startswith("res_page_"))
async def resource_page_callback(callback: types.CallbackQuery) -> None:
    parsed = parse_resource_page_callback(get_callback_data(callback), "res_page_")
    if not parsed:
        await callback.answer("Неверная страница.", show_alert=True)
        return
    resource_type, page = parsed
    await show_resources_by_type(callback, resource_type, page)
    await callback.answer()

@dp.callback_query(F.data == "back_to_resource_cats")
async def back_to_resource_categories(callback: types.CallbackQuery) -> None:
    await replace_callback_message_text(callback, "Выбери категорию ресурсов:", reply_markup=get_resource_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith(("view_resource_", "nav_resource_")))
async def view_resource_by_type(callback: types.CallbackQuery) -> None:
    parsed = parse_resource_view_callback(get_callback_data(callback))
    if not parsed:
        await callback.answer("Неверная ссылка на ресурс.", show_alert=True)
        return
    resource_id, resource_type, page = parsed
    is_navigation = get_callback_data(callback).startswith("nav_resource_")
    await callback.answer()
    await log_view_resource(callback.from_user.id, resource_id)

    card_view = await build_resource_card(
        db,
        resource_id,
        context_type='type',
        context_id=resource_type,
        page=page,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(get_callback_message(callback).chat),
    )

    neighbours = await db.get_prev_next_resource_by_type(
        resource_id,
        resource_type,
    )

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=ResourceViewCallback(
                neighbours['prev_id'], resource_type, page
            ).pack(navigation=True)
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=ResourceViewCallback(
                neighbours['next_id'], resource_type, page
            ).pack(navigation=True)
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"res_page_{resource_type}_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    render_card = replace_rich_card if is_navigation else upsert_rich_card
    await render_card(
        bot=get_bound_bot(callback),
        chat_id=get_callback_message(callback).chat.id,
        rich_message=card_view.rich_message,
        plain_text=card_view.fallback_html,
        reply_markup=reply_markup,
        current_message=get_callback_message(callback),
    )

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await replace_callback_message_text(callback, "📋 Главное меню", reply_markup=get_main_menu_inline_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("main_section_"))
async def main_menu_section(callback: types.CallbackQuery, state: FSMContext) -> None:
    section = get_callback_data(callback).removeprefix("main_section_")
    if section not in {"mobs", "resources", "gear", "search"}:
        await callback.answer("Неизвестный раздел.", show_alert=True)
        return
    await state.clear()
    if section == "mobs":
        text = "Выбери локацию для мобов:"
        keyboard = await get_locations_keyboard("mobs")
    elif section == "resources":
        text = "Выбери категорию ресурсов:"
        keyboard = get_resource_categories_keyboard()
    elif section == "gear":
        text = "Выбери редкость снаряжения:"
        keyboard = get_rarities_keyboard()
    else:
        text = "Нажми кнопку поиска и введи название моба, ресурса, снаряжения или карты."
        keyboard = get_inline_search_button()
    rows = list(keyboard.inline_keyboard)
    if not any(button.callback_data == "back_to_main_menu" for row in rows for button in row):
        rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main_menu")])
    await replace_callback_message_text(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()

# ---------- Запуск ----------
async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise ValueError("BOT_TOKEN not set")
    logging.basicConfig(level=logging.INFO)
    bot = Bot(token=token)
    try:
        await db.connect()
        global BOT_USERNAME
        me = await bot.me()
        BOT_USERNAME = me.username
        dp.update.middleware(AnalyticsMiddleware())
        dp.include_router(admin_router)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot, close_bot_session=False, tasks_concurrency_limit=64)
    finally:
        try:
            await update_tasks.close(timeout=30.0)
        finally:
            try:
                await background_tasks.close(timeout=2.0)
            finally:
                try:
                    await db.close()
                finally:
                    await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
