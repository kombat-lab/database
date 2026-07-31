import os
import logging
import asyncio
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineQuery, InlineQueryResultArticle, InputRichMessage,
    InputRichMessageContent, FSInputFile
)
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

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
BOT_USERNAME = None

RARITY_EMOJIS = {
    "common": "⚪",
    "rare": "🟢",
    "epic": "🔵",
    "legendary": "🟣",
}

RARITY_NAMES = {
    "common": "Обычное",
    "rare": "Редкое",
    "epic": "Сверхредкое",
    "legendary": "Эпическое",
}

RESOURCE_TYPE_NAMES = {
    "craft": "⚒️ Крафтовый",
    "consumable": "✨ Расходуемый",
    "scroll_recipe": "📜 Рецепт экипировки",
    "scroll": "📜 Рецепт экипировки",
    "currency": "💰 Валюта",
    "alchemy": "⚗️ Алхимия",
}

RESOURCE_TYPE_PLURAL_NAMES = {
    "craft": "⚒️ Крафтовые",
    "consumable": "✨ Расходуемые",
    "scroll_recipe": "📜 Рецепты экипировки",
    "scroll": "📜 Рецепты экипировки",
    "currency": "💰 Валюта",
    "alchemy": "⚗️ Алхимия",
}

RESOURCE_TYPE_TITLES = {
    "craft": "Крафтовые",
    "consumable": "Расходуемые",
    "scroll_recipe": "Рецепты экипировки",
    "scroll": "Рецепты экипировки",
    "currency": "Валюта",
    "alchemy": "Алхимия",
}

RARITY_ORDER = ("common", "rare", "epic", "legendary")


def get_rarity_emoji(rarity: str | None) -> str:
    return RARITY_EMOJIS.get(rarity or "common", RARITY_EMOJIS["common"])


def get_resource_type_name(resource_type: str | None) -> str:
    return RESOURCE_TYPE_NAMES.get(resource_type or "craft", "📦 Крафтовый")


def build_resource_return_param(
    resource_id: int,
    context_type: str | None,
    context_id: int | str | None,
    page: int,
) -> str | None:
    if context_id is None:
        return None
    if context_type == "location":
        return f"resource_loc_{resource_id}_{context_id}_{page}"
    if context_type == "type":
        return f"resource_type_{resource_id}_{context_id}_{page}"
    return None


def build_gear_return_param(gear_id: int, rarity: str | None, page: int) -> str | None:
    return f"gear_{gear_id}_{rarity}_{page}" if rarity else None

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


def get_location_emoji(location: dict) -> str:
    """Возвращает emoji локации с безопасным fallback на значение из БД."""
    return LOCATION_EMOJI_OVERRIDES.get(location["id"], location.get("emoji") or "📍")


def get_location_button_text(location: dict) -> str:
    return f"{get_location_emoji(location)} {location['name']}"
def make_deep_link(item_type: str, item_id: int, return_param: str = None) -> str:
    """Формирует корректный Telegram start payload длиной до 64 символов."""
    payload = f"{item_type}_{item_id}"
    if return_param:
        candidate = f"{payload}-r-{return_param}"
        if len(candidate) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            payload = candidate
    return f"https://t.me/{BOT_USERNAME}?start={payload}"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
inline_log_tasks = {}

bot = Bot(token=TOKEN)
dp = Dispatcher()

SLOT_DEFINITIONS = (
    ("шлем", "🪖", "Шлем"),
    ("плечи", "🪹", "Плечи"),
    ("тело", "🦺", "Тело"),
    ("плащ", "🧣", "Плащ"),
    ("пояс", "⛓", "Пояс"),
    ("штаны", "🩳", "Штаны"),
    ("ботинки", "🥾", "Ботинки"),
    ("перчатки", "🧤", "Перчатки"),
    ("кольцо", "💍", "Кольцо"),
    ("амул", "📿", "Амулет"),
    ("серьга", "🧏‍♀️", "Серьга"),
    ("основная рука", "🗡", "Основная рука"),
    ("вторая рука", "🛡", "Вторая рука"),
)

SLOT_NAMES = {key: f"{icon} {name}" for key, icon, name in SLOT_DEFINITIONS}
SLOT_ICONS = {key: icon for key, icon, _ in SLOT_DEFINITIONS}
GEAR_SLOT_ORDER = [key for key, _, _ in SLOT_DEFINITIONS]

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

