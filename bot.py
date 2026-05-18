import os
import logging
import asyncio

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.filters import Command

from database import (
    get_locations,
    get_mobs_by_location,
    get_mob_drops,
    get_mob_gear_drops,
    get_resources_by_location,
    get_resource_mobs,
    get_gear_by_rarity,
    get_gear_info,
    get_gear_mobs,
    search,
    get_location_by_id,
    get_resource_info,
    execute_query
)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set")

ITEMS_PER_PAGE = 10
MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "🔍 Поиск"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ---------------------- Клавиатуры ----------------------
def get_main_menu_reply_keyboard() -> ReplyKeyboardMarkup:
    keyboard_rows = [
        [KeyboardButton(text="🐾 Мобы"), KeyboardButton(text="📦 Ресурсы")],
        [KeyboardButton(text="⚔️ Снаряжение"), KeyboardButton(text="🔍 Поиск")]
    ]
    return ReplyKeyboardMarkup(
        keyboard=keyboard_rows,
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_locations_keyboard(category: str) -> InlineKeyboardMarkup:
    """Клавиатура со списком локаций для мобов или ресурсов."""
    locations = get_locations()
    keyboard_rows = []
    for loc in locations:
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"{loc['emoji']} {loc['name']}",
                callback_data=f"list_{category}_{loc['id']}_1"
            )
        ])
    keyboard_rows.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def get_rarities_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора редкости для снаряжения."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚪ Обычное", callback_data="list_gear_common_1")],
        [InlineKeyboardButton(text="🟢 Редкое", callback_data="list_gear_rare_1")],
        [InlineKeyboardButton(text="🔵 Сверхредкое", callback_data="list_gear_epic_1")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")]
    ])

def get_items_keyboard(category: str, location_id: int, page: int) -> InlineKeyboardMarkup:
    """
    Клавиатура со списком мобов или ресурсов в выбранной локации.
    Для ресурсов в callback_data передаём ещё и location_id для возврата.
    """
    if category == "mobs":
        items = get_mobs_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        next_items = get_mobs_by_location(location_id, page*ITEMS_PER_PAGE, 1)
        total_items = page*ITEMS_PER_PAGE + len(next_items)
    elif category == "resources":
        items = get_resources_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        next_items = get_resources_by_location(location_id, page*ITEMS_PER_PAGE, 1)
        total_items = page*ITEMS_PER_PAGE + len(next_items)
    else:
        return InlineKeyboardMarkup(inline_keyboard=[])

    keyboard_rows = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        # Важно: для ресурсов добавляем location_id и page
        callback_data = f"view_{category}_{item['id']}_{location_id}_{page}"
        keyboard_rows.append([InlineKeyboardButton(text=name, callback_data=callback_data)])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page_{category}_{location_id}_{page-1}"))
    if page * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"page_{category}_{location_id}_{page+1}"))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard_rows.append([InlineKeyboardButton(
        text="🔙 Назад к локациям",
        callback_data=f"back_to_locations_{category}"
    )])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def get_gear_by_rarity_keyboard(rarity: str, page: int) -> InlineKeyboardMarkup:
    """Клавиатура со списком снаряжения выбранной редкости."""
    offset = (page - 1) * ITEMS_PER_PAGE
    items = get_gear_by_rarity(rarity, offset, ITEMS_PER_PAGE + 1)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]

    keyboard_rows = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback_data = f"view_gear_{item['id']}_{rarity}_{page}"
        keyboard_rows.append([InlineKeyboardButton(text=name, callback_data=callback_data)])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page_gear_{rarity}_{page-1}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"page_gear_{rarity}_{page+1}"))
    if nav_buttons:
        keyboard_rows.append(nav_buttons)

    keyboard_rows.append([InlineKeyboardButton(text="🔄 Выбрать другую редкость", callback_data="gear_rarities")])
    keyboard_rows.append([InlineKeyboardButton(text="🔙 В главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def get_inline_search_button() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Искать через @fog_database_bot", switch_inline_query_current_chat="")]
    ])

# ---------------------- Обработчики команд ----------------------
@dp.message(Command("start", "menu"))
async def send_menu(message: types.Message):
    await message.answer("Выбери категорию:", reply_markup=get_main_menu_reply_keyboard())

@dp.message(Command("search"))
async def search_command(message: types.Message):
    await message.answer("🔎 Просто напишите название моба, ресурса или предмета снаряжения в чат.")

# ---------------------- Обработчики reply-кнопок ----------------------
@dp.message(F.text == "🐾 Мобы")
async def mobs_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для мобов:", reply_markup=get_locations_keyboard("mobs"))

@dp.message(F.text == "📦 Ресурсы")
async def resources_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери локацию для ресурсов:", reply_markup=get_locations_keyboard("resources"))

