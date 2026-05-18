import os
import logging
from typing import Dict, List, Any, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor

# Импортируем все функции для работы с БД из database.py
from database import (
    get_locations,
    get_mobs_by_location,
    get_mob_drops,
    get_mob_gear_drops,
    get_resources_by_location,
    get_resource_mobs,
    get_gear_by_location,
    get_gear_info,
    get_gear_mobs,
    search,
    get_location_by_id,
    get_resource_info  # если понадобится
)

# --- Настройки ---
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена")

ITEMS_PER_PAGE = 10

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)
dp.middleware.setup(LoggingMiddleware())


# --- Клавиатуры ---
def get_main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply-клавиатура (сворачиваемая) с главными кнопками."""
    keyboard = ReplyKeyboardMarkup(
        resize_keyboard=True,
        one_time_keyboard=False,
        row_width=2
    )
    buttons = [
        KeyboardButton("🐾 Мобы"),
        KeyboardButton("📦 Ресурсы"),
        KeyboardButton("⚔️ Снаряжение"),
        KeyboardButton("🔍 Поиск")
    ]
    keyboard.add(*buttons)
    return keyboard

def get_locations_keyboard(category: str) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура для выбора локации."""
    locations = get_locations()
    keyboard = InlineKeyboardMarkup(row_width=1)
    for loc in locations:
        keyboard.add(InlineKeyboardButton(
            f"{loc['emoji']} {loc['name']}",
            callback_data=f"list_{category}_{loc['id']}_1"
        ))
    keyboard.add(InlineKeyboardButton("🔙 Назад в меню", callback_data="main_menu"))
    return keyboard

def get_items_keyboard(category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура со списком предметов и пагинацией."""
    if category == "mobs":
        items = get_mobs_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        # Для подсчёта общего количества используем тот же запрос без лимита, но лучше сделать отдельную функцию count_*
        # Временно: просто проверяем, есть ли следующая страница
        next_items = get_mobs_by_location(location_id, page*ITEMS_PER_PAGE, 1)
        total_items = page*ITEMS_PER_PAGE + len(next_items)  # грубо
    elif category == "resources":
        items = get_resources_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        next_items = get_resources_by_location(location_id, page*ITEMS_PER_PAGE, 1)
        total_items = page*ITEMS_PER_PAGE + len(next_items)
    elif category == "gear":
        items = get_gear_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        next_items = get_gear_by_location(location_id, page*ITEMS_PER_PAGE, 1)
        total_items = page*ITEMS_PER_PAGE + len(next_items)
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


# --- Обработчики команд и сообщений ---
@dp.message_handler(commands=['start', 'menu'])
async def send_menu(message: types.Message):
    """Отправляет приветствие и reply-клавиатуру без реплая на сообщение пользователя."""
    await message.answer(
        "Выбери категорию:",
        reply_markup=get_main_menu_reply_keyboard()
    )
    # Удаляем команду /start или /menu, чтобы чат был чище
    await message.delete()

@dp.message_handler(commands=['search'])
async def search_command(message: types.Message):
    await message.answer("Введите поисковый запрос (название моба, ресурса или снаряжения):")
    await message.delete()

@dp.message_handler(lambda message: message.text and not message.text.startswith('/'))
async def handle_search(message: types.Message):
    """Обработка поискового запроса (после команды /search или просто текста)."""
    query_text = message.text.strip()
    if len(query_text) < 2:
        await message.answer("Введите хотя бы 2 символа для поиска.")
        return
    results = search(query_text)
    if not any(results.values()):
        await message.answer("Ничего не найдено.")
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
    await message.answer(reply, parse_mode="Markdown")

# --- Обработчики reply-кнопок (текстовые сообщения) ---
# При нажатии на кнопку удаляем сообщение пользователя, чтобы не было видно текст кнопки,
# и отправляем ответ без реплая.
@dp.message_handler(lambda message: message.text == "🐾 Мобы")
async def mobs_button(message: types.Message):
    await message.delete()  # убираем сообщение "🐾 Мобы"
    await message.answer("Выбери локацию для мобов:", reply_markup=get_locations_keyboard("mobs"))

@dp.message_handler(lambda message: message.text == "📦 Ресурсы")
async def resources_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для ресурсов:", reply_markup=get_locations_keyboard("resources"))

@dp.message_handler(lambda message: message.text == "⚔️ Снаряжение")
async def gear_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для снаряжения:", reply_markup=get_locations_keyboard("gear"))

@dp.message_handler(lambda message: message.text == "🔍 Поиск")
async def search_button(message: types.Message):
    await message.delete()
    await search_command(message)  # отправляем сообщение с просьбой ввести запрос


# --- Инлайн-колбэки (навигация и просмотр) ---
@dp.callback_query_handler(lambda c: c.data == "main_menu")
async def main_menu_callback(callback_query: types.CallbackQuery):
    """Возврат в главное меню: удаляем текущее инлайн-сообщение и показываем новое с reply-клавиатурой."""
    await callback_query.message.delete()
    await callback_query.message.answer(
        "Выбери категорию:",
        reply_markup=get_main_menu_reply_keyboard()
    )
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
    # Функции get_mob_* работают с database.py, нужно импортировать execute_query? Нет, они уже есть.
    # Однако в database.py нет прямой функции get_mob_by_id, поэтому используем execute_query? Но лучше добавить.
    # Для простоты воспользуемся execute_query, но чтобы не плодить зависимости, перепишем: можно импортировать execute_query из database.py.
    from database import execute_query
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
    res = get_resource_info(resource_id)  # используем функцию из database.py
    if not res:
        await callback_query.message.edit_text("Ресурс не найден.")
        await callback_query.answer()
        return
    mobs = get_resource_mobs(resource_id)
    text = f"{res['emoji']} *{res['name']}*\n\n"
    if mobs:
        text += "*Падает с мобов:*\n" + "\n".join(f"{m['emoji']} {m['name']}" for m in mobs) + "\n"
    else:
        text += "Ни с кого не падает (возможно, крафтовый).\n"
    back_btn = InlineKeyboardButton("🔙 Назад в меню", callback_data="main_menu")
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
    back_btn = InlineKeyboardButton("🔙 Назад в меню", callback_data="main_menu")
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(row_width=1).add(back_btn))
    await callback_query.answer()


# --- Запуск ---
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
