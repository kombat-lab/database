import os
import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
    InputRichMessage
)

from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext

from database import db
from admin_handlers import admin_router
from utils import clean_username, escape_html
from analytics import (
    AnalyticsMiddleware,
    log_start, log_view_mob, log_view_resource, log_view_gear, log_view_card,
    log_search, log_inline_search
)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN not set")

ITEMS_PER_PAGE = 10
FETCH_EXTRA = 1
MAIN_MENU_BUTTONS = {"🐾 Мобы", "📦 Ресурсы", "⚔️ Снаряжение", "🔍 Поиск"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
inline_log_tasks = {}

bot = Bot(token=TOKEN)
dp = Dispatcher()

SLOT_ICONS = {
    'шлем': '🪖',
    'плечи': '🪹',
    'тело': '🦺',
    'плащ': '🧣',
    'пояс': '⛓',
    'штаны': '🩳',
    'ботинки': '🥾',
    'перчатки': '🧤',
    'кольцо': '💍',
    'амул': '📿',
    'серьга': '🧏‍♀️',
    'основная рука': '🗡',
    'вторая рука': '🛡'
}

# ---------- Формирование карточек ----------
async def format_mob_card_plain(mob_id: int) -> str:
    data = await db.get_mob_full_card(mob_id)
    if not data:
        return "Моб не найден."

    loc_str = f"{data['loc_emoji']} {escape_html(data['loc_name'])}"
    text = f"{data['emoji']} <b>{escape_html(data['name'])}</b>\n"
    text += f"❤️ HP: {data['hp']}\n✨ Пыль: {data['dust_min']}-{data['dust_max']}\n⭐ Опыт: {data['exp']}\n📍 Локация: {loc_str}\n\n"

    if data['resource_drops']:
        text += "<b>📦 Падает:</b>\n" + "\n".join(f"{r['emoji']} {escape_html(r['name'])}" for r in data['resource_drops']) + "\n"
    if data['gear_drops']:
        rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵"}
        text += "\n<b>⚔️ Снаряжение:</b>\n"
        for g in data['gear_drops']:
            rarity_icon = rarity_emoji.get(g.get('rarity', 'common'), '⚪')
            text += f"{rarity_icon} {g['emoji']} {escape_html(g['name'])}\n"
    if data['card_drops']:
        text += "\n<b>🃏 Карты:</b>\n"
        for c in data['card_drops']:
            slot_icon = SLOT_ICONS.get(c.get('slot', ''), '')
            text += f"{c['emoji']} {escape_html(c['name'])} {slot_icon}\n".strip() + "\n"
    return text

async def format_mob_card(mob_id: int) -> InputRichMessage:
    data = await db.get_mob_full_card(mob_id)
    if not data:
        return InputRichMessage(html="Моб не найден.")

    loc_str = f"{data['loc_emoji']} {escape_html(data['loc_name'])}"

    # Новая таблица 2×2
    table_html = f"""
    <table border="1" cellspacing="0" cellpadding="5">
        <tbody>
            <tr>
                <td><b>❤️ HP:</b> {data['hp']}</td>
                <td><b>⭐ Опыт:</b> {data['exp']}</td>
            </tr>
            <tr>
                <td><b>✨ Пыль:</b> {data['dust_min']}-{data['dust_max']}</td>
                <td><b>📍 Локация:</b> {loc_str}</td>
            </tr>
        </tbody>
    </table>
    """

    drops_html = ""
    if data['resource_drops']:
        drops_html += "<b>📦 Падает:</b><br>" + "<br>".join(
            f"{r['emoji']} {escape_html(r['name'])}" for r in data['resource_drops']
        ) + "<br>"
    if data['gear_drops']:
        rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵"}
        drops_html += "<br><b>⚔️ Снаряжение:</b><br>"
        for g in data['gear_drops']:
            rarity_icon = rarity_emoji.get(g.get('rarity', 'common'), '⚪')
            drops_html += f"{rarity_icon} {g['emoji']} {escape_html(g['name'])}<br>"
    if data['card_drops']:
        drops_html += "<br><b>🃏 Карты:</b><br>"
        for c in data['card_drops']:
            slot_icon = SLOT_ICONS.get(c.get('slot', ''), '')
            drops_html += f"{c['emoji']} {escape_html(c['name'])} {slot_icon}<br>"

    full_html = f"""
    <div><b>{data['emoji']} {escape_html(data['name'])}</b></div>
    {table_html}
    <div>{drops_html}</div>
    """
    return InputRichMessage(html=full_html.strip())

async def format_resource_card(resource_id: int) -> str:
    data = await db.get_resource_card(resource_id)
    if not data:
        return "Ресурс не найден."
    type_names = {
        'craft': '⚒️ Крафтовый',
        'consumable': '✨ Расходуемый',
        'scroll_recipe': '📜 Рецепт экипировки',
        'scroll': '📜 Рецепт экипировки',
        'currency': '💰 Валюта',
        'alchemy': '⚗️ Алхимия'
    }
    type_str = type_names.get(data.get('type', 'craft'), '📦 Крафтовый')
    is_alchemy = (data.get('type') == 'alchemy')
    
    text = f"{data['emoji']} <b>{escape_html(data['name'])}</b>\n"
    text += f"🏷 Тип: {type_str}\n"
    
    if not is_alchemy:
        text += "\n"
    
    if data['mobs']:
        text += "<b>Падает с мобов:</b>\n"
        for m in data['mobs']:
            loc_str = f"{m.get('location_emoji', '')} {escape_html(m.get('location_name', ''))}" if m.get('location_name') else ""
            text += f"{m['emoji']} {escape_html(m['name'])} <i>{loc_str}</i>\n"
        text += "\n"
    if data.get('note'):
        text += f"\n📝 <i>{escape_html(data['note'])}</i>\n"

    recipe = await db.get_recipe_for_resource(resource_id)
    if recipe and recipe['ingredients']:
        if not is_alchemy:
            text += "\n⚗️ <b>Алхимия:</b>\n"
        else:
            if not data['mobs'] and not data.get('note'):
                text += "\n"
        dust = None
        other = []
        for ing in recipe['ingredients']:
            if ing['resource_id'] == 71:
                dust = ing
            else:
                other.append(ing)
        if dust:
            text += f"✨ Пыль — {dust['quantity']} шт.\n"
        for ing in other:
            text += f"{ing['emoji']} {escape_html(ing['name'])} — {ing['quantity']} шт.\n"
        text += "\n🏛 <b>Где крафтить:</b>\n"
        text += "🏛 Город - 🛣 Вторая улица - 👤 Алхимик - ⚗️ Алхимия"

    return text

async def format_gear_card_rich(gear_id: int) -> InputRichMessage:
    data = await db.get_gear_card(gear_id)
    if not data:
        return InputRichMessage(html="Предмет не найден.")

    rarity_names = {
        "common": "Обычное",
        "rare": "Редкое",
        "epic": "Сверхредкое",
        "legendary": "Эпическая"
    }
    rarity_emoji = {
        "common": "⚪",
        "rare": "🟢",
        "epic": "🔵",
        "legendary": "🟣"
    }
    slot_names = {
        'шлем': '🪖 Шлем', 'плечи': '🪹 Плечи', 'тело': '🦺 Тело', 'плащ': '🧣 Плащ',
        'пояс': '⛓ Пояс', 'штаны': '🩳 Штаны', 'ботинки': '🥾 Ботинки', 'перчатки': '🧤 Перчатки',
        'кольцо': '💍 Кольцо', 'амул': '📿 Амулет', 'серьга': '🧏‍♀️ Серьга',
        'основная рука': '🗡 Основная рука', 'вторая рука': '🛡 Вторая рука'
    }
    rarity_text = f"{rarity_emoji[data['rarity']]} {rarity_names[data['rarity']]}"
    slot_text = slot_names.get(data['slot'], data['slot'])
    craft_text = "да" if data.get('craftable') else "нет"

    # Заголовок с именем и эмодзи
    html = f"<b>{data['emoji']} {escape_html(data['name'])}</b><br>"

    # Таблица 2×3 (заголовки и значения)
    html += """
    <table border="1" cellspacing="0" cellpadding="5">
        <tbody>
            <tr>
                <th><b>Редкость</b></th>
                <th><b>Слот</b></th>
                <th><b>Крафт</b></th>
            </tr>
            <tr>
                <td>{rarity}</td>
                <td>{slot}</td>
                <td>{craft}</td>
            </tr>
        </tbody>
    </table>
    """.format(rarity=rarity_text, slot=slot_text, craft=craft_text)

    # Блок с ресурсами (если крафтится)
    if data.get('craftable'):
        html += "<b>Требуемые ресурсы:</b><br>"
        if data['ingredients']:
            rows = ""
            for ing in data['ingredients']:
                ing_name = f"{ing['emoji']} {escape_html(ing['name'])}"
                rows += f"<tr><td>{ing_name}</td><td>{ing['quantity']} шт.</td></tr>"
            html += f"""
            <table border="1" cellspacing="0" cellpadding="5">
                <tbody>
                    {rows}
                </tbody>
            </table>
            """
        else:
            html += "<i>Рецепт не найден.</i>"

        if data['owners']:
            owners_list = "<br>".join(f"@{escape_html(clean_username(u))}" for u in data['owners'])
            html += f"<b>👥 Владельцы рецепта:</b><br>{owners_list}<br>"

    # Блок с мобами
    if data['mobs']:
        if data['rarity'] == 'epic':
            html += "<br><b>📜 Свиток падает с мобов:</b><br>"
        else:
            html += "<br><b>⚔️ Выпадает с мобов:</b><br>"
        mobs_list = "<br>".join(f"{m['emoji']} {escape_html(m['name'])}" for m in data['mobs'])
        html += mobs_list

    return InputRichMessage(html=html.strip())

async def ormat_gear_card_plain(gear_id: int) -> str:
    data = await db.get_gear_card(gear_id)
    if not data:
        return "Предмет не найден."
    rarity_names = {
        "common": "Обычное",
        "rare": "Редкое",
        "epic": "Сверхредкое",
        "legendary": "Эпическая"
    }
    rarity_emoji = {
        "common": "⚪",
        "rare": "🟢",
        "epic": "🔵",
        "legendary": "🟣"
    }
    slot_names = {
        'шлем': '🪖 Шлем', 'плечи': '🪹 Плечи', 'тело': '🦺 Тело', 'плащ': '🧣 Плащ',
        'пояс': '⛓ Пояс', 'штаны': '🩳 Штаны', 'ботинки': '🥾 Ботинки', 'перчатки': '🧤 Перчатки',
        'кольцо': '💍 Кольцо',
        'амул': '📿 Амулет',
        'серьга': '🧏‍♀️ Серьга',
        'основная рука': '🗡 Основная рука', 'вторая рука': '🛡 Вторая рука'
    }
    rarity_text = f"{rarity_emoji[data['rarity']]} {rarity_names[data['rarity']]}"
    slot_text = slot_names.get(data['slot'], data['slot'])
    text = f"{data['emoji']} <b>{escape_html(data['name'])}</b>\n"
    text += f"Редкость: {rarity_text}\nСлот: {slot_text}\n"
    
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

async def format_card_card(card_id: int) -> str:
    card = await db.get_card_by_id(card_id)
    if not card:
        return "Карта не найдена."
    slot_names = {
        'шлем': '🪖 Шлем', 'плечи': '🪹 Плечи', 'тело': '🦺 Тело', 'плащ': '🧣 Плащ',
        'пояс': '⛓ Пояс', 'штаны': '🩳 Штаны', 'ботинки': '🥾 Ботинки', 'перчатки': '🧤 Перчатки',
        'кольцо': '💍 Кольцо',
        'амул': '📿 Амулет',
        'серьга': '🧏‍♀️ Серьга',
        'основная рука': '🗡 Основная рука', 'вторая рука': '🛡 Вторая рука'
    }
    slot_text = slot_names.get(card['slot'], card['slot'])
    text = f"🃏 {card['emoji']} <b>{escape_html(card['name'])}</b>\n"
    text += f"Слот: {slot_text}\n\n"
    bonuses = []
    for i in range(1, 5):
        bonus = card.get(f'bonus{i}', '')
        if bonus:
            bonuses.append(bonus)
    if bonuses:
        text += "<b>Бонусы:</b>\n"
        for b in bonuses:
            text += f"   • {escape_html(b)}\n"
    if card.get('note'):
        text += f"\n📰 <i>{escape_html(card['note'])}</i>\n"

    mobs = await db.get_card_drop_mobs(card_id)
    if mobs:
        text += "\n<b>📜 Падает с мобов:</b>\n"
        for m in mobs:
            loc_str = f"{m['location_emoji']} {escape_html(m['location_name'])}" if m.get('location_name') else ""
            text += f"{m['emoji']} {escape_html(m['name'])} <i>{loc_str}</i>\n"
    else:
        text += "\n<i>Нет информации</i>"
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
        [InlineKeyboardButton(text="🔵 Сверхредкое", callback_data="list_gear_epic_1")],
        [InlineKeyboardButton(text="🟣 Эпическая", callback_data="list_gear_legendary_1")]
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
        callback_data = f"view_{category}_{item['id']}_{location_id}_{page}"
        keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_{category}_{location_id}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_{category}_{location_id}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к локациям", callback_data=f"back_to_locations_{category}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def get_gear_by_rarity_keyboard(rarity: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ITEMS_PER_PAGE
    items = await db.get_gear_by_rarity_sorted_by_slot(rarity, offset, ITEMS_PER_PAGE + FETCH_EXTRA)
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]
    keyboard = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        callback_data = f"view_gear_{item['id']}_{rarity}_{page}"
        keyboard.append([InlineKeyboardButton(text=name, callback_data=callback_data)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_gear_{rarity}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_gear_{rarity}_{page+1}"))
    if nav:
        keyboard.append(nav)
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
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"view_card_{card['id']}_{page}")])

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

    type_names = {
        'craft': 'Крафтовые',
        'consumable': 'Расходуемые',
        'scroll_recipe': 'Рецепты экипировки',
        'currency': 'Валюта',
        'alchemy': 'Алхимия'
    }
    type_display = type_names.get(resource_type, resource_type)

    keyboard = []
    for res in items:
        text = f"{res['emoji']} {res['name']}"
        callback_data = f"view_resource_{res['id']}_{resource_type}_{page}"
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
    await log_start(message.from_user.id)
    await message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())

