import asyncio
import logging
import os
import re

from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputRichMessageContent,
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
)
from ui.callbacks import (
    CardViewCallback,
    EntityBackCallback,
    EntityNavigateCallback,
    GearViewCallback,
    MobViewCallback,
    RecipeOwnerCallback,
    ResourceLocationViewCallback,
    ResourceViewCallback,
    parse_resource_page,
    parse_return_context,
)
from ui.cards import (
    RESOURCE_TYPE_NAMES,
    RESOURCE_TYPE_TITLES,
    build_card_card,
    build_gear_card,
    build_mob_card,
    build_resource_card,
    get_rarity_emoji,
)
from ui.rich import RichRenderMode, present_rich_card
from ui.links import EntityLinkMode
from ui.navigation import EntityNavigationHistory, EntityRef
from utils import escape_html

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set")

ITEMS_PER_PAGE = 10
FETCH_EXTRA = 1
MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "🔍 Поиск"}
BOT_USERNAME = None

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


def get_location_emoji(location: dict) -> str:
    """Возвращает emoji локации с безопасным fallback на значение из БД."""
    return LOCATION_EMOJI_OVERRIDES.get(location["id"], location.get("emoji") or "📍")


def get_location_button_text(location: dict) -> str:
    return f"{get_location_emoji(location)} {location['name']}"


def get_location_list_title(location: dict, category: str, page: int) -> str:
    category_title = LOCATION_CONTENT_TITLES.get(category, category)
    return f"{get_location_emoji(location)} {location['name']} - {category_title}\nСтраница {page}"


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
inline_log_tasks = {}
entity_navigation = EntityNavigationHistory()

bot = Bot(token=TOKEN)
dp = Dispatcher()


def get_card_link_mode(chat: types.Chat) -> EntityLinkMode:
    return (
        EntityLinkMode.CALLBACK
        if chat.type == ChatType.PRIVATE
        else EntityLinkMode.DEEP_LINK
    )


def get_navigation_key(callback: types.CallbackQuery) -> tuple[int, int, int]:
    return (
        callback.from_user.id,
        callback.message.chat.id,
        callback.message.message_id,
    )


async def build_interactive_entity_card(entity: EntityRef):
    common = {
        "bot_username": BOT_USERNAME,
        "link_mode": EntityLinkMode.CALLBACK,
    }
    if entity.entity_type == "mob":
        return await build_mob_card(db, entity.entity_id, **common)
    if entity.entity_type == "resource":
        return await build_resource_card(db, entity.entity_id, **common)
    if entity.entity_type == "gear":
        return await build_gear_card(db, entity.entity_id, **common)
    if entity.entity_type == "card":
        return await build_card_card(db, entity.entity_id, **common)
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
) -> None:
    old_key = get_navigation_key(callback)
    card_view = await build_interactive_entity_card(entity)
    sent = await present_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        card=card_view,
        reply_markup=build_interactive_navigation_keyboard(previous),
        current_message=callback.message,
    )
    new_key = (
        callback.from_user.id,
        sent.chat.id,
        sent.message_id,
    )
    entity_navigation.transfer(old_key, new_key)

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

# ---------- Клавиатуры ----------
def get_main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🐾 Мобы"), KeyboardButton(text="📦 Ресурсы")],
            [KeyboardButton(text="⚔️ Снаряжение"), KeyboardButton(text="🔍 Поиск")]
        ],
        resize_keyboard=True
    )

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

