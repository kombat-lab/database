import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes
)
import database as db

# Токен из переменной окружения
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("Переменная окружения BOT_TOKEN не установлена")

ITEMS_PER_PAGE = 5

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def build_menu_buttons(category: str, location_id: int = None, page: int = 1):
    """Строит инлайн-клавиатуру для списка элементов"""
    buttons = []
    if category == "mobs":
        items = db.get_mobs_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        total_items = len(db.get_mobs_by_location(location_id, 0, 1000))
    elif category == "resources":
        items = db.get_resources_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        total_items = len(db.get_resources_by_location(location_id, 0, 1000))
    elif category == "gear":
        items = db.get_gear_by_location(location_id, (page-1)*ITEMS_PER_PAGE, ITEMS_PER_PAGE)
        total_items = len(db.get_gear_by_location(location_id, 0, 1000))
    else:
        return [], 0, 0

    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback = f"view_{category}_{item['id']}"
        buttons.append([InlineKeyboardButton(name, callback_data=callback)])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀ Назад", callback_data=f"page_{category}_{location_id}_{page-1}"))
    if page * ITEMS_PER_PAGE < total_items:
        nav_buttons.append(InlineKeyboardButton("Вперед ▶", callback_data=f"page_{category}_{location_id}_{page+1}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    back_btn = [InlineKeyboardButton("🔙 Назад в меню", callback_data="main_menu")]
    buttons.append(back_btn)
    return buttons, items, total_items

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для игры.\n"
        "Используй кнопки в меню или команду /menu для навигации.\n"
        "Также ты можешь искать мобов, ресурсы и снаряжение через /search."
    )

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🐾 Мобы", callback_data="cat_mobs")],
        [InlineKeyboardButton("📦 Ресурсы", callback_data="cat_resources")],
        [InlineKeyboardButton("⚔️ Снаряжение", callback_data="cat_gear")],
        [InlineKeyboardButton("🔍 Поиск", callback_data="search_mode")],
    ]
    await update.message.reply_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите поисковый запрос (название моба, ресурса или снаряжения):")

async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.message.text.strip()
    if len(query_text) < 2:
        await update.message.reply_text("Введите хотя бы 2 символа для поиска.")
        return
    results = db.search(query_text)
    if not any(results.values()):
        await update.message.reply_text("Ничего не найдено.")
        return
    reply = "🔎 *Результаты поиска:*\n\n"
    if results["mobs"]:
        reply += "*Мобы:*\n"
        for m in results["mobs"]:
            loc = db.get_location_by_id(m["location_id"])
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
    await update.message.reply_text(reply, parse_mode="Markdown")

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "main_menu":
        keyboard = [
            [InlineKeyboardButton("🐾 Мобы", callback_data="cat_mobs")],
            [InlineKeyboardButton("📦 Ресурсы", callback_data="cat_resources")],
            [InlineKeyboardButton("⚔️ Снаряжение", callback_data="cat_gear")],
            [InlineKeyboardButton("🔍 Поиск", callback_data="search_mode")],
        ]
        await query.edit_message_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data == "search_mode":
        await query.edit_message_text("Введите поисковый запрос (название моба, ресурса или снаряжения):")
        return

    if data.startswith("cat_"):
        category = data[4:]  # mobs, resources, gear
        locations = db.get_locations()
        buttons = []
        for loc in locations:
            buttons.append([InlineKeyboardButton(f"{loc['emoji']} {loc['name']}", callback_data=f"list_{category}_{loc['id']}_1")])
        buttons.append([InlineKeyboardButton("🔙 Назад", callback_data="main_menu")])
        await query.edit_message_text(f"Выбери локацию для {category}:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("list_"):
        # format: list_{category}_{location_id}_{page}
        _, category, loc_id, page = data.split("_")
        loc_id = int(loc_id)
        page = int(page)
        buttons, items, total = build_menu_buttons(category, loc_id, page)
        location = db.get_location_by_id(loc_id)
        title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}\n"
        if not items:
            title += "В этой локации ничего нет."
        await query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("page_"):
        # page_{category}_{location_id}_{page}
        _, category, loc_id, page = data.split("_")
        loc_id = int(loc_id)
        page = int(page)
        buttons, items, total = build_menu_buttons(category, loc_id, page)
        location = db.get_location_by_id(loc_id)
        title = f"{location['emoji']} {location['name']} - {category.capitalize()}\nСтраница {page}\n"
        await query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("view_"):
        # view_{category}_{id}
        _, category, item_id = data.split("_")
        item_id = int(item_id)
        if category == "mobs":
            mob = db.execute_query("SELECT * FROM mobs WHERE id = ?", (item_id,))
            if not mob:
                await query.edit_message_text("Моб не найден.")
                return
            mob = mob[0]
            loc = db.get_location_by_id(mob["location_id"])
            drops = db.get_mob_drops(item_id)
            gear_drops = db.get_mob_gear_drops(item_id)
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
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[back_btn]]))
        elif category == "resources":
            res = db.get_resource_info(item_id)
            if not res:
                await query.edit_message_text("Ресурс не найден.")
                return
            mobs = db.get_resource_mobs(item_id)
            text = f"{res['emoji']} *{res['name']}*\n\n"
            if mobs:
                text += "*Падает с мобов:*\n" + "\n".join(f"{m['emoji']} {m['name']}" for m in mobs) + "\n"
            else:
                text += "Ни с кого не падает (возможно, крафтовый).\n"
            back_btn = InlineKeyboardButton("🔙 Назад", callback_data="main_menu")
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[back_btn]]))
        elif category == "gear":
            gear = db.get_gear_info(item_id)
            if not gear:
                await query.edit_message_text("Предмет не найден.")
                return
            mobs = db.get_gear_mobs(item_id) if gear["rarity"] == "common" else []
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
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[back_btn]]))

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    app.add_handler(CallbackQueryHandler(menu_callback))
    app.run_polling()

if __name__ == "__main__":
    main()