GEAR_CLASSES = {"Аколит", "Бастион", "Маг", "Охотник", "Тень"}

def format_gear_classes(classes_value) -> str:
    """Сокращает полный набор классов до надписи «Все классы»."""
    selected = {
        class_name.strip()
        for class_name in str(classes_value or "").split(",")
        if class_name.strip()
    }
    if not selected or selected == GEAR_CLASSES:
        return "Все классы"
    return ", ".join(
        class_name
        for class_name in ("Аколит", "Бастион", "Маг", "Охотник", "Тень")
        if class_name in selected
    )


# ---------- Формирование карточек ----------
async def format_mob_card_plain(mob_id: int, location_id: int = None, page: int = 1) -> str:
    data = await db.get_mob_full_card(mob_id)
    if not data:
        return "Моб не найден."

    loc_str = f"{escape_html(data['loc_emoji'])} {escape_html(data['loc_name'])}"
    text = f"{escape_html(data['emoji'])} <b>{escape_html(data['name'])}</b>\n"
    text += f"❤️ HP: {data['hp']}\n✨ Пыль: {data['dust_min']}-{data['dust_max']}\n⭐ Опыт: {data['exp']}\n📍 Локация: {loc_str}\n\n"

    # return для возврата к этому мобу
    return_param = f"mob_{mob_id}_{location_id}_{page}" if location_id else None

    if data['resource_drops']:
        text += "<b>📦 Падает:</b>\n"
        for r in data['resource_drops']:
            link = make_deep_link("resource", r['id'], return_param)
            text += f"{escape_html(r['emoji'])} <a href='{link}'>{escape_html(r['name'])}</a>\n"
        text += "\n"

    if data['gear_drops']:
        text += "<b>⚔️ Снаряжение:</b>\n"
        for g in data['gear_drops']:
            rarity_icon = get_rarity_emoji(g.get('rarity'))
            link = make_deep_link("gear", g['id'], return_param)
            text += f"{rarity_icon} {escape_html(g['emoji'])} <a href='{link}'>{escape_html(g['name'])}</a>\n"
        text += "\n"

    if data['card_drops']:
        text += "<b>🃏 Карты:</b>\n"
        for c in data['card_drops']:
            slot_icon = SLOT_ICONS.get(c.get('slot', ''), '')
            link = make_deep_link("card", c['id'], return_param)
            text += f"{escape_html(c['emoji'])} <a href='{link}'>{escape_html(c['name'])}</a> {slot_icon}\n"
        text += "\n"

    return text


async def format_mob_card(mob_id: int, location_id: int = None, page: int = 1) -> InputRichMessage:
    data = await db.get_mob_full_card(mob_id)
    if not data:
        return InputRichMessage(html="Моб не найден.")

    loc_str = f"{escape_html(data['loc_emoji'])} {escape_html(data['loc_name'])}"
    return_param = f"mob_{mob_id}_{location_id}_{page}" if location_id else None

    # Таблица 2×2
    table_html = f"""
    <table border="1" cellspacing="0" cellpadding="5">
        <tbody>
            <tr>
                <td><b>❤️ HP:</b> {data['hp']}</td>
                <td><b>⭐ Опыт:</b> {data['exp']}</td>
            </tr>
            <tr>
                <td><b>✨ Пыль:</b> {data['dust_min']}-{data['dust_max']}</td>
                <td><b>{loc_str}</b></td>
            </tr>
        </tbody>
    </table>
    """

    drops_html = ""
    if data['resource_drops']:
        drops_html += "<b>📦 Падает:</b><br>"
        for r in data['resource_drops']:
            link = make_deep_link("resource", r['id'], return_param)
            drops_html += f"{escape_html(r['emoji'])} <a href='{link}'>{escape_html(r['name'])}</a><br>"
        drops_html += "<br>"

    if data['gear_drops']:
        drops_html += "<b>⚔️ Снаряжение:</b><br>"
        for g in data['gear_drops']:
            rarity_icon = get_rarity_emoji(g.get('rarity'))
            link = make_deep_link("gear", g['id'], return_param)
            drops_html += f"{rarity_icon} {escape_html(g['emoji'])} <a href='{link}'>{escape_html(g['name'])}</a><br>"
        drops_html += "<br>"

    if data['card_drops']:
        drops_html += "<b>🃏 Карты:</b><br>"
        for c in data['card_drops']:
            slot_icon = SLOT_ICONS.get(c.get('slot', ''), '')
            link = make_deep_link("card", c['id'], return_param)
            drops_html += f"{escape_html(c['emoji'])} <a href='{link}'>{escape_html(c['name'])}</a> {slot_icon}<br>"
        drops_html += "<br>"

    full_html = f"""
    <div><b>{escape_html(data['emoji'])} {escape_html(data['name'])}</b></div>
    {table_html}
    <div>{drops_html}</div>
    """
    return InputRichMessage(html=full_html.strip())