async def show_cards_list(target, page: int):
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
        await target.message.edit_text("🃏 Список карт:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

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

async def show_resources_by_type(target, resource_type: str, page: int):
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
        await target.message.edit_text(f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

async def delayed_log_inline_search(user_id: int, query: str, delay: float = 0.8):
    await asyncio.sleep(delay)
    if query.strip():
        await log_inline_search(user_id, query)
    inline_log_tasks.pop(user_id, None)

# ---------- Обработчики ----------
@dp.message(Command("start", "menu"))
async def send_menu(message: types.Message):
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        payload, separator, return_param = args[1].partition("-r-")
        payload_match = re.fullmatch(r"(resource|mob|gear|card)_(\d+)", payload)
        target_type = payload_match.group(1) if payload_match else None
        target_id = int(payload_match.group(2)) if payload_match else None
        if target_id is not None and target_id < 1:
            target_type = target_id = None
        return_context = parse_return_context(return_param if separator else None)

        if target_type and target_id is not None:
            # Определяем контекст для возврата (если есть)
            context_type = return_context.get("context_type") if return_context else None
            context_id = return_context.get("context_id") if return_context else None
            page = return_context.get("page", 1) if return_context else 1
            rarity = return_context.get("rarity") if return_context else None

            # Формируем карточку в зависимости от типа
            if target_type == "resource":
                card_view = await build_resource_card(
                    db,
                    target_id,
                    context_type=context_type,
                    context_id=context_id,
                    page=page,
                    bot_username=BOT_USERNAME,
                    link_mode=get_card_link_mode(message.chat),
                )
            elif target_type == "mob":
                card_view = await build_mob_card(
                    db,
                    target_id,
                    location_id=None,
                    page=page,
                    bot_username=BOT_USERNAME,
                    link_mode=get_card_link_mode(message.chat),
                )
            elif target_type == "gear":
                card_view = await build_gear_card(
                    db,
                    target_id,
                    rarity,
                    page,
                    bot_username=BOT_USERNAME,
                    link_mode=get_card_link_mode(message.chat),
                )
            elif target_type == "card":
                card_view = await build_card_card(
                    db,
                    target_id,
                    page=page,
                    context_type=context_type,
                    context_id=context_id,
                    bot_username=BOT_USERNAME,
                    link_mode=get_card_link_mode(message.chat),
                )
            else:
                await message.answer("Неизвестный тип объекта.")
                return

            # Строим кнопку возврата на основе проверенного контекста.
            keyboard = None
            if return_context:
                kind = return_context["kind"]
                item_id = return_context["item_id"]
                if kind == "gear":
                    callback_data = GearViewCallback(
                        item_id,
                        return_context['rarity'],
                        None,
                        page,
                    ).pack()
                    button_text = "🔙 Вернуться к снаряжению"
                elif kind == "mob":
                    callback_data = MobViewCallback(
                        item_id,
                        return_context['location_id'],
                        page,
                    ).pack()
                    button_text = "🔙 Вернуться к мобу"
                elif kind == "resource_loc":
                    callback_data = ResourceLocationViewCallback(
                        item_id,
                        return_context['context_id'],
                        page,
                    ).pack()
                    button_text = "🔙 Вернуться к ресурсу"
                else:
                    callback_data = ResourceViewCallback(
                        item_id,
                        return_context['context_id'],
                        page,
                    ).pack()
                    button_text = "🔙 Вернуться к ресурсу"
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=button_text, callback_data=callback_data)
                ]])

            await present_rich_card(
                bot=message.bot,
                chat_id=message.chat.id,
                card=card_view,
                reply_markup=keyboard,
            )
            try:
                await message.delete()
            except TelegramAPIError:
                logger.debug("Deep-link command could not be deleted", exc_info=True)
            return

    # Если нет параметров – главное меню
    await log_start(message.from_user.id)
    await message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())

@dp.message(Command("search"))
async def search_command(message: types.Message):
    await message.answer("🔎 Напиши название моба, ресурса, снаряжения или карты.")

@dp.message(F.text == "🐾 Мобы")
async def mobs_button(message: types.Message):
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

    await message.answer_photo(
        photo=FSInputFile(map_path),
        caption="🐾 <b>Выбери локацию мобов:</b>",
        reply_markup=await get_locations_keyboard("mobs"),
        parse_mode="HTML",
    )

