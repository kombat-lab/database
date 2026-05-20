import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext

from database import db
from admin_handlers import admin_router
from utils import clean_username, escape_html

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set")

ITEMS_PER_PAGE = 10
FETCH_EXTRA = 1
MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "⚗️ Алхимия", "🔍 Поиск"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- Формирование карточек (без изменений) ----------
async def format_mob_card(mob_id: int) -> str:
    data = await db.get_mob_full_card(mob_id)
    if not data:
        return "Моб не найден."
    loc_str = f"{data['loc_emoji']} {escape_html(data['loc_name'])}"
    text = f"{data['emoji']} <b>{escape_html(data['name'])}</b>\n"
    text += f"❤️ HP: {data['hp']}\n✨ Пыль: {data['dust_min']}-{data['dust_max']}\n⭐ Опыт: {data['exp']}\n📍 Локация: {loc_str}\n\n"
    
    if data['resource_drops']:
        text += "<b>Падает:</b>\n" + "\n".join(f"{r['emoji']} {escape_html(r['name'])}" for r in data['resource_drops']) + "\n"
    
    if data['gear_drops']:
        rarity_emoji = {
            "common": "⚪",
            "rare": "🟢",
            "epic": "🔵"
        }
        text += "\n<b>Снаряжение:</b>\n"
        for g in data['gear_drops']:
            rarity_icon = rarity_emoji.get(g.get('rarity', 'common'), '⚪')
            text += f"{rarity_icon} {g['emoji']} {escape_html(g['name'])}\n"
    
    return text

async def format_resource_card(resource_id: int) -> str:
    data = await db.get_resource_card(resource_id)
    if not data:
        return "Ресурс не найден."
    type_names = {
        'craft': '📦 Крафтовый',
        'consumable': '✨ Расходуемый',
        'scroll_recipe': '📜 Рецепт экипировки',
        'scroll': '📜 Рецепт экипировки',
        'currency': '💰 Валюта'
    }
    type_str = type_names.get(data.get('type', 'craft'), '📦 Крафтовый')
    text = f"{data['emoji']} <b>{escape_html(data['name'])}</b>\n"
    text += f"🏷 Тип: {type_str}\n\n"
    if data['mobs']:
        text += "<b>Падает с мобов:</b>\n" + "\n".join(f"{m['emoji']} {escape_html(m['name'])}" for m in data['mobs']) + "\n"
    # Вместо старого else – выводим примечание, если оно есть
    if data.get('note'):
        text += f"\n📝 <i>{escape_html(data['note'])}</i>"
    return text

async def format_gear_card(gear_id: int) -> str:
    data = await db.get_gear_card(gear_id)
    if not data:
        return "Предмет не найден."
    rarity_names = {"common": "Обычное", "rare": "Редкое", "epic": "Сверхредкое"}
    rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵"}
    rarity_text = f"{rarity_emoji[data['rarity']]} {rarity_names[data['rarity']]}"
    text = f"{data['emoji']} <b>{escape_html(data['name'])}</b>\n"
    text += f"Редкость: {rarity_text}\nСлот: {data['slot']}\n"
    
    if data.get('craftable'):
        text += "Крафт: да\n\n<b>Требуемые ресурсы:</b>\n"
        dust = None
        other = []
        for ing in data['ingredients']:
            if ing['id'] == 71:
                dust = ing
            else:
                other.append(ing)
        if dust:
            text += f"✨ Пыль — {dust['quantity']}\n"
        for ing in other:
            text += f"{ing['emoji']} {escape_html(ing['name'])} — {ing['quantity']} шт.\n"
        if not data['ingredients']:
            text += "<i>Рецепт не найден.</i>\n"
        if data['owners']:
            text += "\n👥 <b>Владельцы рецепта:</b>\n"
            for username in data['owners']:
                clean = clean_username(username)
                text += f"@{escape_html(clean)}\n"
    else:
        text += "Крафт: нет\n"

    if data['mobs']:
        if data['rarity'] == 'epic':
            text += "\n<b>📜 Свиток падает с мобов:</b>\n"
        else:
            text += "\n<b>⚔️ Выпадает с мобов:</b>\n"
        text += "\n".join(f"{m['emoji']} {escape_html(m['name'])}" for m in data['mobs']) + "\n"
    return text