async def format_resource_card(resource_id: int, context_type: str = None, context_id: int = None, page: int = 1) -> str:
    data = await db.get_resource_card(resource_id)
    if not data:
        return "Ресурс не найден."

    type_str = get_resource_type_name(data.get('type'))
    is_alchemy = (data.get('type') == 'alchemy')

    text = f"{escape_html(data['emoji'])} <b>{escape_html(data['name'])}</b>\n"
    text += f"🏷 Тип: {type_str}\n"
    if not is_alchemy:
        text += "\n"

    # return для возврата к этому ресурсу
    return_param = build_resource_return_param(resource_id, context_type, context_id, page)

    if data['mobs']:
        text += "<b>Падает с мобов:</b>\n"
        for m in data['mobs']:
            loc_str = f"{escape_html(m.get('location_emoji', ''))} {escape_html(m.get('location_name', ''))}" if m.get('location_name') else ""
            link = make_deep_link("mob", m['id'], return_param)
            text += f"{escape_html(m['emoji'])} <a href='{link}'>{escape_html(m['name'])}</a> <i>{loc_str}</i>\n"
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
            link = make_deep_link("resource", dust['resource_id'], return_param)
            text += f"✨ <a href='{link}'>Пыль</a> — {dust['quantity']} шт.\n"
        for ing in other:
            link = make_deep_link("resource", ing['resource_id'], return_param)
            text += f"{escape_html(ing['emoji'])} <a href='{link}'>{escape_html(ing['name'])}</a> — {ing['quantity']} шт.\n"

        text += "\n🏛 <b>Где крафтить:</b>\n"
        text += "🏛 Город - 🛣 Вторая улица - 👤 Алхимик - ⚗️ Алхимия"

    return text


async def format_resource_card_rich(resource_id: int, context_type: str = None, context_id: int = None, page: int = 1) -> InputRichMessage:
    data = await db.get_resource_card(resource_id)
    if not data:
        return InputRichMessage(html="Ресурс не найден.")

    type_str = get_resource_type_name(data.get('type'))
    is_alchemy = (data.get('type') == 'alchemy')

    html = f"<b>{escape_html(data['emoji'])} {escape_html(data['name'])}</b><br>"
    html += f"🏷 Тип: {type_str}<br>"

    # Параметр для возврата к текущему ресурсу
    return_param = build_resource_return_param(resource_id, context_type, context_id, page)

    # ----- ТАБЛИЦА С МОБАМИ (добавлено) -----
    if data['mobs']:
        html += "<br><b>Падает с мобов:</b><br>"
        rows = ""
        for m in data['mobs']:
            loc_str = f"{escape_html(m.get('location_emoji', ''))} {escape_html(m.get('location_name', ''))}" if m.get('location_name') else ""
            link = make_deep_link("mob", m['id'], return_param)
            mob_name = f"{escape_html(m['emoji'])} <a href='{link}'>{escape_html(m['name'])}</a>"
            rows += f"<tr><td>{mob_name}</td><td>{loc_str}</td></tr>"
        html += f"""
        <table border="1" cellspacing="0" cellpadding="5">
            <tbody>
                <tr><th>Моб</th><th>Локация</th></tr>
                {rows}
            </tbody>
        </table>
        """

    if data.get('note'):
        html += f"<br>📝 <i>{escape_html(data['note'])}</i><br>"

    # ----- АЛХИМИЯ / РЕЦЕПТ (таблица ингредиентов) -----
    recipe = await db.get_recipe_for_resource(resource_id)
    if recipe and recipe['ingredients']:
        if not is_alchemy:
            html += "<br>⚗️ <b>Алхимия:</b><br>"

        dust = None
        other = []
        for ing in recipe['ingredients']:
            if ing['resource_id'] == 71:
                dust = ing
            else:
                other.append(ing)

        rows = ""
        if dust:
            link = make_deep_link("resource", dust['resource_id'], return_param)
            rows += f"<tr><td>✨ <a href='{link}'>Пыль</a></td><td>{dust['quantity']} шт.</td></tr>"
        for ing in other:
            link = make_deep_link("resource", ing['resource_id'], return_param)
            rows += f"<tr><td>{escape_html(ing['emoji'])} <a href='{link}'>{escape_html(ing['name'])}</a></td><td>{ing['quantity']} шт.</td></tr>"

        html += f"""
        <table border="1" cellspacing="0" cellpadding="5">
            <tbody>
                <tr><th>Ресурс</th><th>Количество</th></tr>
                {rows}
            </tbody>
        </table>
        """
        html += "<br>🏛 <b>Где крафтить:</b><br>"
        html += "🏛 Город - 🛣 Вторая улица - 👤 Алхимик - ⚗️ Алхимия"

    return InputRichMessage(html=html.strip())