@dp.message(Command("search"))
async def search_command(message: types.Message):
    await message.answer("🔎 Напиши название моба, ресурса, снаряжения или карты.")

@dp.message(F.text == "🐾 Мобы")
async def mobs_button(message: types.Message):
    await message.answer("Выбери локацию мобов:", reply_markup=await get_locations_keyboard("mobs"))

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
        rarity_emoji = {"common": "⚪", "rare": "🟢", "epic": "🔵", "legendary": "🟣"}
        for g in results["gear"]:
            reply += f"{g['emoji']} {escape_html(g['name'])} {rarity_emoji.get(g['rarity'], '')}\n"
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
        text = await format_mob_card_plain(mob["id"])
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
        text = await format_gear_card_plain(gear["id"])
        inline_results.append(InlineQueryResultArticle(
            id=f"gear_{gear['id']}",
            title=gear['name'],
            description=f"{gear['slot']} | {gear['rarity']}",
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML")
        ))

    for card in results.get("cards", [])[:50]:
        text = await format_card_card(card["id"])
        inline_results.append(InlineQueryResultArticle(
            id=f"card_{card['id']}",
            title=card['name'],
            description=f"Слот: {card['slot']}",
            input_message_content=InputTextMessageContent(message_text=text, parse_mode="HTML")
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
    rarity_names = {
        "common": "Обычное",
        "rare": "Редкое",
        "epic": "Сверхредкое",
        "legendary": "Эпическая"
    }
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

    await log_view_mob(callback.from_user.id, mob_id)

    # Создаём InputRichMessage
    rich_msg = await format_mob_card(mob_id)

    # Формируем клавиатуру
    neighbours = await db.get_prev_next_mob_by_hp(mob_id, location_id)
    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=f"view_mobs_{neighbours['prev_id']}_{location_id}_{page}"
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=f"view_mobs_{neighbours['next_id']}_{location_id}_{page}"
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

    # Отправляем сообщение
    await callback.message.delete()
    await callback.bot.send_rich_message(
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        reply_markup=reply_markup
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("view_resources_"))
async def view_resource(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    res_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])
    await log_view_resource(callback.from_user.id, res_id)
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
            text="◀️ Предыдущий",
            callback_data=f"view_resources_{prev_res[0]['id']}_{location_id}_{page}"
        ))
    if next_res:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
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

    await log_view_gear(callback.from_user.id, gear_id)

    rich_msg = await format_gear_card_rich(gear_id)

    recipe_id = await db.get_recipe_id_by_gear(gear_id)
    user_username = callback.from_user.username

    neighbours = await db.get_prev_next_gear_by_slot(gear_id, rarity)
    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=f"view_gear_{neighbours['prev_id']}_{rarity}_{page}"
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=f"view_gear_{neighbours['next_id']}_{rarity}_{page}"
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_gear_{rarity}_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)

    if rarity == 'epic' and recipe_id and user_username:
        owners = await db.get_recipe_owners(recipe_id)
        if user_username in owners:
            keyboard.append([InlineKeyboardButton(
                text="❌ У меня нет рецепта",
                callback_data=f"recipe_relinquish_{recipe_id}_{gear_id}_{rarity}_{page}"
            )])
        else:
            keyboard.append([InlineKeyboardButton(
                text="✅ У меня есть рецепт",
                callback_data=f"recipe_claim_{recipe_id}_{gear_id}_{rarity}_{page}"
            )])

    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await callback.message.delete()
    await callback.bot.send_rich_message(
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        reply_markup=reply_markup
    )
    await callback.answer()

