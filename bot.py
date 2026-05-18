import os
import logging
import sqlite3
from typing import Dict, List, Any, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# --- Настройки ---
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена")

DB_PATH = os.getenv("DATABASE_PATH", "game.db")
ITEMS_PER_PAGE = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())

# --- Работа с базой данных ---
def get_db_connection():
    return sqlite3.connect(DB_PATH)

def execute_query(query: str, params: tuple = ()) -> List[Dict[str, Any]]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def get_locations() -> List[Dict]:
    return execute_query("SELECT id, name, emoji FROM locations ORDER BY id")

def get_mobs_by_location(location_id: int, offset: int, limit: int) -> List[Dict]:
    return execute_query(
        "SELECT id, name, emoji, hp, dust_min, dust_max, exp FROM mobs WHERE location_id = ? LIMIT ? OFFSET ?",
        (location_id, limit, offset)
    )

def get_mob_drops(mob_id: int) -> List[Dict]:
    return execute_query(
        "SELECT r.id, r.name, r.emoji FROM mob_drops md JOIN resources r ON md.resource_id = r.id WHERE md.mob_id = ?",
        (mob_id,)
    )

def get_mob_gear_drops(mob_id: int) -> List[Dict]:
    return execute_query(
        "SELECT g.id, g.name, g.rarity, g.slot, g.emoji FROM gear_drops gd JOIN gear g ON gd.gear_id = g.id WHERE gd.mob_id = ? AND g.rarity = 'common'",
        (mob_id,)
    )

def get_resources_by_location(location_id: int, offset: int, limit: int) -> List[Dict]:
    query = """
        SELECT DISTINCT r.id, r.name, r.emoji
        FROM resources r
        JOIN mob_drops md ON r.id = md.resource_id
        JOIN mobs m ON md.mob_id = m.id
        WHERE m.location_id = ?
        LIMIT ? OFFSET ?
    """
    return execute_query(query, (location_id, limit, offset))

def get_resource_mobs(resource_id: int) -> List[Dict]:
    return execute_query(
        "SELECT m.id, m.name, m.emoji FROM mob_drops md JOIN mobs m ON md.mob_id = m.id WHERE md.resource_id = ?",
        (resource_id,)
    )

def get_gear_by_location(location_id: int, offset: int, limit: int) -> List[Dict]:
    query = """
        SELECT DISTINCT g.id, g.name, g.rarity, g.slot, g.emoji
        FROM gear g
        JOIN gear_drops gd ON g.id = gd.gear_id
        JOIN mobs m ON gd.mob_id = m.id
        WHERE m.location_id = ? AND g.rarity = 'common'
        LIMIT ? OFFSET ?
    """
    return execute_query(query, (location_id, limit, offset))

def get_gear_info(gear_id: int) -> Optional[Dict]:
    res = execute_query(
        "SELECT id, name, rarity, slot, craftable, craft_dust, emoji FROM gear WHERE id = ?",
        (gear_id,)
    )
    return res[0] if res else None

def get_gear_mobs(gear_id: int) -> List[Dict]:
    return execute_query(
        "SELECT m.id, m.name, m.emoji FROM gear_drops gd JOIN mobs m ON gd.mob_id = m.id WHERE gd.gear_id = ?",
        (gear_id,)
    )

def search(query: str) -> Dict[str, List[Dict]]:
    query = f"%{query}%"
    mobs = execute_query(
        "SELECT id, name, emoji, hp, dust_min, dust_max, exp, location_id FROM mobs WHERE name LIKE ?",
        (query,)
    )
    resources = execute_query(
        "SELECT id, name, emoji FROM resources WHERE name LIKE ?",
        (query,)
    )
    gear = execute_query(
        "SELECT id, name, rarity, slot, emoji FROM gear WHERE name LIKE ?",
        (query,)
    )
    return {"mobs": mobs, "resources": resources, "gear": gear}

def get_location_by_id(location_id: int) -> Optional[Dict]:
    res = execute_query("SELECT id, name, emoji FROM locations WHERE id = ?", (location_id,))
    return res[0] if res else None