async def format_craft_resource_card(resource_id: int) -> str:
    res = await db.get_resource_by_id(resource_id)
    if not res:
        return "Ресурс не найден."
    recipe = await db.get_recipe_for_resource(resource_id)
    if not recipe or not recipe['ingredients']:
        return f"{res['emoji']} <b>{escape_html(res['name'])}</b>\n\n<i>Рецепт не найден.</i>"
    text = "⚗️ <b>Крафт ресурса</b>\n\n"
    text += f"{res['emoji']} <b>{escape_html(res['name'])}</b>\n"
    text += "<b>Ингредиенты:</b>\n"
    dust = None
    other = []
    for ing in recipe['ingredients']:
        if ing['resource_id'] == 71:
            dust = ing
        else:
            other.append(ing)
    if dust:
        text += f"✨ Пыль — {dust['quantity']}\n"
    for ing in other:
        text += f"{ing['emoji']} {escape_html(ing['name'])} — {ing['quantity']} шт.\n"
    text += "\n🏛 <b>Где крафтить:</b>\n"
    text += "🏛 Город - 🛣 Вторая улица - 👤 Алхимик - ⚗️ Алхимия"
    return text

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
    keyboard = [[InlineKeyboardButton(text=f"{loc['emoji']} {loc['name']}",
                                      callback_data=f"list_{category}_{loc['id']}_1")] for loc in locations]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_rarities_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚪ Обычное", callback_data="list_gear_common_1")],
        [InlineKeyboardButton(text="🟢 Редкое", callback_data="list_gear_rare_1")],
        [InlineKeyboardButton(text="🔵 Сверхредкое", callback_data="list_gear_epic_1")]
    ])

def get_inline_search_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Искать через @fog_database_bot", switch_inline_query_current_chat="")]
    ])