# ---------- Карты ----------
@dp.callback_query(F.data.startswith("cards_page_"))
async def cards_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    await show_cards_list(callback, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_card_"))
async def view_card(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    card_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1
    await log_view_card(callback.from_user.id, card_id)
    text = await format_card_card(card_id)

    neighbours = await db.get_prev_next_card_by_slot(card_id)

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущая",
            callback_data=f"view_card_{neighbours['prev_id']}_{page}"
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующая ▶️",
            callback_data=f"view_card_{neighbours['next_id']}_{page}"
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"cards_page_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await callback.answer()

@dp.callback_query(F.data.startswith("recipe_claim_"))
async def recipe_claim(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    recipe_id = int(parts[2])
    gear_id = int(parts[3])
    rarity = parts[4]
    page = int(parts[5])
    username = callback.from_user.username

    if not username:
        await callback.answer("У тебя нет username. Установи его в настройках Telegram.", show_alert=True)
        return

    await db.add_recipe_owner(recipe_id, username)
    await update_gear_card(callback, gear_id, rarity, page)
    await callback.answer("✅ Ты добавлен в список владельцев рецепта!", show_alert=False)

@dp.callback_query(F.data.startswith("recipe_relinquish_"))
async def recipe_relinquish(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    recipe_id = int(parts[2])
    gear_id = int(parts[3])
    rarity = parts[4]
    page = int(parts[5])
    username = callback.from_user.username

    if not username:
        await callback.answer("Ошибка: username не найден.", show_alert=True)
        return

    await db.remove_recipe_owner(recipe_id, username)
    await update_gear_card(callback, gear_id, rarity, page)
    await callback.answer("❌ Ты удален из списка владельцев рецепта.", show_alert=False)

@dp.callback_query(F.data.startswith("recipe_relinquish_"))
async def recipe_relinquish(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    recipe_id = int(parts[2])
    gear_id = int(parts[3])
    rarity = parts[4]
    page = int(parts[5])
    username = callback.from_user.username

    if not username:
        await callback.answer("Ошибка: username не найден.", show_alert=True)
        return

    await db.remove_recipe_owner(recipe_id, username)

    await update_gear_card(callback, gear_id, rarity, page)
    await callback.answer("❌ Ты удален из списка владельцев рецепта.")

async def update_gear_card(callback: types.CallbackQuery, gear_id: int, rarity: str, page: int):
    rich_msg = await format_gear_card_rich(gear_id)
    recipe_id = await db.get_recipe_id_by_gear(gear_id)
    user_username = callback.from_user.username

    neighbours = await db.get_prev_next_gear_by_slot(gear_id, rarity)
    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=f"view_gear_{neighbours['prev_id']}_{rarity}_{page}"
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=f"view_gear_{neighbours['next_id']}_{rarity}_{page}"
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"list_gear_{rarity}_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)

    if rarity == 'epic' and recipe_id and user_username:
        owners = await db.get_recipe_owners(recipe_id)
        if user_username in owners:
            keyboard.append([InlineKeyboardButton(
                text="❌ У меня нет рецепта",
                callback_data=f"recipe_relinquish_{recipe_id}_{gear_id}_{rarity}_{page}"
            )])
        else:
            keyboard.append([InlineKeyboardButton(
                text="✅ У меня есть рецепт",
                callback_data=f"recipe_claim_{recipe_id}_{gear_id}_{rarity}_{page}"
            )])

    keyboard.append([back_button])
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await callback.message.delete()
    await callback.bot.send_rich_message(
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        reply_markup=reply_markup
    )

# ---------- Ресурсы по категориям ----------
@dp.callback_query(F.data.startswith("resource_cat_"))
async def resource_category_callback(callback: types.CallbackQuery):
    resource_type = callback.data.split("_")[2]
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
    await callback.message.edit_text("Выбери категорию ресурсов:", reply_markup=get_resource_categories_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("view_resource_"))
async def view_resource_by_type(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    resource_id = int(parts[2])
    resource_type = parts[3]
    page = int(parts[4])
    await log_view_resource(callback.from_user.id, resource_id)
    text = await format_resource_card(resource_id)

    neighbours = await db.get_prev_next_resource_by_type(resource_id, resource_type)

    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=f"view_resource_{neighbours['prev_id']}_{resource_type}_{page}"
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=f"view_resource_{neighbours['next_id']}_{resource_type}_{page}"
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=f"res_page_{resource_type}_{page}"
    )

    keyboard = []
    if nav_buttons:
        keyboard.append(nav_buttons)
    keyboard.append([back_button])

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await callback.answer()

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: types.CallbackQuery):
    await callback.message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())
    await callback.message.delete()
    await callback.answer()

# ---------- Запуск ----------
async def main():
    await db.connect()
    await db.init_analytics_tables()
    
    dp.update.middleware(AnalyticsMiddleware())
    dp.include_router(admin_router)
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())