@dp.message(F.text == "⚔️ Снаряжение")
async def gear_button(message: types.Message):
    await message.delete()
    await message.answer("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())

@dp.message(F.text == "🔍 Поиск")
async def search_button(message: types.Message):
    await message.delete()
    await message.answer(
        "Нажми на кнопку ниже, чтобы открыть инлайн-поиск.\n"
        "Затем просто введи запрос (например, *бронзовик*).",
        reply_markup=get_inline_search_button(),
        parse_mode="Markdown"
    )

# ---------------------- Обработчик обычного текстового поиска ----------------------
@dp.message(F.text & ~F.text.startswith('/') & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.via_bot)
async def handle_search(message: types.Message):
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

# ---------------------- ИНЛАЙН-ПОИСК ----------------------
@dp.inline_query()
async def inline_search_handler(inline_query: InlineQuery):
    query = inline_query.query.strip()
    user_id = inline_query.from_user.id
    logger.info(f"Инлайн-поиск от {user_id}: '{query}'")

    if not query:
        await inline_query.answer([], cache_time=5, switch_pm_text="🔍 Введите запрос для поиска", switch_pm_parameter="start")
        return

    results = search(query)
    inline_results = []

    for mob in results.get("mobs", [])[:50]:
        description = f"❤️ HP: {mob['hp']} | ✨ Пыль: {mob['dust_min']}-{mob['dust_max']} | ⭐ Опыт: {mob['exp']}"
        loc = get_location_by_id(mob["location_id"])
        loc_str = f"{loc['emoji']} {loc['name']}" if loc else "Неизвестно"
        message_text = (
            f"{mob['emoji']} *{mob['name']}*\n"
            f"❤️ HP: {mob['hp']}\n"
            f"✨ Пыль: {mob['dust_min']}-{mob['dust_max']}\n"
            f"⭐ Опыт: {mob['exp']}\n"
            f"📍 Локация: {loc_str}\n"
        )
        inline_results.append(InlineQueryResultArticle(
            id=f"mob_{mob['id']}",
            title=mob['name'],
            description=description,
            input_message_content=InputTextMessageContent(message_text=message_text, parse_mode="Markdown")
        ))

    for res in results.get("resources", [])[:50]:
        message_text = f"{res['emoji']} *{res['name']}*\n\n_Ресурс, который падает с мобов._"
        inline_results.append(InlineQueryResultArticle(
            id=f"res_{res['id']}",
            title=res['name'],
            description="Ресурс",
            input_message_content=InputTextMessageContent(message_text=message_text, parse_mode="Markdown")
        ))

    for gear in results.get("gear", [])[:50]:
        rarity_emoji = {"common":"⚪", "rare":"🟢", "epic":"🔵"}.get(gear["rarity"], "")
        description = f"{gear['slot']} | Редкость: {gear['rarity']}"
        message_text = f"{gear['emoji']} *{gear['name']}* {rarity_emoji}\nСлот: {gear['slot']}\nРедкость: {gear['rarity']}"
        inline_results.append(InlineQueryResultArticle(
            id=f"gear_{gear['id']}",
            title=gear['name'],
            description=description,
            input_message_content=InputTextMessageContent(message_text=message_text, parse_mode="Markdown")
        ))

    await inline_query.answer(inline_results, cache_time=0, is_personal=True)

# ---------------------- Callback-обработчики ----------------------
@dp.callback_query(F.data == "main_menu")
async def main_menu_callback(callback_query: types.CallbackQuery):
    await callback_query.message.delete()
    await callback_query.message.answer("Выбери категорию:", reply_markup=get_main_menu_reply_keyboard())
    await callback_query.answer()

@dp.callback_query(F.data == "gear_rarities")
async def gear_rarities_callback(callback_query: types.CallbackQuery):
    await callback_query.message.edit_text("Выбери редкость снаряжения:", reply_markup=get_rarities_keyboard())
    await callback_query.answer()

@dp.callback_query(F.data.startswith("back_to_locations_"))
async def back_to_locations(callback_query: types.CallbackQuery):
    category = callback_query.data.split("_")[3]
    text = "Выбери локацию для мобов:" if category == "mobs" else "Выбери локацию для ресурсов:"
    await callback_query.message.edit_text(text, reply_markup=get_locations_keyboard(category))
    await callback_query.answer()

# ----- Списки по редкости (снаряжение) -----
@dp.callback_query(F.data.startswith("list_gear_"))
async def list_gear_by_rarity(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    rarity = parts[2]
    page = int(parts[3])
    rarity_name = {"common": "Обычное", "rare": "Редкое", "epic": "Сверхредкое"}.get(rarity, rarity)
    keyboard = get_gear_by_rarity_keyboard(rarity, page)
    text = f"⚔️ *Снаряжение — {rarity_name}*\nСтраница {page}"
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query(F.data.startswith("page_gear_"))
async def page_gear_rarity(callback_query: types.CallbackQuery):
    parts = callback_query.data.split("_")
    rarity = parts[2]
    page = int(parts[3])
    rarity_name = {"common": "Обычное", "rare": "Редкое", "epic": "Сверхредкое"}.get(rarity, rarity)
    keyboard = get_gear_by_rarity_keyboard(rarity, page)
    text = f"⚔️ *Снаряжение — {rarity_name}*\nСтраница {page}"
    await callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)
    await callback_query.answer()

# ----- Списки мобов и ресурсов по локациям -----
@dp.callback_query(F.data.startswith("list_"))
async def list_callback(callback_query: types.CallbackQuery):
    if callback_query.data.startswith("list_gear_"):
        return
    _, category, loc_id, page = callback_query.data.split("_")
    loc_id, page = int(loc_id), int(page)
    location = get_location_by_id(loc_id)
    keyboard = get_items_keyboard(category, loc_id, page)
    title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}"
    await callback_query.message.edit_text(title, reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query(F.data.startswith("page_"))
async def page_callback(callback_query: types.CallbackQuery):
    if callback_query.data.startswith("page_gear_"):
        return
    _, category, loc_id, page = callback_query.data.split("_")
    loc_id, page = int(loc_id), int(page)
    location = get_location_by_id(loc_id)
    keyboard = get_items_keyboard(category, loc_id, page)
    title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}"
    await callback_query.message.edit_text(title, reply_markup=keyboard)
    await callback_query.answer()