async def get_items_keyboard(category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ITEMS_PER_PAGE
    if category == "mobs":
        items = await db.get_mobs_by_location(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    else:
        items = await db.get_resources_by_location(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]
    keyboard = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback_data = f"view_{category}_{item['id']}_{location_id}_{page}"
        keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page_{category}_{location_id}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"page_{category}_{location_id}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к локациям", callback_data=f"back_to_locations_{category}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def get_gear_by_rarity_keyboard(rarity: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ITEMS_PER_PAGE
    items = await db.get_gear_by_rarity(rarity, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]
    keyboard = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback_data = f"view_gear_{item['id']}_{rarity}_{page}"
        keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page_gear_{rarity}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"page_gear_{rarity}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def show_craft_resources_list(target, resources: list, page: int):
    total = len(resources)
    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    page_items = resources[start:end]
    has_next = end < total

    keyboard = []
    for res in page_items:
        keyboard.append([InlineKeyboardButton(
            text=f"{res['emoji']} {res['name']}",
            callback_data=f"craft_resource_{res['id']}_{page}"
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"craft_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"craft_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main_menu")])

    if isinstance(target, types.Message):
        await target.answer("⚗️ Выберите ресурс для крафта:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await target.message.edit_text("⚗️ Выберите ресурс для крафта:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

def get_resource_categories_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(text="📦 Крафтовые", callback_data="resource_cat_craft")],
        [InlineKeyboardButton(text="✨ Расходуемые", callback_data="resource_cat_consumable")],
        [InlineKeyboardButton(text="📜 Рецепты экипировки", callback_data="resource_cat_scroll_recipe")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="resource_cat_currency")],
        [InlineKeyboardButton(text="⚗️ Алхимия", callback_data="resource_cat_alchemy")],
        [InlineKeyboardButton(text="Карты", callback_data="resource_cat_maps")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="back_to_main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def show_resources_by_type(target, resource_type: str, page: int):
    """Показывает список ресурсов определённого типа с пагинацией"""
    offset = (page - 1) * ITEMS_PER_PAGE
    items = await db.get_resources_by_type(resource_type, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]

    type_names = {
        'craft': 'Крафтовые',
        'consumable': 'Расходуемые',
        'scroll_recipe': 'Рецепты экипировки',
        'currency': 'Валюта'
    }
    type_display = type_names.get(resource_type, resource_type)

    keyboard = []
    for res in items:
        text = f"{res['emoji']} {res['name']}"
        callback_data = f"view_resource_{res['id']}_{resource_type}_{page}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"res_page_{resource_type}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"res_page_{resource_type}_{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_resource_cats")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main_menu")])

    if isinstance(target, types.Message):
        await target.answer(f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await target.message.edit_text(f"📦 Ресурсы — {type_display}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

# ---------- Обработчики ----------
@dp.message(Command("start", "menu"))
async def send_menu(message: types.Message):
    await message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())

@dp.message(Command("search"))
async def search_command(message: types.Message):
    await message.answer("🔎 Просто напишите название моба, ресурса или предмета снаряжения в чат.")

@dp.message(F.text == "🐾 Мобы")
async def mobs_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для мобов:", reply_markup=await get_locations_keyboard("mobs"))

@dp.message(F.text == "📦 Ресурсы")
async def resources_button(message: types.Message):
    await message.delete()
    await message.answer("Выберите категорию ресурсов:", reply_markup=get_resource_categories_keyboard())

@dp.message(F.text == "⚔️ Снаряжение")
async def gear_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())

@dp.message(F.text == "🔍 Поиск")
async def search_button(message: types.Message):
    await message.delete()
    await message.answer(
        "Нажми на кнопку ниже, чтобы включить поиск.\nЗатем просто введи запрос (например, <b>бронзовик</b> или <b>хитин</b>).",
        reply_markup=get_inline_search_button(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "resource_cat_alchemy")
async def resource_cat_alchemy(callback: types.CallbackQuery, state: FSMContext):
    """Показывает список крафтовых ресурсов (бывший раздел Алхимия)"""
    craftable_resources = await db.get_craftable_resources()
    if not craftable_resources:
        await callback.message.edit_text("Пока нет доступных рецептов крафта ресурсов.")
        return
    await show_craft_resources_list(callback, craftable_resources, 1)
    await callback.answer()

@dp.callback_query(F.data == "resource_cat_maps")
async def resource_cat_maps(callback: types.CallbackQuery):
    """Заглушка для категории Карты"""
    await callback.message.edit_text(
        "Раздел в разработке.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_resource_cats")]
        ])
    )
    await callback.answer()

# ---------- Текстовый поиск ----------
@dp.message(StateFilter(None), F.text & ~F.text.startswith('/') & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.via_bot)
async def handle_search(message: types.Message, state: FSMContext):
    query_text = message.text.strip()
    if len(query_text) < 2:
        await message.answer("Введите хотя бы 2 символа для поиска.")
        return

    results = await db.search(query_text)
    if not any(results.values()):
        await message.answer("Ничего не найдено.")
        return

    reply = "🔎 <b>Результаты поиска:</b>\n\n"
    if results["mobs"]:
        reply += "<b>Мобы:</b>\n"
        for m in results["mobs"]:
            loc_str = f"{m['location_emoji']} {escape_html(m['location_name'])}" if m.get('location_name') else "?"
            reply += f"{m['emoji']} {escape_html(m['name'])} ({loc_str})\n"
        reply += "\n"
    if results["resources"]:
        reply += "<b>Ресурсы:</b>\n"
        for r in results["resources"]:
            reply += f"{r['emoji']} {escape_html(r['name'])}\n"
        reply += "\n"
    if results["gear"]:
        reply += "<b>Снаряжение:</b>\n"
        rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵"}
        for g in results["gear"]:
            reply += f"{g['emoji']} {escape_html(g['name'])} {rarity_emoji.get(g['rarity'], '')}\n"
    await message.answer(reply, parse_mode="HTML")

# ---------- Инлайн-поиск ----------
@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    query = inline_query.query.strip()
    if not query:
        await inline_query.answer([], cache_time=5, switch_pm_text="🔍 Введите запрос для поиска", switch_pm_parameter="start")
        return

    results = await db.search(query)
    inline_results = []

    for mob in results.get("mobs", [])[:50]:
        text = await format_mob_card(mob["id"])
        desc = f"❤️ HP: {mob['hp']} | ✨ Пыль: {mob['dust_min']}-{mob['dust_max']} | ⭐ Опыт: {mob['exp']}"
        inline_results.append(InlineQueryResultArticle(
            id=f"mob_{mob['id']}",
            title=mob['name'],
            description=desc,
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML")
        ))

    for res in results.get("resources", [])[:50]:
        text = await format_resource_card(res["id"])
        inline_results.append(InlineQueryResultArticle(
            id=f"res_{res['id']}",
            title=res['name'],
            description="Ресурс",
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML")
        ))

    for gear in results.get("gear", [])[:50]:
        text = await format_gear_card(gear["id"])
        inline_results.append(InlineQueryResultArticle(
            id=f"gear_{gear['id']}",
            title=gear['name'],
            description=f"{gear['slot']} | {gear['rarity']}",
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML")
        ))

    await inline_query.answer(inline_results, cache_time=0, is_personal=True)

# ---------- Callback-обработчики ----------
@dp.callback_query(F.data == "gear_rarities")
async def gear_rarities_callback(callback: types.CallbackQuery):
    await callback.message.edit_text("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("back_to_locations_"))
async def back_to_locations(callback: types.CallbackQuery):
    category = callback.data.split("_")[3]
    text = "Выбери локацию для мобов:" if category == "mobs" else "Выбери локацию для ресурсов:"
    keyboard = await get_locations_keyboard(category)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith(("list_mobs_", "list_resources_", "page_mobs_", "page_resources_")))
async def list_or_page_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    if parts[0] in ("list", "page"):
        category = parts[1]
        loc_id = int(parts[2])
        page = int(parts[3])
    else:
        category = parts[1]
        loc_id = int(parts[2])
        page = int(parts[3])
    location = await db.get_location_by_id(loc_id)
    keyboard = await get_items_keyboard(category, loc_id, page)
    title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}"
    await callback.message.edit_text(title, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith(("list_gear_", "page_gear_")))