# --- Клавиатуры и навигация ---
def get_main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("🐾 Мобы", callback_data="cat_mobs"),
        InlineKeyboardButton("📦 Ресурсы", callback_data="cat_resources"),
        InlineKeyboardButton("⚔️ Снаряжение", callback_data="cat_gear"),
        InlineKeyboardButton("🔍 Поиск", callback_data="search_mode")
    )
    return keyboard

def get_locations_keyboard(category: str) -> InlineKeyboardMarkup:
    locations = get_locations()
    keyboard = InlineKeyboardMarkup(row_width=1)
    for loc in locations:
        keyboard.add(InlineKeyboardButton(f"{loc['emoji']} {loc['name']}", callback_data=f"list_{category}_{loc['id']}_1"))
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="main_menu"))
    return keyboard

def get_items_keyboard(category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
    if category == "mobs":
        items = get_mobs_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        total_items = len(get_mobs_by_location(location_id, 0, 1000))
    elif category == "resources":
        items = get_resources_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        total_items = len(get_resources_by_location(location_id, 0, 1000))
    elif category == "gear":
        items = get_gear_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        total_items = len(get_gear_by_location(location_id, 0, 1000))
    else:
        return InlineKeyboardMarkup()

    keyboard = InlineKeyboardMarkup(row_width=1)
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback = f"view_{category}_{item['id']}"
        keyboard.add(InlineKeyboardButton(name, callback_data=callback))

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀ Назад", callback_data=f"page_{category}_{location_id}_{page-1}"))
    if page * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶", callback_data=f"page_{category}_{location_id}_{page+1}"))
    if nav_buttons:
        keyboard.row(*nav_buttons)
    keyboard.add(InlineKeyboardButton("🔙 Назад в меню", callback_data="main_menu"))
    return keyboard

# --- Обработчики ---
@dp.message_handler(commands=['start', 'menu'])
async def send_menu(message: types.Message):
    await message.reply("Выбери категорию:", reply_markup=get_main_menu_keyboard())

@dp.message_handler(commands=['search'])
async def search_command(message: types.Message):
    await message.reply("Введите поисковый запрос (название моба, ресурса или снаряжения):")

@dp.message_handler(lambda message: message.text and not message.text.startswith('/'))
async def handle_search(message: types.Message):
    query_text = message.text.strip()
    if len(query_text) < 2:
        await message.reply("Введите хотя бы 2 символа для поиска.")
        return
    results = search(query_text)
    if not any(results.values()):
        await message.reply("Ничего не найдено.")
        return
    reply = "🔎 *Результаты поиска:*\n\n"
    if results["mobs"]:
        reply += "*Мобы:*\n"
        for m in results["mobs"]:
            loc = get_location_by_id(m["location_id"])
            loc_emoji = loc["emoji"] if loc else ""
            reply += f"{m['emoji']} {m['name']} ({loc_emoji} {loc['name'] if loc else '?'})\n"
        reply += "\n"
    if results["resources"]:
        reply += "*Ресурсы:*\n"
        for r in results["resources"]:
            reply += f"{r['emoji']} {r['name']}\n"
        reply += "\n"
    if results["gear"]:
        reply += "*Снаряжение:*\n"
        for g in results["gear"]:
            rarity_emoji = {"common":"⚪", "rare":"🟢", "epic":"🔵"}.get(g["rarity"], "")
            reply += f"{g['emoji']} {g['name']} {rarity_emoji}\n"
        reply += "\n"
    reply += "Для подробностей используй меню или введи новый запрос."
    await message.reply(reply, parse_mode="Markdown")

@dp.callback_query_handler(lambda c: c.data == "main_menu")
async def main_menu_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Выбери категорию:", reply_markup=get_main_menu_keyboard())
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data == "search_mode")
async def search_mode_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Введите поисковый запрос (название моба, ресурса или снаряжения):")
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("cat_"))
async def category_callback(callback_query: types.CallbackQuery):
    category = callback_query.data[4:]  # mobs, resources, gear
    await callback_query.message.edit_text(f"Выбери локацию для {category}:", reply_markup=get_locations_keyboard(category))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("list_"))