# ----- Просмотр моба -----
@dp.callback_query(F.data.startswith("view_mobs_"))
async def view_mob(callback_query: types.CallbackQuery):
    # data = "view_mobs_{mob_id}_{location_id}_{page}"
    parts = callback_query.data.split("_")
    mob_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])

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
    text += f"❤️ HP: {mob['hp']}\n✨ Пыль: {mob['dust_min']}-{mob['dust_max']}\n⭐ Опыт: {mob['exp']}\n📍 Локация: {loc['emoji']} {loc['name']}\n\n"
    if drops:
        text += "*Падает:*\n" + "\n".join(f"{r['emoji']} {r['name']}" for r in drops) + "\n"
    if gear_drops:
        text += "\n*Снаряжение:*\n" + "\n".join(f"{g['emoji']} {g['name']} ({g['slot']})" for g in gear_drops) + "\n"

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку мобов",
        callback_data=f"list_mobs_{location_id}_{page}"
    )
    await callback_query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button]])
    )
    await callback_query.answer()

# ----- Просмотр ресурса (исправлен: возврат к списку ресурсов той же локации) -----
@dp.callback_query(F.data.startswith("view_resources_"))
async def view_resource(callback_query: types.CallbackQuery):
    # data = "view_resources_{resource_id}_{location_id}_{page}"
    parts = callback_query.data.split("_")
    resource_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])

    res = get_resource_info(resource_id)
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

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку ресурсов",
        callback_data=f"list_resources_{location_id}_{page}"
    )
    await callback_query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button]])
    )
    await callback_query.answer()

# ----- Просмотр снаряжения -----
@dp.callback_query(F.data.startswith("view_gear_"))
async def view_gear(callback_query: types.CallbackQuery):
    # data = "view_gear_{gear_id}_{rarity}_{page}"
    parts = callback_query.data.split("_")
    gear_id = int(parts[2])
    rarity = parts[3]
    page = int(parts[4])

    gear = get_gear_info(gear_id)
    if not gear:
        await callback_query.message.edit_text("Предмет не найден.")
        await callback_query.answer()
        return
    mobs = get_gear_mobs(gear_id) if gear["rarity"] == "common" else []
    text = f"{gear['emoji']} *{gear['name']}*\nРедкость: {gear['rarity']}\nСлот: {gear['slot']}\n"
    if gear.get("craftable"):
        text += f"Крафт: да, пыль: {gear['craft_dust']}\n"
    else:
        text += "Крафт: нет (выпадает)\n"
    if mobs:
        text += "\n*Выпадает с мобов:*\n" + "\n".join(f"{m['emoji']} {m['name']}" for m in mobs) + "\n"

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_gear_{rarity}_{page}"
    )
    other_rarity_button = InlineKeyboardButton(
        text="🔄 Выбрать другую редкость",
        callback_data="gear_rarities"
    )
    await callback_query.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[back_button], [other_rarity_button]])
    )
    await callback_query.answer()

# ---------------------- Запуск ----------------------
async def main():
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    asyncio.run(main())