async def gear_list_or_page_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    rarity = parts[2]
    page = int(parts[3])
    rarity_names = {"common": "Обычное", "rare": "Редкое", "epic": "Сверхредкое"}
    rarity_name = rarity_names.get(rarity, rarity)
    keyboard = await get_gear_by_rarity_keyboard(rarity, page)
    text = f"⚔️ <b>Снаряжение — {rarity_name}</b>\nСтраница {page}"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_mobs_"))
async def view_mob(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    mob_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])
    text = await format_mob_card(mob_id)

    prev_mob = await db.execute_query(
        "SELECT id FROM mobs WHERE location_id = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (location_id, mob_id)
    )
    next_mob = await db.execute_query(
        "SELECT id FROM mobs WHERE location_id = ? AND id > ? ORDER BY id LIMIT 1",
        (location_id, mob_id)
    )
    nav_buttons = []
    if prev_mob:
        nav_buttons.append(InlineKeyboardButton(
            text="◀ Предыдущий",
            callback_data=f"view_mobs_{prev_mob[0]['id']}_{location_id}_{page}"
        ))
    if next_mob:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶",
            callback_data=f"view_mobs_{next_mob[0]['id']}_{location_id}_{page}"
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_mobs_{location_id}_{page}"
    )
    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_resources_"))