async def format_gear_card_plain(gear_id: int, rarity: str = None, page: int = 1) -> str:
    data = await db.get_gear_card(gear_id)
    if not data:
        return "Предмет не найден."

    text = (
        f"{get_rarity_emoji(data.get('rarity'))} "
        f"{escape_html(data['emoji'])} <b>{escape_html(data['name'])}</b>\n"
    )
    text += f"Уровень: {data.get('level', 1)}\n"
    text += f"Класс: {escape_html(format_gear_classes(data.get('classes')))}\n"
    if data.get('note'):
        text += f"\n📝 {escape_html(data['note'])}\n"

    # return для возврата к этому снаряжению (используется при клике на моба или ресурс)
    return_param = build_gear_return_param(gear_id, rarity, page)

    if data.get('craftable') and data['ingredients']:
        text += "Крафт: да\n\n<b>Требуемые ресурсы:</b>\n"
        dust = None
        other = []
        for ing in data['ingredients']:
            if ing['id'] == 71:
                dust = ing
            else:
                other.append(ing)
        if dust:
            link = make_deep_link("resource", dust['id'], return_param)
            text += f"✨ <a href='{link}'>Пыль</a> — {dust['quantity']}\n"
        for ing in other:
            link = make_deep_link("resource", ing['id'], return_param)
            text += f"{escape_html(ing['emoji'])} <a href='{link}'>{escape_html(ing['name'])}</a> — {ing['quantity']} шт.\n"
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
        for m in data['mobs']:
            link = make_deep_link("mob", m['id'], return_param)
            text += f"{escape_html(m['emoji'])} <a href='{link}'>{escape_html(m['name'])}</a>\n"

    return text


async def format_gear_card_rich(gear_id: int, rarity: str = None, page: int = 1) -> InputRichMessage:
    data = await db.get_gear_card(gear_id)
    if not data:
        return InputRichMessage(html="Предмет не найден.")

    craft_text = "да" if data.get('craftable') else "нет"

    html = (
        f"<b>{get_rarity_emoji(data.get('rarity'))} "
        f"{escape_html(data['emoji'])} {escape_html(data['name'])}</b><br>"
    )
    html += f"""
    <table border="1" cellspacing="0" cellpadding="5">
        <tbody>
            <tr>
                <th align="center">Уровень</th>
                <th align="center">Класс</th>
                <th align="center">Крафт</th>
            </tr>
            <tr>
                <td align="center">{data.get('level', 1)}</td>
                <td align="center">{escape_html(format_gear_classes(data.get('classes')))}</td>
                <td align="center">{craft_text}</td>
            </tr>
        </tbody>
    </table>
    """
    if data.get('note'):
        html += f"<br>📝 <b>Примечание:</b> {escape_html(data['note'])}<br>"

    return_param = build_gear_return_param(gear_id, rarity, page)

    if data.get('craftable') and data['ingredients']:
        html += "<b>Требуемые ресурсы:</b><br>"
        rows = ""
        for ing in data['ingredients']:
            ing_name = f"{escape_html(ing['emoji'])} {escape_html(ing['name'])}"
            link = make_deep_link("resource", ing['id'], return_param)
            ing_link = f'<a href="{link}">{ing_name}</a>'
            rows += f"<tr><td>{ing_link}</td><td>{ing['quantity']} шт.</td></tr>"
        html += f"""
        <table border="1" cellspacing="0" cellpadding="5">
            <tbody>{rows}</tbody>
        </table>
        """
        if data.get('owners'):
            owners_list = "<br>".join(f"@{escape_html(clean_username(u))}" for u in data['owners'])
            html += f"""
            <details>
                <summary>👥 Владельцы рецепта</summary>
                {owners_list}
            </details>
            """

    if data['mobs']:
        if data['rarity'] == 'epic':
            html += "<br><b>📜 Свиток падает с мобов:</b><br>"
        else:
            html += "<br><b>⚔️ Выпадает с мобов:</b><br>"
        mobs_list = []
        for m in data['mobs']:
            link = make_deep_link("mob", m['id'], return_param)
            mobs_list.append(f"{escape_html(m['emoji'])} <a href='{link}'>{escape_html(m['name'])}</a>")
        html += "<br>".join(mobs_list)

    return InputRichMessage(html=html.strip())