@dp.message(F.text == "📦 Ресурсы")
async def resources_button(message: types.Message):
    await message.answer("Выбери категорию ресурсов:", reply_markup=get_resource_categories_keyboard())

@dp.message(F.text == "⚔️ Снаряжение")
async def gear_button(message: types.Message):
    await message.answer("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())

@dp.message(F.text == "🔍 Поиск")
async def search_button(message: types.Message):
    await message.answer(
        "Нажми на кнопку ниже, чтобы включить поиск.\nЗатем просто введи запрос (например, <b>бронзовик</b> или <b>хитин</b>).",
        reply_markup=get_inline_search_button(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "resource_cat_alchemy")
async def resource_cat_alchemy(callback: types.CallbackQuery, state: FSMContext):
    await show_resources_by_type(callback, 'alchemy', 1)
    await callback.answer()

@dp.callback_query(F.data == "resource_cat_cards")
async def resource_cat_cards(callback: types.CallbackQuery):
    await show_cards_list(callback, 1)
    await callback.answer()

# ---------- Текстовый поиск ----------
@dp.message(StateFilter(None), F.text & ~F.text.startswith('/') & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.via_bot)
async def handle_search(message: types.Message, state: FSMContext):
    query_text = message.text.strip()
    if len(query_text) < 2:
        await message.answer("Введи хотя бы 2 символа для поиска.")
        return

    await log_search(message.from_user.id, query_text)

    results = await db.search(query_text)
    if not any(results.values()):
        await message.answer("Ничего не найдено.")
        return

    reply = "🔎 <b>Результаты поиска:</b>\n\n"
    if results["mobs"]:
        reply += "<b>Мобы:</b>\n"
        for m in results["mobs"]:
            loc_str = f"{m['location_emoji']} {escape_html(m['location_name'])}" if m.get('location_name') else "?"
            reply += f"{m['emoji']} {escape_html(m['name'])} <i>{loc_str}</i>\n"
        reply += "\n"
    if results["resources"]:
        reply += "<b>Ресурсы:</b>\n"
        for r in results["resources"]:
            reply += f"{r['emoji']} {escape_html(r['name'])}\n"
        reply += "\n"
    if results["gear"]:
        reply += "<b>Снаряжение:</b>\n"
        for g in results["gear"]:
            reply += f"{g['emoji']} {escape_html(g['name'])} {get_rarity_emoji(g.get('rarity'))}\n"
        reply += "\n"
    if results["cards"]:
        reply += "<b>🃏 Карты:</b>\n"
        for c in results["cards"]:
            reply += f"{c['emoji']} {escape_html(c['name'])} (слот: {c['slot']})\n"
        reply += "\n"
    await message.answer(reply, parse_mode="HTML")

# ---------- Инлайн-поиск ----------
@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    query = inline_query.query.strip()

    if inline_query.from_user.id in inline_log_tasks:
        inline_log_tasks[inline_query.from_user.id].cancel()

    task = asyncio.create_task(delayed_log_inline_search(inline_query.from_user.id, query))
    inline_log_tasks[inline_query.from_user.id] = task

    if not query:
        await inline_query.answer([], cache_time=5, switch_pm_text="🔍 Введи запрос для поиска", switch_pm_parameter="start")
        return

    results = await db.search(query)
    inline_results = []

    for mob in results.get("mobs", [])[:50]:
        if len(inline_results) >= 50:
            break
        card_view = await build_mob_card(
            db,
            mob["id"],
            bot_username=BOT_USERNAME,
        )
        desc = f"❤️ HP: {mob['hp']} | ✨ Пыль: {mob['dust_min']}-{mob['dust_max']} | ⭐ Опыт: {mob['exp']}"
        inline_results.append(InlineQueryResultArticle(
            id=f"mob_{mob['id']}",
            title=mob['name'],
            description=desc,
            input_message_content=InputRichMessageContent(rich_message=card_view.rich_message)
        ))

    for res in results.get("resources", [])[:50]:
        if len(inline_results) >= 50:
            break
        card_view = await build_resource_card(
            db,
            res["id"],
            bot_username=BOT_USERNAME,
        )
        inline_results.append(InlineQueryResultArticle(
            id=f"res_{res['id']}",
            title=res['name'],
            description="Ресурс",
            input_message_content=InputRichMessageContent(rich_message=card_view.rich_message)
        ))

    for gear in results.get("gear", [])[:50]:
        if len(inline_results) >= 50:
            break
        card_view = await build_gear_card(
            db,
            gear["id"],
            gear.get("rarity"),
            bot_username=BOT_USERNAME,
        )
        inline_results.append(InlineQueryResultArticle(
            id=f"gear_{gear['id']}",
            title=gear['name'],
            description=f"{gear['slot']} | {gear['rarity']}",
            input_message_content=InputRichMessageContent(rich_message=card_view.rich_message)
        ))

    for card in results.get("cards", [])[:50]:
        if len(inline_results) >= 50:
            break
        card_view = await build_card_card(
            db,
            card["id"],
            bot_username=BOT_USERNAME,
        )
        inline_results.append(InlineQueryResultArticle(
            id=f"card_{card['id']}",
            title=card['name'],
            description=f"Слот: {card['slot']}",
            input_message_content=InputRichMessageContent(rich_message=card_view.rich_message)
        ))

    await inline_query.answer(inline_results, cache_time=0, is_personal=True)

@dp.chosen_inline_result()
async def chosen_inline_result_handler(chosen_result: types.ChosenInlineResult):
    from analytics import log_inline_result_chosen
    await log_inline_result_chosen(
        chosen_result.from_user.id,
        result_id=chosen_result.result_id,
        query=chosen_result.query
    )


async def replace_callback_message_text(
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
    message = callback.message
    if message.photo:
        await message.answer(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        try:
            await message.delete()
        except TelegramAPIError:
            logger.debug("Could not delete world-map message", exc_info=True)
        return

    await message.edit_text(
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )

# ---------- Callback-обработчики ----------
@dp.callback_query(EntityNavigateCallback.filter())
async def navigate_related_entity(
    callback: types.CallbackQuery,
    callback_data: EntityNavigateCallback,
):
    if callback.message.chat.type != ChatType.PRIVATE:
        await callback.answer(
            "Интерактивная навигация доступна в личном чате с ботом.",
            show_alert=True,
        )
        return

    source = EntityRef(callback_data.source_type, callback_data.source_id)
    target = EntityRef(callback_data.entity_type, callback_data.entity_id)
    if not source.is_valid or not target.is_valid:
        await callback.answer("Некорректная ссылка.", show_alert=True)
        return

    key = get_navigation_key(callback)
    entity_navigation.visit(key, source, target)
    previous = entity_navigation.previous(key)
    await callback.answer()
    await log_interactive_entity_view(callback.from_user.id, target)
    await present_interactive_entity(callback, target, previous)


@dp.callback_query(EntityBackCallback.filter())
async def navigate_related_entity_back(
    callback: types.CallbackQuery,
    callback_data: EntityBackCallback,
):
    fallback = EntityRef(callback_data.entity_type, callback_data.entity_id)
    if not fallback.is_valid:
        await callback.answer("История переходов устарела.", show_alert=True)
        return

    key = get_navigation_key(callback)
    target = entity_navigation.back(key) or fallback
    previous = entity_navigation.previous(key)
    await callback.answer()
    await log_interactive_entity_view(callback.from_user.id, target)
    await present_interactive_entity(callback, target, previous)


@dp.callback_query(F.data == "gear_rarities")
async def gear_rarities_callback(callback: types.CallbackQuery):
    await callback.message.edit_text("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "mobs_dead_forest_locations")
async def mobs_dead_forest_locations(callback: types.CallbackQuery):
    keyboard = await get_dead_forest_locations_keyboard()
    await replace_callback_message_text(
        callback,
        "🪾 <b>Мертвый лес</b>\nВыбери локацию мобов:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("back_to_locations_"))
async def back_to_locations(callback: types.CallbackQuery):
    category = callback.data.split("_")[3]
    text = "Выбери локацию для мобов:" if category == "mobs" else "Выбери локацию для ресурсов:"
    keyboard = await get_locations_keyboard(category)
    await replace_callback_message_text(callback, text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith(("list_mobs_", "list_resources_", "page_mobs_", "page_resources_")))
async def list_or_page_callback(callback: types.CallbackQuery):
    _, category, raw_location_id, raw_page = callback.data.split("_")
    loc_id = int(raw_location_id)
    page = int(raw_page)
    location = await db.get_location_by_id(loc_id)
    keyboard = await get_items_keyboard(category, loc_id, page)
    title = get_location_list_title(location, category, page)
    await replace_callback_message_text(callback, title, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("gear_slots_"))
async def gear_slots_callback(callback: types.CallbackQuery):
    rarity = callback.data.split("_", 2)[2]
    keyboard = await get_gear_slots_keyboard(rarity)
    await callback.message.edit_text("Выбери слот снаряжения:", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "gear_empty_category")
async def gear_empty_category_callback(callback: types.CallbackQuery):
    await callback.answer("В этой категории пока нет предметов.", show_alert=False)

@dp.callback_query(F.data.startswith(("gear_slot_", "page_gear_")))
async def gear_list_or_page_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if parts[0] == "gear":
        rarity, slot_index, page = parts[2], int(parts[3]), 1
    else:
        rarity, slot_index, page = parts[2], int(parts[3]), int(parts[4])
    slot = GEAR_SLOT_ORDER[slot_index]
    keyboard = await get_gear_by_slot_keyboard(rarity, slot_index, page)
    text = f"⚔️ <b>{RARITY_NAMES.get(rarity, rarity)} · {SLOT_NAMES[slot]}</b>\nСтраница {page}"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_mobs_"))
async def view_mob(callback: types.CallbackQuery):
    parsed = MobViewCallback.parse(callback.data)
    if not parsed:
        await callback.answer("Некорректная ссылка на моба.", show_alert=True)
        return
    await callback.answer()

    await log_view_mob(callback.from_user.id, parsed.mob_id)

    card_view = await build_mob_card(
        db,
        parsed.mob_id,
        parsed.location_id,
        parsed.page,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(callback.message.chat),
    )

    # Формируем клавиатуру
    neighbours = await db.get_prev_next_mob_by_hp(
        parsed.mob_id,
        parsed.location_id,
    )
    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=MobViewCallback(
                neighbours['prev_id'], parsed.location_id, parsed.page
            ).pack()
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=MobViewCallback(
                neighbours['next_id'], parsed.location_id, parsed.page
            ).pack()
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_mobs_{parsed.location_id}_{parsed.page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await present_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        card=card_view,
        reply_markup=reply_markup,
        current_message=callback.message,
    )

@dp.callback_query(F.data.startswith(("view_resources_", "nav_resources_")))
async def view_resource(callback: types.CallbackQuery):
    is_navigation = callback.data.startswith("nav_resources_")
    parsed = ResourceLocationViewCallback.parse(callback.data)
    if not parsed:
        await callback.answer("Некорректная ссылка на ресурс.", show_alert=True)
        return
    await callback.answer()
    await log_view_resource(callback.from_user.id, parsed.resource_id)

    card_view = await build_resource_card(
        db,
        parsed.resource_id,
        context_type='location',
        context_id=parsed.location_id,
        page=parsed.page,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(callback.message.chat),
    )

    neighbours = await db.get_prev_next_resource_by_location(
        parsed.resource_id,
        parsed.location_id,
    )

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=ResourceLocationViewCallback(
                neighbours['prev_id'], parsed.location_id, parsed.page
            ).pack(navigation=True)
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=ResourceLocationViewCallback(
                neighbours['next_id'], parsed.location_id, parsed.page
            ).pack(navigation=True)
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_resources_{parsed.location_id}_{parsed.page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await present_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        card=card_view,
        reply_markup=reply_markup,
        current_message=callback.message,
        mode=(RichRenderMode.REPLACE if is_navigation else RichRenderMode.EDIT),
    )

async def build_gear_card_keyboard(
    data: dict,
    username: str | None,
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
    for key, text_label in (
        ('prev_id', '◀️ Предыдущий'),
        ('next_id', 'Следующий ▶️'),
    ):
        neighbour_id = neighbours[key]
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
    if rarity == 'epic' and recipe_id and username:
        is_owner = username in data.get('owners', [])
        action = 'relinquish' if is_owner else 'claim'
        keyboard.append([InlineKeyboardButton(
            text="❌ У меня нет рецепта" if is_owner else "✅ У меня есть рецепт",
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
        await replace_callback_message_text(callback, "Предмет не найден.")
        return False

    # Данные карточки являются источником истины: старые кнопки могут содержать
    # редкость, которая уже изменилась в админке.
    rarity = data['rarity']
    card_view = await build_gear_card(
        db,
        gear_id,
        rarity,
        page,
        data=data,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(callback.message.chat),
    )
    reply_markup = await build_gear_card_keyboard(
        data,
        callback.from_user.username,
        page,
        slot_index,
    )
    await present_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        card=card_view,
        reply_markup=reply_markup,
        current_message=callback.message,
        mode=(RichRenderMode.REPLACE if replace else RichRenderMode.EDIT),
    )
    return True


@dp.callback_query(F.data.startswith(("view_gear_", "nav_gear_")))
async def view_gear(callback: types.CallbackQuery):
    parsed = GearViewCallback.parse(callback.data)
    if not parsed:
        await callback.answer("Некорректная ссылка на снаряжение.", show_alert=True)
        return
    await callback.answer()
    await log_view_gear(callback.from_user.id, parsed.gear_id)
    await render_gear_card(
        callback,
        parsed.gear_id,
        parsed.rarity,
        parsed.page,
        parsed.slot_index,
        replace=callback.data.startswith("nav_gear_"),
    )

# ---------- Карты ----------
@dp.callback_query(F.data.startswith("cards_page_"))
async def cards_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    await show_cards_list(callback, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_card_"))
async def view_card(callback: types.CallbackQuery):
    parsed = CardViewCallback.parse(callback.data)
    if not parsed:
        await callback.answer("Некорректная ссылка на карту.", show_alert=True)
        return
    await callback.answer()
    await log_view_card(callback.from_user.id, parsed.card_id)
    card_view = await build_card_card(
        db,
        parsed.card_id,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(callback.message.chat),
    )

    neighbours = await db.get_prev_next_card_by_slot(parsed.card_id)

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущая",
            callback_data=CardViewCallback(
                neighbours['prev_id'], parsed.page
            ).pack()
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующая ▶️",
            callback_data=CardViewCallback(
                neighbours['next_id'], parsed.page
            ).pack()
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"cards_page_{parsed.page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])

    await present_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        card=card_view,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        current_message=callback.message,
    )

@dp.callback_query(F.data.startswith(("recipe_claim_", "recipe_relinquish_")))
async def update_recipe_owner(callback: types.CallbackQuery):
    parsed = RecipeOwnerCallback.parse(callback.data)
    if not parsed:
        await callback.answer("Некорректная кнопка рецепта.", show_alert=True)
        return
    username = callback.from_user.username
    if not username:
        await callback.answer(
            "У тебя нет username. Установи его в настройках Telegram.",
            show_alert=True,
        )
        return

    gear = await db.get_gear_card(parsed.gear_id)
    if not gear or gear.get('recipe_id') != parsed.recipe_id:
        await callback.answer(
            "Рецепт изменился или был удалён. Открой карточку заново.",
            show_alert=True,
        )
        return
    if gear.get('rarity') != 'epic':
        await callback.answer(
            "Для этого снаряжения учёт владельцев рецепта недоступен.",
            show_alert=True,
        )
        return

    if parsed.action == 'claim':
        await db.add_recipe_owner(parsed.recipe_id, username)
        result_text = "✅ Ты добавлен в список владельцев рецепта!"
    else:
        await db.remove_recipe_owner(parsed.recipe_id, username)
        result_text = "❌ Ты удалён из списка владельцев рецепта."

    await render_gear_card(
        callback,
        parsed.gear_id,
        parsed.rarity,
        parsed.page,
        parsed.slot_index,
        replace=True,
    )
    await callback.answer(result_text, show_alert=False)

# ---------- Ресурсы по категориям ----------
@dp.callback_query(F.data.startswith("resource_cat_"))
async def resource_category_callback(callback: types.CallbackQuery):
    resource_type = callback.data.removeprefix("resource_cat_")
    if resource_type not in RESOURCE_TYPE_NAMES:
        await callback.answer("Неверная категория.", show_alert=True)
        return
    await show_resources_by_type(callback, resource_type, 1)
    await callback.answer()

@dp.callback_query(F.data.startswith("res_page_"))
async def resource_page_callback(callback: types.CallbackQuery):
    parsed = parse_resource_page(callback.data, "res_page_")
    if not parsed:
        await callback.answer("Неверная страница.", show_alert=True)
        return
    resource_type, page = parsed
    await show_resources_by_type(callback, resource_type, page)
    await callback.answer()

@dp.callback_query(F.data == "back_to_resource_cats")
async def back_to_resource_categories(callback: types.CallbackQuery):
    await callback.message.edit_text("Выбери категорию ресурсов:", reply_markup=get_resource_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith(("view_resource_", "nav_resource_")))
async def view_resource_by_type(callback: types.CallbackQuery):
    parsed = ResourceViewCallback.parse(callback.data)
    if not parsed:
        await callback.answer("Неверная ссылка на ресурс.", show_alert=True)
        return
    is_navigation = callback.data.startswith("nav_resource_")
    await callback.answer()
    await log_view_resource(callback.from_user.id, parsed.resource_id)

    card_view = await build_resource_card(
        db,
        parsed.resource_id,
        context_type='type',
        context_id=parsed.resource_type,
        page=parsed.page,
        bot_username=BOT_USERNAME,
        link_mode=get_card_link_mode(callback.message.chat),
    )

    neighbours = await db.get_prev_next_resource_by_type(
        parsed.resource_id,
        parsed.resource_type,
    )

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=ResourceViewCallback(
                neighbours['prev_id'], parsed.resource_type, parsed.page
            ).pack(navigation=True)
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=ResourceViewCallback(
                neighbours['next_id'], parsed.resource_type, parsed.page
            ).pack(navigation=True)
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"res_page_{parsed.resource_type}_{parsed.page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await present_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        card=card_view,
        reply_markup=reply_markup,
        current_message=callback.message,
        mode=(RichRenderMode.REPLACE if is_navigation else RichRenderMode.EDIT),
    )

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: types.CallbackQuery):
    entity_navigation.clear(get_navigation_key(callback))
    await callback.message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())
    await callback.message.delete()
    await callback.answer()

# ---------- Запуск ----------
async def main():
    try:
        await db.connect()

        global BOT_USERNAME
        me = await bot.me()
        BOT_USERNAME = me.username

        dp.update.middleware(AnalyticsMiddleware())
        dp.include_router(admin_router)
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        await db.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