async def view_resource(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    res_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])
    text = await format_resource_card(res_id)

    prev_res = await db.execute_query(
        """
        SELECT r.id FROM resources r
        JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
        JOIN mobs m ON d.mob_id = m.id
        WHERE m.location_id = ? AND r.id < ?
        GROUP BY r.id
        ORDER BY r.id DESC LIMIT 1
        """,
        (location_id, res_id)
    )
    next_res = await db.execute_query(
        """
        SELECT r.id FROM resources r
        JOIN drops d ON d.item_type = 'resource' AND d.item_id = r.id
        JOIN mobs m ON d.mob_id = m.id
        WHERE m.location_id = ? AND r.id > ?
        GROUP BY r.id
        ORDER BY r.id LIMIT 1
        """,
        (location_id, res_id)
    )
    nav_buttons = []
    if prev_res:
        nav_buttons.append(InlineKeyboardButton(
            text="◀ Предыдущий",
            callback_data=f"view_resources_{prev_res[0]['id']}_{location_id}_{page}"
        ))
    if next_res:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶",
            callback_data=f"view_resources_{next_res[0]['id']}_{location_id}_{page}"
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_resources_{location_id}_{page}"
    )
    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("view_gear_"))
async def view_gear(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    gear_id = int(parts[2])
    rarity = parts[3]
    page = int(parts[4])
    text = await format_gear_card(gear_id)

    prev_gear = await db.execute_query(
        "SELECT id FROM gear WHERE rarity = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (rarity, gear_id)
    )
    next_gear = await db.execute_query(
        "SELECT id FROM gear WHERE rarity = ? AND id > ? ORDER BY id LIMIT 1",
        (rarity, gear_id)
    )
    nav_buttons = []
    if prev_gear:
        nav_buttons.append(InlineKeyboardButton(
            text="◀ Предыдущий",
            callback_data=f"view_gear_{prev_gear[0]['id']}_{rarity}_{page}"
        ))
    if next_gear:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶",
            callback_data=f"view_gear_{next_gear[0]['id']}_{rarity}_{page}"
        ))
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_gear_{rarity}_{page}"
    )
    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

# ---------- Крафт ----------
@dp.callback_query(F.data.startswith("craft_page_"))
async def craft_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    craftable_resources = await db.get_craftable_resources()
    await show_craft_resources_list(callback, craftable_resources, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("craft_resource_"))
async def view_craft_resource(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    resource_id = int(parts[2])
    page = int(parts[3])
    text = await format_craft_resource_card(resource_id)
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"craft_back_to_list_{page}"
    )
    keyboard = [[back_button]]
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("craft_back_to_list_"))
async def craft_back_to_list(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[4])
    craftable_resources = await db.get_craftable_resources()
    await show_craft_resources_list(callback, craftable_resources, page)
    await callback.answer()

# ---------- НОВЫЕ CALLBACK ДЛЯ РЕСУРСОВ ПО ТИПАМ ----------
@dp.callback_query(F.data.startswith("resource_cat_"))
async def resource_category_callback(callback: types.CallbackQuery):
    resource_type = callback.data.split("_")[2]  # craft, consumable, scroll_recipe, currency
    await show_resources_by_type(callback, resource_type, 1)
    await callback.answer()

@dp.callback_query(F.data.startswith("res_page_"))
async def resource_page_callback(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    resource_type = parts[2]
    page = int(parts[3])
    await show_resources_by_type(callback, resource_type, page)
    await callback.answer()

@dp.callback_query(F.data == "back_to_resource_cats")
async def back_to_resource_categories(callback: types.CallbackQuery):
    await callback.message.edit_text("Выберите категорию ресурсов:", reply_markup=get_resource_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("view_resource_"))
async def view_resource_by_type(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    resource_id = int(parts[2])
    resource_type = parts[3]   # может не использоваться, но полезно для навигации
    page = int(parts[4])
    
    text = await format_resource_card(resource_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"res_page_{resource_type}_{page}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main_menu")]
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())
    await callback.answer()

# ---------- Запуск ----------
async def main():
    await db.connect()
    dp.include_router(admin_router)
    await dp.start_polling(bot, skip_updates=True)
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