async def format_card_card(card_id: int, page: int = 1, context_type: str = None, context_id: int = None) -> str:
    card = await db.get_card_by_id(card_id)
    if not card:
        return "Карта не найдена."

    slot_text = SLOT_NAMES.get(card['slot'], card['slot'])

    return_param = None
    if context_type and context_id:
        if context_type == 'location':
            return_param = f"card_loc_{card_id}_{context_id}_{page}"
        elif context_type == 'type':
            return_param = f"card_type_{card_id}_{context_id}_{page}"

    text = f"🃏 {escape_html(card['emoji'])} <b>{escape_html(card['name'])}</b>\n"
    text += f"Слот: {escape_html(slot_text)}\n\n"

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
            loc_str = f"{escape_html(m['location_emoji'])} {escape_html(m['location_name'])}" if m.get('location_name') else ""
            link = make_deep_link("mob", m['id'], return_param)
            text += f"{escape_html(m['emoji'])} <a href='{link}'>{escape_html(m['name'])}</a> <i>{loc_str}</i>\n"
    else:
        text += "\n<i>Нет информации</i>"

    return text


async def format_card_card_rich(card_id: int, page: int = 1, context_type: str = None,
                                context_id: int = None) -> InputRichMessage:
    """Формирует Rich-карточку карты, используя проверенный HTML fallback."""
    plain = await format_card_card(card_id, page, context_type, context_id)
    return InputRichMessage(html=plain.replace("\n", "<br>"))