async def list_callback(callback_query: types.CallbackQuery):
    _, category, loc_id, page = callback_query.data.split("_")
    loc_id = int(loc_id)
    page = int(page)
    location = get_location_by_id(loc_id)
    keyboard = get_items_keyboard(category, loc_id, page)
    title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}\n"
    await callback_query.message.edit_text(title, reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("page_"))
async def page_callback(callback_query: types.CallbackQuery):
    _, category, loc_id, page = callback_query.data.split("_")
    loc_id = int(loc_id)
    page = int(page)
    location = get_location_by_id(loc_id)
    keyboard = get_items_keyboard(category, loc_id, page)
    title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}\n"
    await callback_query.message.edit_text(title, reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("view_mobs_"))
async def view_mob(callback_query: types.CallbackQuery):
    mob_id = int(callback_query.data.split("_")[2])
    mob = execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback_query.message.edit_text("Моб не найден.")
        await callback_query.answer()
        return
    mob = mob[0]
    loc = get_location_by_id(mob["location_id"])
    drops = get_mob_drops(mob_id)
    gear_drops = get_mob_gear_drops(mob_id)
    text = f"{mob['emoji']} *{mob['name']}*\n"
    text += f"❤️ HP: {mob['hp']}\n"
    text += f"✨ Пыль: {mob['dust_min']}-{mob['dust_max']}\n"
    text += f"⭐ Опыт: {mob['exp']}\n"
    text += f"📍 Локация: {loc['emoji']} {loc['name']}\n\n"
    if drops:
        text += "*Падает:*\n" + "\n".join(f"{r['emoji']} {r['name']}" for r in drops) + "\n"
    if gear_drops:
        text += "\n*Снаряжение:*\n" + "\n".join(f"{g['emoji']} {g['name']} ({g['slot']})" for g in gear_drops) + "\n"
    back_btn = InlineKeyboardButton("🔙 Назад", callback_data=f"list_mobs_{mob['location_id']}_1")
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(row_width=1).add(back_btn))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("view_resources_"))
async def view_resource(callback_query: types.CallbackQuery):
    resource_id = int(callback_query.data.split("_")[2])
    res = execute_query("SELECT id, name, emoji FROM resources WHERE id = ?", (resource_id,))
    if not res:
        await callback_query.message.edit_text("Ресурс не найден.")
        await callback_query.answer()
        return
    res = res[0]
    mobs = get_resource_mobs(resource_id)
    text = f"{res['emoji']} *{res['name']}*\n\n"
    if mobs:
        text += "*Падает с мобов:*\n" + "\n".join(f"{m['emoji']} {m['name']}" for m in mobs) + "\n"
    else:
        text += "Ни с кого не падает (возможно, крафтовый).\n"
    back_btn = InlineKeyboardButton("🔙 Назад", callback_data="main_menu")
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(row_width=1).add(back_btn))
    await callback_query.answer()

@dp.callback_query_handler(lambda c: c.data.startswith("view_gear_"))
async def view_gear(callback_query: types.CallbackQuery):
    gear_id = int(callback_query.data.split("_")[2])
    gear = get_gear_info(gear_id)
    if not gear:
        await callback_query.message.edit_text("Предмет не найден.")
        await callback_query.answer()
        return
    mobs = get_gear_mobs(gear_id) if gear["rarity"] == "common" else []
    text = f"{gear['emoji']} *{gear['name']}*\n"
    text += f"Редкость: {gear['rarity']}\n"
    text += f"Слот: {gear['slot']}\n"
    if gear["craftable"]:
        text += f"Крафт: да, пыль: {gear['craft_dust']}\n"
    else:
        text += "Крафт: нет (выпадает)\n"
    if mobs:
        text += "\n*Выпадает с мобов:*\n" + "\n".join(f"{m['emoji']} {m['name']}" for m in mobs) + "\n"
    back_btn = InlineKeyboardButton("🔙 Назад", callback_data="main_menu")
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(row_width=1).add(back_btn))
    await callback_query.answer()

# --- Запуск ---
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
