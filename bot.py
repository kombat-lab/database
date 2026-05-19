import os
import logging
import asyncio
import re

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.filters import Command

from database import db

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set")

ITEMS_PER_PAGE = 10
FETCH_EXTRA = 1  # для определения наличия следующей страницы
MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "🔍 Поиск"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------- Экранирование Markdown ----------
def escape_markdown(text: str) -> str:
    """Экранирует спецсимволы для Telegram parse_mode='Markdown'."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(r'([%s])' % re.escape(escape_chars), r'\\\1', text)

def clean_username(username: str) -> str:
    """Убирает ведущий '@' если есть, чтобы затем добавить его при выводе."""
    return username.lstrip('@') if username else ''

# ---------------------- Формирование карточек (без дублирования) ----------------------
async def format_mob_card(mob: dict) -> str:
    loc = await db.get_location_by_id(mob["location_id"])
    loc_str = f"{loc['emoji']} {escape_markdown(loc['name'])}" if loc else "Неизвестно"
    drops = await db.get_mob_drops(mob["id"])
    gear_drops = await db.get_mob_gear_drops(mob["id"])

    text = f"{mob['emoji']} *{escape_markdown(mob['name'])}*\n"
    text += f"❤️ HP: {mob['hp']}\n✨ Пыль: {mob['dust_min']}-{mob['dust_max']}\n⭐ Опыт: {mob['exp']}\n📍 Локация: {loc_str}\n\n"
    if drops:
        text += "*Падает:*\n" + "\n".join(f"{r['emoji']} {escape_markdown(r['name'])}" for r in drops) + "\n"
    if gear_drops:
        text += "\n*Снаряжение:*\n" + "\n".join(f"{g['emoji']} {escape_markdown(g['name'])} ({g['slot']})" for g in gear_drops) + "\n"
    return text

async def format_resource_card(resource: dict) -> str:
    mobs = await db.get_resource_mobs(resource["id"])
    text = f"{resource['emoji']} *{escape_markdown(resource['name'])}*\n\n"
    if mobs:
        text += "*Падает с мобов:*\n" + "\n".join(f"{m['emoji']} {escape_markdown(m['name'])}" for m in mobs) + "\n"
    else:
        text += "_Ни с кого не падает (возможно, крафтовый)._"
    return text

async def format_gear_card(gear: dict) -> str:
    rarity_names = {"common": "Обычное", "rare": "Редкое", "epic": "Сверхредкое"}
    rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵"}
    rarity_text = f"{rarity_emoji[gear['rarity']]} {rarity_names[gear['rarity']]}"

    mobs = await db.get_gear_mobs(gear["id"]) if gear["rarity"] == "common" else []
    ingredients = await db.get_recipe_for_gear(gear["id"]) if gear.get("craftable") else []
    owners = await db.get_recipe_owners(gear["id"]) if gear.get("craftable") else []

    text = f"{gear['emoji']} *{escape_markdown(gear['name'])}*\n"
    text += f"Редкость: {rarity_text}\nСлот: {gear['slot']}\n"
    if gear.get("craftable"):
        text += f"Крафт: да\n\n*Требуемые ресурсы:*\n✨ Пыль — {gear['craft_dust']}\n"
        for ing in ingredients:
            text += f"{ing['emoji']} {escape_markdown(ing['name'])} — {ing['quantity']} шт.\n"
        if not ingredients:
            text += "_Рецепт не найден._\n"
        if owners:
            text += "\n👥 *Владельцы рецепта:*\n"
            for username in owners:
                clean = clean_username(username)
                text += f"@{escape_markdown(clean)}\n"
    else:
        text += "Крафт: нет\n"
    if mobs:
        text += "\n*Выпадает с мобов:*\n" + "\n".join(f"{m['emoji']} {escape_markdown(m['name'])}" for m in mobs) + "\n"
    return text

# ---------------------- Клавиатуры ----------------------
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
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_rarities_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚪ Обычное", callback_data="list_gear_common_1")],
        [InlineKeyboardButton(text="🟢 Редкое", callback_data="list_gear_rare_1")],
        [InlineKeyboardButton(text="🔵 Сверхредкое", callback_data="list_gear_epic_1")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")]
    ])

async def get_items_keyboard(category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ITEMS_PER_PAGE
    # Запрашиваем на 1 элемент больше, чтобы узнать о наличии следующей страницы
    if category == "mobs":
        items = await db.get_mobs_by_location(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    elif category == "resources":
        items = await db.get_resources_by_location(location_id, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    else:
        return InlineKeyboardMarkup(inline_keyboard=[])

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

    keyboard.append([InlineKeyboardButton(text="🔙 Назад к локациям",
                                          callback_data=f"back_to_locations_{category}")])
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
    keyboard.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_inline_search_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Искать через @fog_database_bot", switch_inline_query_current_chat="")]
    ])

# ---------------------- Обработчики команд ----------------------
@dp.message(Command("start", "menu"))
async def send_menu(message: types.Message):
    await message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())

@dp.message(Command("search"))
async def search_command(message: types.Message):
    await message.answer("🔎 Просто напишите название моба, ресурса или предмета снаряжения в чат.")

# ---------------------- Reply-кнопки ----------------------
@dp.message(F.text == "🐾 Мобы")
async def mobs_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для мобов:", reply_markup=await get_locations_keyboard("mobs"))

@dp.message(F.text == "📦 Ресурсы")
async def resources_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для ресурсов:", reply_markup=await get_locations_keyboard("resources"))

@dp.message(F.text == "⚔️ Снаряжение")
async def gear_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())

@dp.message(F.text == "🔍 Поиск")
async def search_button(message: types.Message):
    await message.delete()
    await message.answer(
        "Нажми на кнопку ниже, чтобы открыть инлайн-поиск.\nЗатем просто введи запрос (например, *бронзовик*).",
        reply_markup=get_inline_search_button(),
        parse_mode="Markdown"
    )

# ---------------------- Текстовый поиск ----------------------
@dp.message(F.text & ~F.text.startswith('/') & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.via_bot)
async def handle_search(message: types.Message):
    query_text = message.text.strip()
    if len(query_text) < 2:
        await message.answer("Введите хотя бы 2 символа для поиска.")
        return

    results = await db.search(query_text)
    if not any(results.values()):
        await message.answer("Ничего не найдено.")
        return

    reply = "🔎 *Результаты поиска:*\n\n"
    if results["mobs"]:
        reply += "*Мобы:*\n"
        for m in results["mobs"]:
            loc = await db.get_location_by_id(m["location_id"])
            loc_str = f"{loc['emoji']} {escape_markdown(loc['name'])}" if loc else "?"
            reply += f"{m['emoji']} {escape_markdown(m['name'])} ({loc_str})\n"
        reply += "\n"
    if results["resources"]:
        reply += "*Ресурсы:*\n" + "\n".join(f"{r['emoji']} {escape_markdown(r['name'])}" for r in results["resources"]) + "\n\n"
    if results["gear"]:
        reply += "*Снаряжение:*\n"
        rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵"}
        for g in results["gear"]:
            reply += f"{g['emoji']} {escape_markdown(g['name'])} {rarity_emoji.get(g['rarity'], '')}\n"
    await message.answer(reply, parse_mode="Markdown")

# ---------------------- Инлайн-поиск ----------------------
@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    query = inline_query.query.strip()
    if not query:
        await inline_query.answer([], cache_time=5, switch_pm_text="🔍 Введите запрос для поиска", switch_pm_parameter="start")
        return

    results = await db.search(query)
    inline_results = []

    # Мобы
    for mob in results.get("mobs", [])[:50]:
        text = await format_mob_card(mob)
        desc = f"❤️ HP: {mob['hp']} | ✨ Пыль: {mob['dust_min']}-{mob['dust_max']} | ⭐ Опыт: {mob['exp']}"
        inline_results.append(InlineQueryResultArticle(
            id=f"mob_{mob['id']}",
            title=mob['name'],
            description=desc,
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="Markdown")
        ))

    # Ресурсы
    for res in results.get("resources", [])[:50]:
        text = await format_resource_card(res)
        inline_results.append(InlineQueryResultArticle(
            id=f"res_{res['id']}",
            title=res['name'],
            description="Ресурс",
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="Markdown")
        ))

    # Снаряжение
    for gear in results.get("gear", [])[:50]:
        full_gear = await db.get_gear_info(gear["id"])
        if not full_gear:
            continue
        text = await format_gear_card(full_gear)
        inline_results.append(InlineQueryResultArticle(
            id=f"gear_{gear['id']}",
            title=full_gear['name'],
            description=f"{full_gear['slot']} | {full_gear['rarity']}",
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="Markdown")
        ))

    await inline_query.answer(inline_results, cache_time=0, is_personal=True)

# ---------------------- Callback-обработчики ----------------------
@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback_query: types.CallbackQuery):
    await callback_query.message.delete()
    await callback_query.message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())
    await callback_query.answer()

@dp.callback_query(F.data == "gear_rarities")
async def gear_rarities_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())
    await callback_query.answer()

@dp.callback_query(F.data.startswith("back_to_locations_"))
async def back_to_locations(callback_query: types.CallbackQuery):
    category = callback_query.data.split("_")[3]
    text = "Выбери локацию для мобов:" if category == "mobs" else "Выбери локацию для ресурсов:"
    keyboard = await get_locations_keyboard(category)
    await callback_query.message.edit_text(text, reply_markup=keyboard)
    await callback_query.answer()

# Объединённый обработчик для списков (мобы/ресурсы) и их страниц
@dp.callback_query(F.data.startswith(("list_mobs_", "list_resources_", "page_mobs_", "page_resources_")))
async def list_or_page_callback(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    # Формат: [префикс, category, location_id, page]
    # префикс может быть "list" или "page"
    if parts[0] in ("list", "page"):
        category = parts[1]  # "mobs" или "resources"
        loc_id = int(parts[2])
        page = int(parts[3])
    else:  # "list_mobs_1_2" -> parts = ["list", "mobs", "1", "2"]
        category = parts[1]
        loc_id = int(parts[2])
        page = int(parts[3])

    location = await db.get_location_by_id(loc_id)
    keyboard = await get_items_keyboard(category, loc_id, page)
    title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}"
    await callback_query.message.edit_text(title, reply_markup=keyboard)
    await callback_query.answer()

# Снаряжение по редкости (и страницы)
@dp.callback_query(F.data.startswith(("list_gear_", "page_gear_")))
async def gear_list_or_page_callback(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    # Пример: list_gear_common_1  или page_gear_common_2
    rarity = parts[2]
    page = int(parts[3])
    rarity_names = {"common": "Обычное", "rare": "Редкое", "epic": "Сверхредкое"}
    rarity_name = rarity_names.get(rarity, rarity)
    keyboard = await get_gear_by_rarity_keyboard(rarity, page)
    text = f"⚔️ *Снаряжение — {rarity_name}*\nСтраница {page}"
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback_query.answer()

# Просмотр моба
@dp.callback_query(F.data.startswith("view_mobs_"))
async def view_mob(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    mob_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])

    mob_res = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob_res:
        await callback_query.message.edit_text("Моб не найден.")
        await callback_query.answer()
        return
    mob = mob_res[0]
    text = await format_mob_card(mob)
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку мобов",
        callback_data=f"list_mobs_{location_id}_{page}"
    )
    await callback_query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button]])
    )
    await callback_query.answer()

# Просмотр ресурса
@dp.callback_query(F.data.startswith("view_resources_"))
async def view_resource(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    resource_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])

    res = await db.get_resource_info(resource_id)
    if not res:
        await callback_query.message.edit_text("Ресурс не найден.")
        await callback_query.answer()
        return
    text = await format_resource_card(res)
    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку ресурсов",
        callback_data=f"list_resources_{location_id}_{page}"
    )
    await callback_query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button]])
    )
    await callback_query.answer()

# Просмотр снаряжения
@dp.callback_query(F.data.startswith("view_gear_"))
async def view_gear(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    gear_id = int(parts[2])
    rarity = parts[3]
    page = int(parts[4])

    gear = await db.get_gear_info(gear_id)
    if not gear:
        await callback_query.message.edit_text("Предмет не найден.")
        await callback_query.answer()
        return

    text = await format_gear_card(gear)
    back_button = InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"list_gear_{rarity}_{page}")
    other_rarity_button = InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")
    await callback_query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button], [other_rarity_button]])
    )
    await callback_query.answer()

# ---------------------- Запуск ----------------------
async def main():
    await db.connect()
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())