async def upsert_rich_card(*, bot: Bot, chat_id: int, rich_message: InputRichMessage,
                           plain_text: str, reply_markup: InlineKeyboardMarkup = None,
                           current_message: types.Message = None) -> types.Message:
    """Редактирует карточку на месте и безопасно откатывается к обычному HTML."""
    if current_message:
        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=current_message.message_id,
                rich_message=rich_message,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as error:
            if isinstance(error, TelegramBadRequest) and "message is not modified" in str(error).lower():
                return current_message
            logger.info("Rich Message edit failed, using HTML fallback: %s", error)

        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=current_message.message_id,
                text=plain_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as error:
            logger.info("HTML edit failed, sending a replacement: %s", error)

    try:
        sent = await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=rich_message,
            reply_markup=reply_markup,
        )
    except TelegramAPIError as error:
        logger.warning("Rich Message send failed, using HTML fallback: %s", error)
        sent = await bot.send_message(
            chat_id=chat_id,
            text=plain_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    if current_message:
        try:
            await current_message.delete()
        except TelegramAPIError:
            logger.debug("Old card could not be deleted", exc_info=True)
    return sent

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
        callback_data = f"view_{category}_{item['id']}_{location_id}_{page}"
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
    items = await db.execute_query(
        "SELECT * FROM gear WHERE rarity = ? AND slot = ? ORDER BY level, name LIMIT ? OFFSET ?",
        (rarity, slot, ITEMS_PER_PAGE + FETCH_EXTRA, offset),
    )
    has_next = len(items) > ITEMS_PER_PAGE
    items = items[:ITEMS_PER_PAGE]
    keyboard = []
    for item in items:
        name = f"{item.get('emoji', '')} {item['name']}"
        keyboard.append([InlineKeyboardButton(
            text=name,
            callback_data=f"view_gear_{item['id']}_{rarity}_{slot_index}_{page}",
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

    type_display = RESOURCE_TYPE_TITLES.get(resource_type, resource_type)

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
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        payload, separator, return_param = args[1].partition("-r-")
        target_type = None
        target_id = None
        return_param = return_param if separator else None

        parts = payload.split("_")
        if len(parts) == 2:
            target_type = parts[0]
            try:
                target_id = int(parts[1])
            except ValueError:
                pass

        if target_type and target_id is not None:
            # Определяем контекст для возврата (если есть)
            context_type = None
            context_id = None
            page = 1
            rarity = None
            if return_param:
                parts = return_param.split("_")
                if len(parts) >= 3:
                    obj_type = parts[0]
                    if obj_type == "gear" and len(parts) == 4:
                        context_type = "gear"
                        context_id = parts[1]   # gear_id
                        page = int(parts[3])
                    elif obj_type == "mob" and len(parts) == 4:
                        context_type = "mob"
                        context_id = parts[1]   # mob_id
                        page = int(parts[3])
                    elif obj_type == "resource_loc" and len(parts) == 4:
                        context_type = "location"
                        context_id = int(parts[2])  # location_id
                        page = int(parts[3])
                    elif obj_type == "resource_type" and len(parts) == 4:
                        context_type = "type"
                        context_id = parts[2]   # resource_type
                        page = int(parts[3])
                    # можно добавить card и другие

            # Формируем карточку в зависимости от типа
            if target_type == "resource":
                rich_msg = await format_resource_card_rich(
                    target_id,
                    context_type=context_type,
                    context_id=context_id,
                    page=page
                )
            elif target_type == "mob":
                # Для мобов используем rich-формат (если нужно передать location_id и page)
                # В формате mob_{id} мы не знаем location_id, поэтому передаём None
                rich_msg = await format_mob_card(target_id, location_id=None, page=page)
            elif target_type == "gear":
                # Для снаряжения нужны rarity и page (если они есть в return, иначе используем дефолтные)
                # Но мы можем получить rarity из return_param, если он есть
                if context_type == "gear" and context_id:
                    # context_id содержит gear_id, но мы уже знаем target_id = gear_id
                    # Нам нужна rarity. Попробуем извлечь из return_param
                    if return_param:
                        parts = return_param.split("_")
                        if len(parts) == 4 and parts[0] == "gear":
                            rarity = parts[2]
                            page = int(parts[3])
                            rich_msg = await format_gear_card_rich(target_id, rarity, page)
                        else:
                            rich_msg = await format_gear_card_rich(target_id, None, 1)
                    else:
                        rich_msg = await format_gear_card_rich(target_id, None, 1)
                else:
                    rich_msg = await format_gear_card_rich(target_id, None, 1)
            elif target_type == "card":
                rich_msg = await format_card_card_rich(
                    target_id,
                    page=page,
                    context_type=context_type,
                    context_id=context_id
                )
            else:
                await message.answer("Неизвестный тип объекта.")
                return

            # Строим кнопку возврата на основе return_param (если есть)
            keyboard = None
            if return_param:
                parts = return_param.split("_")
                if len(parts) >= 3:
                    obj_type = parts[0]
                    if obj_type == "gear" and len(parts) == 4:
                        try:
                            gear_id = int(parts[1])
                            rarity = parts[2]
                            page = int(parts[3])
                            back_button = InlineKeyboardButton(
                                text="🔙 Вернуться к снаряжению",
                                callback_data=f"view_gear_{gear_id}_{rarity}_{page}"
                            )
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[back_button]])
                        except (IndexError, ValueError):
                            pass
                    elif obj_type == "mob" and len(parts) == 4:
                        try:
                            mob_id = int(parts[1])
                            location_id = int(parts[2])
                            page = int(parts[3])
                            back_button = InlineKeyboardButton(
                                text="🔙 Вернуться к мобу",
                                callback_data=f"view_mobs_{mob_id}_{location_id}_{page}"
                            )
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[back_button]])
                        except (IndexError, ValueError):
                            pass
                    elif obj_type == "resource_loc" and len(parts) == 4:
                        try:
                            res_id = int(parts[1])
                            location_id = int(parts[2])
                            page = int(parts[3])
                            back_button = InlineKeyboardButton(
                                text="🔙 Вернуться к ресурсу",
                                callback_data=f"view_resources_{res_id}_{location_id}_{page}"
                            )
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[back_button]])
                        except (IndexError, ValueError):
                            pass
                    elif obj_type == "resource_type" and len(parts) == 4:
                        try:
                            res_id = int(parts[1])
                            res_type = parts[2]
                            page = int(parts[3])
                            back_button = InlineKeyboardButton(
                                text="🔙 Вернуться к ресурсу",
                                callback_data=f"view_resource_{res_id}_{res_type}_{page}"
                            )
                            keyboard = InlineKeyboardMarkup(inline_keyboard=[[back_button]])
                        except (IndexError, ValueError):
                            pass

            plain_formatters = {
                "resource": lambda: format_resource_card(target_id, context_type, context_id, page),
                "mob": lambda: format_mob_card_plain(target_id, page=page),
                "gear": lambda: format_gear_card_plain(target_id, rarity, page),
                "card": lambda: format_card_card(target_id, page, context_type, context_id),
            }
            plain_text = await plain_formatters[target_type]()
            await upsert_rich_card(
                bot=message.bot,
                chat_id=message.chat.id,
                rich_message=rich_msg,
                plain_text=plain_text,
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
        rich_message = await format_mob_card(mob["id"])
        desc = f"❤️ HP: {mob['hp']} | ✨ Пыль: {mob['dust_min']}-{mob['dust_max']} | ⭐ Опыт: {mob['exp']}"
        inline_results.append(InlineQueryResultArticle(
            id=f"mob_{mob['id']}",
            title=mob['name'],
            description=desc,
            input_message_content=InputRichMessageContent(rich_message=rich_message)
        ))

    for res in results.get("resources", [])[:50]:
        if len(inline_results) >= 50:
            break
        rich_message = await format_resource_card_rich(res["id"])
        inline_results.append(InlineQueryResultArticle(
            id=f"res_{res['id']}",
            title=res['name'],
            description="Ресурс",
            input_message_content=InputRichMessageContent(rich_message=rich_message)
        ))

    for gear in results.get("gear", [])[:50]:
        if len(inline_results) >= 50:
            break
        rich_message = await format_gear_card_rich(gear["id"], gear.get("rarity"))
        inline_results.append(InlineQueryResultArticle(
            id=f"gear_{gear['id']}",
            title=gear['name'],
            description=f"{gear['slot']} | {gear['rarity']}",
            input_message_content=InputRichMessageContent(rich_message=rich_message)
        ))

    for card in results.get("cards", [])[:50]:
        if len(inline_results) >= 50:
            break
        rich_message = await format_card_card_rich(card["id"])
        inline_results.append(InlineQueryResultArticle(
            id=f"card_{card['id']}",
            title=card['name'],
            description=f"Слот: {card['slot']}",
            input_message_content=InputRichMessageContent(rich_message=rich_message)
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
    title = f"{get_location_emoji(location)} {location['name']} - {category.capitalize()}\nСтраница {page}"
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
    await callback.answer()
    parts = callback.data.split("_")
    mob_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])

    await log_view_mob(callback.from_user.id, mob_id)

    # Создаём InputRichMessage
    rich_msg = await format_mob_card(mob_id, location_id, page)
    plain_text = await format_mob_card_plain(mob_id, location_id, page)

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

    await upsert_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        plain_text=plain_text,
        reply_markup=reply_markup,
        current_message=callback.message,
    )

@dp.callback_query(F.data.startswith("view_resources_"))
async def view_resource(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    res_id = int(parts[2])
    location_id = int(parts[3])
    page = int(parts[4])
    await log_view_resource(callback.from_user.id, res_id)

    rich_msg = await format_resource_card_rich(res_id, context_type='location', context_id=location_id, page=page)
    plain_text = await format_resource_card(res_id, context_type='location', context_id=location_id, page=page)

    # Навигация по ресурсам в локации
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
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await upsert_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        plain_text=plain_text,
        reply_markup=reply_markup,
        current_message=callback.message,
    )

@dp.callback_query(F.data.startswith("view_gear_"))
async def view_gear(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    gear_id = int(parts[2])
    rarity = parts[3]
    slot_index = int(parts[4]) if len(parts) >= 6 else None
    page = int(parts[5]) if len(parts) >= 6 else int(parts[4])

    await log_view_gear(callback.from_user.id, gear_id)

    # Передаём rarity и page в функцию
    rich_msg = await format_gear_card_rich(gear_id, rarity, page)
    plain_text = await format_gear_card_plain(gear_id, rarity, page)

    # остальной код без изменений (кнопки и т.д.)
    recipe_id = await db.get_recipe_id_by_gear(gear_id)
    user_username = callback.from_user.username

    if slot_index is not None:
        slot = GEAR_SLOT_ORDER[slot_index]
        ids = await db.execute_query("SELECT id FROM gear WHERE rarity = ? AND slot = ? ORDER BY level, name, id", (rarity, slot))
        ordered = [row['id'] for row in ids]
        pos = ordered.index(gear_id) if gear_id in ordered else -1
        neighbours = {
            'prev_id': ordered[pos-1] if pos > 0 else None,
            'next_id': ordered[pos+1] if 0 <= pos < len(ordered)-1 else None,
        }
    else:
        neighbours = await db.get_prev_next_gear_by_slot(gear_id, rarity)
    nav_buttons = []
    if neighbours['prev_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="◀️ Предыдущий",
            callback_data=(f"view_gear_{neighbours['prev_id']}_{rarity}_{slot_index}_{page}" if slot_index is not None else f"view_gear_{neighbours['prev_id']}_{rarity}_{page}")
        ))
    if neighbours['next_id']:
        nav_buttons.append(InlineKeyboardButton(
            text="Следующий ▶️",
            callback_data=(f"view_gear_{neighbours['next_id']}_{rarity}_{slot_index}_{page}" if slot_index is not None else f"view_gear_{neighbours['next_id']}_{rarity}_{page}")
        ))

    back_button = InlineKeyboardButton(
        text="🔙 Назад к списку",
        callback_data=(f"page_gear_{rarity}_{slot_index}_{page}" if slot_index is not None else "gear_rarities")
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

    await upsert_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        plain_text=plain_text,
        reply_markup=reply_markup,
        current_message=callback.message,
    )

# ---------- Карты ----------
@dp.callback_query(F.data.startswith("cards_page_"))
async def cards_page_callback(callback: types.CallbackQuery):
    page = int(callback.data.split("_")[2])
    await show_cards_list(callback, page)
    await callback.answer()

@dp.callback_query(F.data.startswith("view_card_"))
async def view_card(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    card_id = int(parts[2])
    page = int(parts[3]) if len(parts) > 3 else 1
    await log_view_card(callback.from_user.id, card_id)
    text = await format_card_card(card_id)
    rich_msg = await format_card_card_rich(card_id)

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

    await upsert_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        plain_text=text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
        current_message=callback.message,
    )

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

async def update_gear_card(callback: types.CallbackQuery, gear_id: int, rarity: str, page: int):
    rich_msg = await format_gear_card_rich(gear_id, rarity, page)
    plain_text = await format_gear_card_plain(gear_id, rarity, page)
    # остальной код без изменений (он уже использует rarity и page)
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

    await upsert_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        plain_text=plain_text,
        reply_markup=reply_markup,
        current_message=callback.message,
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
    await callback.answer()
    parts = callback.data.split("_")
    resource_id = int(parts[2])
    resource_type = parts[3]
    page = int(parts[4])
    await log_view_resource(callback.from_user.id, resource_id)

    rich_msg = await format_resource_card_rich(resource_id, context_type='type', context_id=resource_type, page=page)
    plain_text = await format_resource_card(resource_id, context_type='type', context_id=resource_type, page=page)

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
    reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    await upsert_rich_card(
        bot=callback.bot,
        chat_id=callback.message.chat.id,
        rich_message=rich_msg,
        plain_text=plain_text,
        reply_markup=reply_markup,
        current_message=callback.message,
    )

@dp.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu(callback: types.CallbackQuery):
    await callback.message.answer("📋 Главное меню", reply_markup=get_main_menu_reply_keyboard())
    await callback.message.delete()
    await callback.answer()

# ---------- Запуск ----------
async def main():
    await db.connect()
    await db.init_analytics_tables()

    global BOT_USERNAME
    me = await bot.me()
    BOT_USERNAME = me.username
    
    dp.update.middleware(AnalyticsMiddleware())
    dp.include_router(admin_router)
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(main())
