import os
import logging
from aiogram import BaseMiddleware, Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError

from database import db
from utils import is_valid_emoji, clean_username, escape_html
from admin_utils import (
    ADMIN_ITEMS_PER_PAGE,
    get_admin_main_keyboard,
    admin_close,
    admin_cancel_edit,
    render_entity_list,
    show_edit_menu,
    edit_admin_rich,
    register_generic_handlers,
    GenericEditStates,
)
from stats_handlers import stats_router

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

GEAR_SLOTS = [
    'шлем', 'плечи', 'тело', 'плащ', 'пояс', 'штаны', 'ботинки', 'перчатки',
    'кольцо', 'амул', 'серьга', 'основная рука', 'вторая рука'
]
GEAR_SLOT_LABELS = {
    'шлем':'🪖 Шлем','плечи':'🪹 Плечи','тело':'🦺 Тело','плащ':'🧣 Плащ',
    'пояс':'⛓ Пояс','штаны':'🩳 Штаны','ботинки':'🥾 Ботинки','перчатки':'🧤 Перчатки',
    'кольцо':'💍 Кольцо','амул':'📿 Амулет','серьга':'🧏‍♀️ Серьга',
    'основная рука':'🗡 Основная рука','вторая рука':'🛡 Вторая рука'
}
RESOURCE_TYPES = [
    ('craft', '📦 Крафтовые'), ('consumable', '✨ Расходуемые'),
    ('scroll_recipe', '📜 Рецепты экипировки'), ('currency', '💰 Валюта'),
    ('alchemy', '🧪 Алхимия')
]
CARD_SLOTS = GEAR_SLOTS


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery):
            await event.answer("⛔ Нет доступа.", show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer("⛔ Нет доступа.")
        return None


admin_access = AdminOnlyMiddleware()
admin_router.message.outer_middleware(admin_access)
admin_router.callback_query.outer_middleware(admin_access)

# Подключаем роутер статистики
admin_router.include_router(stats_router)
stats_router.message.outer_middleware(admin_access)
stats_router.callback_query.outer_middleware(admin_access)

admin_router.callback_query(F.data == "admin_close")(admin_close)
admin_router.callback_query(F.data == "admin_cancel_edit")(admin_cancel_edit)

# ============================================================
# КОНФИГУРАЦИИ СУЩНОСТЕЙ
# ============================================================

class ResourceListStates(StatesGroup):
    list_page = State()

class GearListStates(StatesGroup):
    list_page = State()

class CardListStates(StatesGroup):
    list_page = State()

class CardAddStates(StatesGroup):
    name = State()
    emoji = State()
    slot = State()
    bonus1 = State()
    bonus2 = State()
    bonus3 = State()
    bonus4 = State()
    note = State()

# --- Ресурсы ---
async def resource_get_page(offset, limit):
    return await db.get_resources_page(offset, limit)

async def resource_update_field(res_id, field, value):
    if field == 'name':
        await db.update_resource(res_id, name=value)
    elif field == 'emoji':
        await db.update_resource(res_id, emoji=value)
    elif field == 'type':
        await db.update_resource(res_id, resource_type=value)
    elif field == 'note':
        await db.update_resource(res_id, note=value)

async def resource_get_by_id(res_id):
    return await db.get_resource_by_id(res_id)

async def resource_delete(res_id):
    await db.delete_resource(res_id)

# --- Снаряжение ---
async def gear_get_page(offset, limit):
    return await db.get_all_gear(offset, limit)

async def gear_update_field(gear_id, field, value):
    if field == 'name':
        await db.update_gear(gear_id, name=value)
    elif field == 'emoji':
        await db.update_gear(gear_id, emoji=value)
    elif field == 'rarity':
        await db.update_gear(gear_id, rarity=value)
    elif field == 'slot':
        await db.update_gear(gear_id, slot=value)
    elif field == 'level':
        await db.update_gear(gear_id, level=value)
    elif field == 'classes':
        await db.update_gear(gear_id, classes=value)
    elif field == 'note':
        await db.update_gear(gear_id, note=value)

async def gear_get_by_id(gear_id):
    return await db.get_gear_by_id(gear_id)

async def gear_delete(gear_id):
    await db.delete_gear(gear_id)

# --- Карты ---
async def card_get_page(offset, limit):
    return await db.get_cards_page(offset, limit)

async def card_update_field(card_id, field, value):
    await db.update_card(card_id, **{field: value})

async def card_get_by_id(card_id):
    return await db.get_card_by_id(card_id)

async def card_delete(card_id):
    await db.delete_card(card_id)

ENTITY_CONFIGS = {}

def _resource_display_format(d):
    note_part = ""
    note_val = d.get('note')
    if note_val:
        note_part = f"\n📝 {note_val}"
    return f"{d.get('emoji','')} {d.get('name','')} (тип: {ENTITY_CONFIGS['resource']['display_mapping']['type'].get(d.get('type','craft'), d.get('type','craft'))}){note_part}"

ENTITY_CONFIGS['resource'] = {
    'name': 'resource',
    'name_ru': 'ресурс',
    'get_page_func': resource_get_page,
    'get_by_id_func': resource_get_by_id,
    'update_field_func': resource_update_field,
    'delete_func': resource_delete,
    'item_callback_prefix': 'resource_edit',
    'list_state': ResourceListStates.list_page,
    'list_title': "📦 Ресурсы:\nВыберите ресурс для редактирования или добавьте новый:",
    'add_button': True,
    'add_button_text': "➕ Добавить ресурс",
    'add_callback': "resource_add_start",
    'edit_fields': [
        ('name', '✏️ Название'),
        ('emoji', '😀 Эмодзи'),
        ('type', '🏷 Тип'),
        ('note', '📝 Примечание')
    ],
    'integer_fields': [],
    'select_options': {
        'type': ['craft', 'consumable', 'scroll_recipe', 'currency', 'alchemy']
    },
    'display_mapping': {
        'type': {
            'craft': '📦 Крафтовый',
            'consumable': '✨ Расходуемый',
            'scroll_recipe': '📜 Рецепт экипировки',
            'currency': '💰 Валюта',
            'alchemy': '🧪 Алхимия'
        }
    },
    'display_format': _resource_display_format
}

ENTITY_CONFIGS['gear'] = {
    'name': 'gear',
    'name_ru': 'снаряжение',
    'get_page_func': gear_get_page,
    'get_by_id_func': gear_get_by_id,
    'update_field_func': gear_update_field,
    'delete_func': gear_delete,
    'item_callback_prefix': 'gear_edit',
    'list_state': GearListStates.list_page,
    'list_title': "⚔️ Управление снаряжением:\nВыберите предмет для редактирования или добавьте новый:",
    'add_button': True,
    'add_button_text': "➕ Добавить снаряжение",
    'add_callback': "gear_add_start",
    'edit_fields': [
        ('name', '✏️ Название'),
        ('rarity', '⭐ Редкость'),
        ('slot', '🔧 Слот'),
        ('level', '📈 Уровень'),
        ('classes', '🧙 Классы'),
        ('note', '📝 Примечание'),
        ('emoji', '😀 Эмодзи')
    ],
    'integer_fields': ['level'],
    'select_options': {
        'rarity': ['common', 'rare', 'epic', 'legendary'],
        'slot': [
            'шлем', 'плечи', 'тело', 'плащ', 'пояс', 'штаны', 'ботинки', 'перчатки',
            'кольцо', 'амул', 'серьга', 'основная рука', 'вторая рука'
        ]
    },
    'display_mapping': {
        'rarity': {
            'common': '⚪ Обычное',
            'rare': '🟢 Редкое',
            'epic': '🔵 Сверхредкое',
            'legendary': '🟣 Эпическая'
        },
        'slot': {
            'шлем': '🪖 Шлем',
            'плечи': '🪹 Плечи',
            'тело': '🦺 Тело',
            'плащ': '🧣 Плащ',
            'пояс': '⛓ Пояс',
            'штаны': '🩳 Штаны',
            'ботинки': '🥾 Ботинки',
            'перчатки': '🧤 Перчатки',
            'кольцо': '💍 Кольцо',
            'амул': '📿 Амулет',
            'серьга': '🧏‍♀️ Серьга',
            'основная рука': '🗡 Основная рука',
            'вторая рука': '🛡 Вторая рука'
        }
    },
    'display_format': lambda d: (
        f"{d.get('emoji','')} {d.get('name','')} "
        f"[{ENTITY_CONFIGS['gear']['display_mapping']['rarity'].get(d.get('rarity','common'), d.get('rarity','common'))}]"
        f"\n📈 Уровень: {d.get('level', 1)}"
        f"\n🧙 Классы: {d.get('classes') or 'Все классы'}"
        + (f"\n📝 {d.get('note')}" if d.get('note') else '')
    )
}

ENTITY_CONFIGS['card'] = {
    'name': 'card',
    'name_ru': 'карту',
    'get_page_func': card_get_page,
    'get_by_id_func': card_get_by_id,
    'update_field_func': card_update_field,
    'delete_func': card_delete,
    'item_callback_prefix': 'card_edit',
    'list_state': CardListStates.list_page,
    'list_title': "🃏 Управление картами:\nВыберите карту для редактирования или добавьте новую:",
    'add_button': True,
    'add_button_text': "➕ Добавить карту",
    'add_callback': "card_add_start",
    'edit_fields': [
        ('name', '✏️ Название'),
        ('emoji', '😀 Эмодзи'),
        ('slot', '🔧 Слот'),
        ('bonus1', '✨ Бонус 1'),
        ('bonus2', '✨ Бонус 2'),
        ('bonus3', '✨ Бонус 3'),
        ('bonus4', '✨ Бонус 4'),
        ('note', '📝 Примечание')
    ],
    'integer_fields': [],
    'select_options': {
        'slot': [
            'шлем', 'плечи', 'тело', 'плащ', 'пояс', 'штаны', 'ботинки', 'перчатки',
            'кольцо', 'амул', 'серьга', 'основная рука', 'вторая рука'
        ]
    },
    'display_mapping': {
        'slot': {
            'шлем': '🪖 Шлем', 'плечи': '🪹 Плечи', 'тело': '🦺 Тело', 'плащ': '🧣 Плащ',
            'пояс': '⛓ Пояс', 'штаны': '🩳 Штаны', 'ботинки': '🥾 Ботинки', 'перчатки': '🧤 Перчатки',
            'кольцо': '💍 Кольцо', 'амул': '📿 Амулет',
            'серьга': '🧏‍♀️ Серьга',
            'основная рука': '🗡 Основная рука', 'вторая рука': '🛡 Вторая рука'
        }
    },
    'display_format': lambda d: f"{d.get('emoji','')} {d.get('name','')} (слот: {ENTITY_CONFIGS['card']['display_mapping']['slot'].get(d.get('slot','?'), d.get('slot','?'))})"
}

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ РЕСУРСОВ
# ============================================================

@admin_router.callback_query(F.data == "admin_manage_resources")
async def manage_resources(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS['resource'], 1)

@admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("resource_edit_"))
async def resource_edit_item(callback: types.CallbackQuery, state: FSMContext):
    res_id = int(callback.data.split("_")[2])
    res = await db.get_resource_by_id(res_id)
    if not res:
        await callback.message.edit_text("Ресурс не найден.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, res_id, ENTITY_CONFIGS['resource'], res)

@admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("page_"))
async def resource_page_nav(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[1])
    await render_entity_list(callback, state, ENTITY_CONFIGS['resource'], page)

@admin_router.callback_query(ResourceListStates.list_page, F.data == "resource_add_start")
async def resource_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название нового ресурса:")
    await state.set_state(ResourceAddStates.name)

class ResourceAddStates(StatesGroup):
    name = State()
    emoji = State()
    type = State()
    note = State()

@admin_router.message(ResourceAddStates.name, F.text)
async def resource_add_emoji(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(res_name=name)
    await message.answer("Введите эмодзи:")
    await state.set_state(ResourceAddStates.emoji)

@admin_router.message(ResourceAddStates.emoji, F.text)
async def resource_add_emoji_input(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(res_emoji=emoji)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Для крафта", callback_data="res_type_craft")],
        [InlineKeyboardButton(text="✨ Расходуемый", callback_data="res_type_consumable")],
        [InlineKeyboardButton(text="📜 Рецепт экипировки", callback_data="res_type_scroll_recipe")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="res_type_currency")],
        [InlineKeyboardButton(text="🧪 Алхимия", callback_data="res_type_alchemy")]
    ])
    await message.answer("Выберите тип ресурса:", reply_markup=keyboard)
    await state.set_state(ResourceAddStates.type)

@admin_router.callback_query(ResourceAddStates.type, F.data.startswith("res_type_"))
async def resource_add_note(callback: types.CallbackQuery, state: FSMContext):
    type_map = {
        "craft": "craft",
        "consumable": "consumable",
        "scroll_recipe": "scroll_recipe",
        "currency": "currency",
        "alchemy": "alchemy"
    }
    resource_type = type_map.get(callback.data.split("_")[2], "craft")
    await state.update_data(res_type=resource_type)
    await callback.message.edit_text(
        "Введите примечание для ресурса (например, «Продаётся у торговца в городе»).\n"
        "Если не нужно, отправьте «-» или оставьте пустым:"
    )
    await state.set_state(ResourceAddStates.note)

@admin_router.message(ResourceAddStates.note, F.text)
async def resource_save(message: types.Message, state: FSMContext):
    note = message.text.strip()
    if note == "-":
        note = ""
    data = await state.get_data()
    try:
        await db.add_resource(data['res_name'], data['res_emoji'], data['res_type'], note)
        await message.answer("✅ Ресурс добавлен.")
    except Exception as e:
        logger.exception("Не удалось добавить ресурс")
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ СНАРЯЖЕНИЯ
# ============================================================

def build_admin_gear_slots_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"admin_gear_slot_{i}")] for i, slot in enumerate(GEAR_SLOTS)]
    rows.append([InlineKeyboardButton(text="➕ Добавить снаряжение", callback_data="gear_add_start")])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def render_admin_gear_slot(callback, state, slot_index: int, page: int = 1):
    slot = GEAR_SLOTS[slot_index]
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items = await db.execute_query(
        "SELECT id, name, emoji, rarity, level FROM gear WHERE slot = ? ORDER BY rarity, level, name LIMIT ? OFFSET ?",
        (slot, ADMIN_ITEMS_PER_PAGE + 1, offset),
    )
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    rarity_icons = {'common':'⚪','rare':'🟢','epic':'🔵','legendary':'🟣'}
    rows = [[InlineKeyboardButton(text=f"{rarity_icons.get(x.get('rarity'),'⚪')} {x.get('emoji','')} {x['name']} · ур. {x.get('level',1)}", callback_data=f"gear_edit_{x['id']}")] for x in items]
    nav=[]
    if page>1: nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_gear_page_{slot_index}_{page-1}"))
    if has_next: nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"admin_gear_page_{slot_index}_{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад к слотам", callback_data="admin_manage_gear")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(f"⚔️ Управление снаряжением · {GEAR_SLOT_LABELS[slot]}\nВыберите предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(gear_slot_index=slot_index, current_page=page, editing_entity='gear')
    await state.set_state(GearListStates.list_page)
    await callback.answer()

@admin_router.callback_query(F.data == "admin_manage_gear")
async def manage_gear(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚔️ Управление снаряжением\nВыберите слот:", reply_markup=build_admin_gear_slots_keyboard())
    await state.set_state(GearListStates.list_page)
    await callback.answer()

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_slot_"))
async def admin_gear_slot(callback: types.CallbackQuery, state: FSMContext):
    await render_admin_gear_slot(callback, state, int(callback.data.rsplit('_',1)[1]), 1)

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_page_"))
async def admin_gear_page(callback: types.CallbackQuery, state: FSMContext):
    parts=callback.data.split('_')
    await render_admin_gear_slot(callback, state, int(parts[3]), int(parts[4]))

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("gear_edit_"))
async def gear_edit_item(callback: types.CallbackQuery, state: FSMContext):
    gear_id = int(callback.data.split("_")[2])
    gear = await db.get_gear_by_id(gear_id)
    if not gear:
        await callback.message.edit_text("Снаряжение не найдено.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, gear_id, ENTITY_CONFIGS['gear'], gear)

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("legacy_gear_page_"))
async def gear_page_nav(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[1])
    await render_entity_list(callback, state, ENTITY_CONFIGS['gear'], page)

@admin_router.callback_query(GearListStates.list_page, F.data == "gear_add_start")
async def gear_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название снаряжения:")
    await state.set_state(GearAddStates.name)

class GearAddStates(StatesGroup):
    name = State()
    rarity = State()
    slot = State()
    emoji = State()
    level = State()
    classes = State()
    note = State()

class GearClassEditStates(StatesGroup):
    selecting = State()

GEAR_CLASSES = ["Аколит", "Бастион", "Маг", "Охотник", "Тень"]

def build_gear_classes_keyboard(selected):
    selected = set(selected)
    rows = []
    for class_name in GEAR_CLASSES:
        mark = "☑️" if class_name in selected else "⬜"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {class_name}",
            callback_data=f"gear_class_toggle_{GEAR_CLASSES.index(class_name)}"
        )])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="gear_classes_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@admin_router.message(GearAddStates.name, F.text)
async def gear_add_rarity(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(gear_name=name)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚪ Обычное (common)", callback_data="rarity_common")],
        [InlineKeyboardButton(text="🟢 Редкое (rare)", callback_data="rarity_rare")],
        [InlineKeyboardButton(text="🔵 Сверхредкое (epic)", callback_data="rarity_epic")],
        [InlineKeyboardButton(text="🟣 Эпическая (legendary)", callback_data="rarity_legendary")]
    ])
    await message.answer("Выберите редкость:", reply_markup=keyboard)
    await state.set_state(GearAddStates.rarity)

@admin_router.callback_query(GearAddStates.rarity, F.data.startswith("rarity_"))
async def gear_add_slot(callback: types.CallbackQuery, state: FSMContext):
    rarity = callback.data.split("_")[1]
    await state.update_data(gear_rarity=rarity)
    slots = [
        ("шлем", "🪖 Шлем"), ("плечи", "🪹 Плечи"), ("тело", "🦺 Тело"),
        ("плащ", "🧣 Плащ"), ("пояс", "⛓ Пояс"), ("штаны", "🩳 Штаны"),
        ("ботинки", "🥾 Ботинки"), ("перчатки", "🧤 Перчатки"),
        ("кольцо", "💍 Кольцо"),
        ("амул", "📿 Амулет"),
        ("серьга", "🧏‍♀️ Серьга"),
        ("основная рука", "🗡 Основная рука"),
        ("вторая рука", "🛡 Вторая рука")
    ]
    keyboard = [[InlineKeyboardButton(text=label, callback_data=f"slot_{name}")] for name, label in slots]
    await callback.message.edit_text("Выберите слот:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(GearAddStates.slot)

@admin_router.callback_query(GearAddStates.slot, F.data.startswith("slot_"))
async def gear_add_emoji(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split("_")[1]
    await state.update_data(gear_slot=slot)
    await callback.message.edit_text("Введите эмодзи:")
    await state.set_state(GearAddStates.emoji)

@admin_router.message(GearAddStates.emoji, F.text)
async def gear_add_level_prompt(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(gear_emoji=emoji)
    await message.answer("Введите минимальный уровень для экипировки (целое число от 1):")
    await state.set_state(GearAddStates.level)

@admin_router.message(GearAddStates.level, F.text)
async def gear_add_classes_prompt(message: types.Message, state: FSMContext):
    try:
        level = int(message.text.strip())
        if level < 1:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число не меньше 1.")
        return
    await state.update_data(gear_level=level, gear_classes=[])
    await message.answer(
        "Выберите один или несколько классов, затем нажмите «Готово»:",
        reply_markup=build_gear_classes_keyboard([])
    )
    await state.set_state(GearAddStates.classes)

@admin_router.callback_query(GearAddStates.classes, F.data.startswith("gear_class_toggle_"))
async def gear_add_toggle_class(callback: types.CallbackQuery, state: FSMContext):
    index = int(callback.data.rsplit("_", 1)[1])
    if index < 0 or index >= len(GEAR_CLASSES):
        await callback.answer("Неизвестный класс", show_alert=True)
        return
    data = await state.get_data()
    selected = list(data.get('gear_classes', []))
    class_name = GEAR_CLASSES[index]
    if class_name in selected:
        selected.remove(class_name)
    else:
        selected.append(class_name)
    selected.sort(key=GEAR_CLASSES.index)
    await state.update_data(gear_classes=selected)
    await callback.message.edit_reply_markup(reply_markup=build_gear_classes_keyboard(selected))
    await callback.answer()

@admin_router.callback_query(GearAddStates.classes, F.data == "gear_classes_done")
async def gear_add_note_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('gear_classes'):
        await callback.answer("Выберите хотя бы один класс", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏭ Без примечания", callback_data="gear_note_skip")
    ]])
    await callback.message.edit_text(
        "Введите примечание для снаряжения или нажмите «Без примечания»:",
        reply_markup=keyboard
    )
    await state.set_state(GearAddStates.note)
    await callback.answer()

async def save_new_gear(target, state: FSMContext, note: str):
    data = await state.get_data()
    try:
        await db.add_gear(
            data['gear_name'], data['gear_rarity'], data['gear_slot'], data['gear_emoji'],
            data['gear_level'], ", ".join(data['gear_classes']), note
        )
        await target.answer("✅ Снаряжение добавлено.")
    except Exception as e:
        await target.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await target.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())

@admin_router.message(GearAddStates.note, F.text)
async def gear_save_with_note(message: types.Message, state: FSMContext):
    await save_new_gear(message, state, message.text.strip())

@admin_router.callback_query(GearAddStates.note, F.data == "gear_note_skip")
async def gear_save_without_note(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await save_new_gear(callback.message, state, "")

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ КАРТ
# ============================================================

@admin_router.callback_query(F.data == "admin_manage_cards")
async def manage_cards(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS['card'], 1)

@admin_router.callback_query(CardListStates.list_page, F.data.startswith("card_edit_"))
async def card_edit_item(callback: types.CallbackQuery, state: FSMContext):
    card_id = int(callback.data.split("_")[2])
    card = await db.get_card_by_id(card_id)
    if not card:
        await callback.message.edit_text("Карта не найдена.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, card_id, ENTITY_CONFIGS['card'], card)

@admin_router.callback_query(CardListStates.list_page, F.data.startswith("page_"))
async def card_page_nav(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[1])
    await render_entity_list(callback, state, ENTITY_CONFIGS['card'], page)

@admin_router.callback_query(CardListStates.list_page, F.data == "card_add_start")
async def card_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название карты:")
    await state.set_state(CardAddStates.name)

@admin_router.message(CardAddStates.name, F.text)
async def card_add_emoji(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(card_name=name)
    await message.answer("Введите эмодзи:")
    await state.set_state(CardAddStates.emoji)

@admin_router.message(CardAddStates.emoji, F.text)
async def card_add_emoji_input(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(card_emoji=emoji)
    slots = [
        ("шлем", "🪖 Шлем"), ("плечи", "🪹 Плечи"), ("тело", "🦺 Тело"),
        ("плащ", "🧣 Плащ"), ("пояс", "⛓ Пояс"), ("штаны", "🩳 Штаны"),
        ("ботинки", "🥾 Ботинки"), ("перчатки", "🧤 Перчатки"),
        ("кольцо", "💍 Кольцо"),
        ("амул", "📿 Амулет"),
        ("серьга", "🧏‍♀️ Серьга"),
        ("основная рука", "🗡 Основная рука"),
        ("вторая рука", "🛡 Вторая рука")
    ]
    keyboard = [[InlineKeyboardButton(text=label, callback_data=f"card_slot_{name}")] for name, label in slots]
    await message.answer("Выберите слот:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(CardAddStates.slot)

@admin_router.callback_query(CardAddStates.slot, F.data.startswith("card_slot_"))
async def card_add_bonus1(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split("_")[2]
    await state.update_data(card_slot=slot)
    await callback.message.edit_text("Введите первый бонус (например: «Удача +2»):\nЕсли не нужно, отправьте «-».")
    await state.set_state(CardAddStates.bonus1)

@admin_router.message(CardAddStates.bonus1, F.text)
async def card_add_bonus2(message: types.Message, state: FSMContext):
    bonus1 = message.text.strip()
    if bonus1 == "-":
        bonus1 = ""
    await state.update_data(card_bonus1=bonus1)
    await message.answer("Введите второй бонус (или «-»):")
    await state.set_state(CardAddStates.bonus2)

@admin_router.message(CardAddStates.bonus2, F.text)
async def card_add_bonus3(message: types.Message, state: FSMContext):
    bonus2 = message.text.strip()
    if bonus2 == "-":
        bonus2 = ""
    await state.update_data(card_bonus2=bonus2)
    await message.answer("Введите третий бонус (или «-»):")
    await state.set_state(CardAddStates.bonus3)

@admin_router.message(CardAddStates.bonus3, F.text)
async def card_add_bonus4(message: types.Message, state: FSMContext):
    bonus3 = message.text.strip()
    if bonus3 == "-":
        bonus3 = ""
    await state.update_data(card_bonus3=bonus3)
    await message.answer("Введите четвёртый бонус (или «-»):")
    await state.set_state(CardAddStates.bonus4)

@admin_router.message(CardAddStates.bonus4, F.text)
async def card_add_note(message: types.Message, state: FSMContext):
    bonus4 = message.text.strip()
    if bonus4 == "-":
        bonus4 = ""
    await state.update_data(card_bonus4=bonus4)
    await message.answer("Введите примечание (или «-»):")
    await state.set_state(CardAddStates.note)

@admin_router.message(CardAddStates.note, F.text)
async def card_save(message: types.Message, state: FSMContext):
    note = message.text.strip()
    if note == "-":
        note = ""
    data = await state.get_data()
    try:
        await db.add_card(
            name=data['card_name'],
            emoji=data['card_emoji'],
            slot=data['card_slot'],
            bonus1=data.get('card_bonus1', ''),
            bonus2=data.get('card_bonus2', ''),
            bonus3=data.get('card_bonus3', ''),
            bonus4=data.get('card_bonus4', ''),
            note=note
        )
        await message.answer("✅ Карта добавлена.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())

# ============================================================
# УПРАВЛЕНИЕ МОБАМИ
# ============================================================

class MobStates(StatesGroup):
    add_name = State()
    add_emoji = State()
    add_hp = State()
    add_dust_min = State()
    add_dust_max = State()
    add_exp = State()
    add_location = State()
    edit_select = State()
    edit_field = State()
    edit_new_value = State()
    drop_category = State()
    drop_list_page = State()

async def get_mob_locations_keyboard() -> InlineKeyboardMarkup:
    locations = await db.get_locations()
    rows = [[InlineKeyboardButton(
        text=f"{loc.get('emoji') or '📍'} {loc['name']}",
        callback_data=f"mob_location_{loc['id']}"
    )] for loc in locations]
    rows.append([InlineKeyboardButton(text="➕ Добавить моба", callback_data="mob_add_start")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_mob_list_keyboard(location_id: int, page: int = 1) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    mobs = await db.execute_query(
        "SELECT id, name, emoji FROM mobs WHERE location_id = ? ORDER BY name LIMIT ? OFFSET ?",
        (location_id, ADMIN_ITEMS_PER_PAGE + 1, offset),
    )
    has_next = len(mobs) > ADMIN_ITEMS_PER_PAGE
    mobs = mobs[:ADMIN_ITEMS_PER_PAGE]
    rows = [[InlineKeyboardButton(
        text=f"{mob['emoji']} {mob['name']}", callback_data=f"edit_mob_{mob['id']}"
    )] for mob in mobs]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"mob_page_{location_id}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"mob_page_{location_id}_{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="➕ Добавить моба", callback_data="mob_add_start")])
    rows.append([InlineKeyboardButton(text="🔙 К локациям", callback_data="back_to_mob_locations")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "admin_edit_mob")
async def start_edit_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.clear()
    await callback.message.edit_text(
        "🐾 Управление мобами:\nВыберите локацию:",
        reply_markup=await get_mob_locations_keyboard(),
    )
    await state.set_state(MobStates.edit_select)
    await callback.answer()


@admin_router.callback_query(MobStates.edit_select, F.data.startswith("mob_location_"))
async def mob_location_select(callback: types.CallbackQuery, state: FSMContext):
    location_id = int(callback.data.rsplit("_", 1)[1])
    location = await db.execute_query("SELECT name, emoji FROM locations WHERE id = ?", (location_id,))
    if not location:
        await callback.answer("Локация не найдена", show_alert=True)
        return
    await state.update_data(mob_location_id=location_id)
    loc = location[0]
    await callback.message.edit_text(
        f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:",
        reply_markup=await get_mob_list_keyboard(location_id, 1),
    )
    await callback.answer()


@admin_router.callback_query(MobStates.edit_select, F.data.startswith("mob_page_"))
async def mob_list_page(callback: types.CallbackQuery, state: FSMContext):
    _, _, location_id, page = callback.data.split("_")
    location_id, page = int(location_id), int(page)
    await state.update_data(mob_location_id=location_id)
    location = await db.execute_query("SELECT name, emoji FROM locations WHERE id = ?", (location_id,))
    loc = location[0] if location else {'name': 'Локация', 'emoji': '📍'}
    await callback.message.edit_text(
        f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:",
        reply_markup=await get_mob_list_keyboard(location_id, page),
    )
    await callback.answer()


@admin_router.callback_query(MobStates.edit_select, F.data == "back_to_mob_locations")
async def back_to_mob_locations(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(mob_location_id=None)
    await callback.message.edit_text(
        "🐾 Управление мобами:\nВыберите локацию:",
        reply_markup=await get_mob_locations_keyboard(),
    )
    await callback.answer()

@admin_router.callback_query(MobStates.edit_select, F.data.startswith("edit_mob_"))
async def mob_edit_menu(callback: types.CallbackQuery, state: FSMContext):
    mob_id = int(callback.data.split("_")[2])
    mob = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await callback.answer()
        return
    mob = mob[0]
    await state.update_data(mob_id=mob_id)
    fields = [
        ('name', f"Имя: {mob['name']}"),
        ('emoji', f"Эмодзи: {mob['emoji']}"),
        ('hp', f"HP: {mob['hp']}"),
        ('dust_min', f"Пыль мин: {mob['dust_min']}"),
        ('dust_max', f"Пыль макс: {mob['dust_max']}"),
        ('exp', f"Опыт: {mob['exp']}"),
        ('location_id', f"ID локации: {mob['location_id']}")
    ]
    keyboard = [[InlineKeyboardButton(text=label, callback_data=f"mob_edit_field_{field}")] for field, label in fields]
    keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="mob_drop_menu")])
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить моба", callback_data="mob_delete")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_mob_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(f"Редактирование моба ID {mob_id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(MobStates.edit_field)

@admin_router.callback_query(MobStates.edit_field, F.data.startswith("mob_edit_field_"))
async def mob_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_", 3)[3]
    await state.update_data(edit_field=field)
    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(MobStates.edit_new_value)

@admin_router.message(MobStates.edit_new_value, F.text)
async def mob_update_field(message: types.Message, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    field = data.get('edit_field')

    if not mob_id or not field:
        logger.warning(f"Ошибка состояния: mob_id={mob_id}, field={field}. Данные состояния: {data}")
        await message.answer(
            "❌ Ошибка состояния. Пожалуйста, начните редактирование моба заново.\n"
            "Выберите моба из списка:"
        )
        await state.clear()
        keyboard = await get_mob_locations_keyboard()
        await message.answer("Выберите моба для редактирования:", reply_markup=keyboard)
        await state.set_state(MobStates.edit_select)
        return

    new_value = message.text.strip()

    if field in ('hp', 'dust_min', 'dust_max', 'exp', 'location_id'):
        try:
            new_value = int(new_value)
            if new_value < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите положительное целое число.")
            return

    if field == 'emoji' and not is_valid_emoji(new_value):
        await message.answer("❌ Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return

    if field == 'name' and not new_value:
        await message.answer("❌ Имя не может быть пустым.")
        return

    try:
        await db.update_mob_field(mob_id, field, new_value)

        mob_result = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
        if not mob_result:
            await message.answer("❌ Моб не найден. Возврат в админку.")
            await state.clear()
            await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            return
        mob = mob_result[0]

        fields_list = [
            ('name', f"Имя: {mob['name']}"),
            ('emoji', f"Эмодзи: {mob['emoji']}"),
            ('hp', f"HP: {mob['hp']}"),
            ('dust_min', f"Пыль мин: {mob['dust_min']}"),
            ('dust_max', f"Пыль макс: {mob['dust_max']}"),
            ('exp', f"Опыт: {mob['exp']}"),
            ('location_id', f"ID локации: {mob['location_id']}")
        ]
        keyboard = []
        for field_name, label in fields_list:
            keyboard.append([InlineKeyboardButton(text=label, callback_data=f"mob_edit_field_{field_name}")])
        keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="mob_drop_menu")])
        keyboard.append([InlineKeyboardButton(text="🗑 Удалить моба", callback_data="mob_delete")])
        keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_mob_list")])
        keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])

        await message.answer(f"✅ Поле {field} обновлено.")
        await message.answer(
            f"Редактирование моба ID {mob_id}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )

        await state.update_data(edit_field=None)
        await state.set_state(MobStates.edit_field)

        try:
            await message.delete()
        except TelegramAPIError:
            pass

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.exception("Ошибка при обновлении поля моба")

@admin_router.callback_query(F.data == "back_to_mob_list")
async def back_to_mob_list_from_edit(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    location_id = data.get('mob_location_id')
    if not location_id and data.get('mob_id'):
        row = await db.execute_query("SELECT location_id FROM mobs WHERE id = ?", (data['mob_id'],))
        location_id = row[0]['location_id'] if row else None
    await state.clear()
    await state.set_state(MobStates.edit_select)
    if location_id:
        await state.update_data(mob_location_id=location_id)
        location = await db.execute_query("SELECT name, emoji FROM locations WHERE id = ?", (location_id,))
        loc = location[0] if location else {'name': 'Локация', 'emoji': '📍'}
        await callback.message.edit_text(
            f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:",
            reply_markup=await get_mob_list_keyboard(location_id, 1),
        )
    else:
        await callback.message.edit_text(
            "🐾 Управление мобами:\nВыберите локацию:",
            reply_markup=await get_mob_locations_keyboard(),
        )
    await callback.answer()

@admin_router.callback_query(MobStates.edit_field, F.data == "mob_delete")
async def mob_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    mob = await db.execute_query("SELECT name FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_mob_delete")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_mob_list")]
    ])
    await callback.message.edit_text(f"Удалить моба <b>{mob[0]['name']}</b>?", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@admin_router.callback_query(F.data == "confirm_mob_delete")
async def mob_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    await db.delete_mob(mob_id)
    await callback.message.edit_text("✅ Моб удалён.")
    keyboard = await get_mob_locations_keyboard()
    await callback.message.answer("🐾 Управление мобами:\nВыберите моба или добавьте нового:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)
    await callback.answer()

# ------------------- Управление дропом -------------------
def build_drop_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🃏 Карты", callback_data="drop_category_card")],
        [InlineKeyboardButton(text="🔙 Назад к мобу", callback_data="back_to_mob_list")],
    ])

def build_drop_filters_keyboard(category: str) -> InlineKeyboardMarkup:
    if category == 'resource':
        rows = [[InlineKeyboardButton(text=label, callback_data=f"drop_filter_resource_{i}")] for i, (_, label) in enumerate(RESOURCE_TYPES)]
    elif category == 'gear':
        rows = [[InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"drop_filter_gear_{i}")] for i, slot in enumerate(GEAR_SLOTS)]
    else:
        rows = [[InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"drop_filter_card_{i}")] for i, slot in enumerate(CARD_SLOTS)]
    rows.append([InlineKeyboardButton(text="🔙 Назад к типам дропа", callback_data="back_to_drop_categories")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def resolve_drop_filter(category: str, filter_index: int) -> str:
    if category == 'resource': return RESOURCE_TYPES[filter_index][0]
    if category == 'gear': return GEAR_SLOTS[filter_index]
    return CARD_SLOTS[filter_index]

async def get_drop_list_keyboard(mob_id: int, category: str, filter_value: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    if category == 'resource':
        sql = "SELECT id, name, emoji FROM resources WHERE type = ? ORDER BY name LIMIT ? OFFSET ?"
        params = (filter_value, ADMIN_ITEMS_PER_PAGE + 1, offset)
    elif category == 'gear':
        sql = "SELECT id, name, emoji, rarity, slot FROM gear WHERE slot = ? ORDER BY CASE rarity WHEN 'common' THEN 1 WHEN 'rare' THEN 2 WHEN 'epic' THEN 3 WHEN 'legendary' THEN 4 ELSE 5 END, level, name LIMIT ? OFFSET ?"
        params = (filter_value, ADMIN_ITEMS_PER_PAGE + 1, offset)
    else:
        sql = "SELECT id, name, emoji, slot FROM cards WHERE slot = ? ORDER BY name LIMIT ? OFFSET ?"
        params = (filter_value, ADMIN_ITEMS_PER_PAGE + 1, offset)
    items = await db.execute_query(sql, params)
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    rows=[]
    rarity_icons={'common':'⚪','rare':'🟢','epic':'🔵','legendary':'🟣'}
    for item in items:
        enabled = await db.get_drop_status(mob_id, category, item['id'])
        prefix = rarity_icons.get(item.get('rarity'),'') if category == 'gear' else ''
        rows.append([InlineKeyboardButton(text=f"{'✅' if enabled else '❌'} {prefix} {item.get('emoji','')} {item['name']}", callback_data=f"drop_toggle_{category}_{item['id']}_{page}")])
    nav=[]
    if page>1: nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"drop_page_{category}_{page-1}"))
    if has_next: nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"drop_page_{category}_{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data=f"back_to_drop_filters_{category}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@admin_router.callback_query(MobStates.edit_field, F.data == "mob_drop_menu")
async def mob_drop_category(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите тип дропа:", reply_markup=build_drop_categories_keyboard())
    await state.set_state(MobStates.drop_category)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data == "back_to_mob_list")
async def back_to_mob_edit_from_drop_category(callback: types.CallbackQuery, state: FSMContext):
    data=await state.get_data(); mob_id=data.get('mob_id')
    mob=(await db.execute_query("SELECT * FROM mobs WHERE id = ?",(mob_id,)))[0]
    fields=[('name',f"Имя: {mob['name']}"),('emoji',f"Эмодзи: {mob['emoji']}"),('hp',f"HP: {mob['hp']}"),('dust_min',f"Пыль мин: {mob['dust_min']}"),('dust_max',f"Пыль макс: {mob['dust_max']}"),('exp',f"Опыт: {mob['exp']}"),('location_id',f"ID локации: {mob['location_id']}")]
    rows=[[InlineKeyboardButton(text=label,callback_data=f"mob_edit_field_{field}")] for field,label in fields]
    rows += [[InlineKeyboardButton(text="📦 Управление дропом",callback_data="mob_drop_menu")],[InlineKeyboardButton(text="🗑 Удалить моба",callback_data="mob_delete")],[InlineKeyboardButton(text="🔙 Назад к списку",callback_data="back_to_mob_list")],[InlineKeyboardButton(text="🏠 Главное меню",callback_data="admin_cancel_edit")]]
    await callback.message.edit_text(f"Редактирование моба ID {mob_id}",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)); await state.set_state(MobStates.edit_field); await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_filters(callback: types.CallbackQuery, state: FSMContext):
    category=callback.data.split('_')[2]
    await callback.message.edit_text("Выберите категорию:",reply_markup=build_drop_filters_keyboard(category))
    await state.update_data(drop_category=category)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data.startswith("drop_filter_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext):
    parts=callback.data.split('_'); category=parts[2]; filter_index=int(parts[3]); filter_value=resolve_drop_filter(category,filter_index)
    data=await state.get_data(); keyboard=await get_drop_list_keyboard(data['mob_id'],category,filter_value,1)
    await callback.message.edit_text("✅ — падает, ❌ — не падает",reply_markup=keyboard)
    await state.update_data(drop_category=category,drop_filter_index=filter_index,drop_filter_value=filter_value,drop_page=1)
    await state.set_state(MobStates.drop_list_page); await callback.answer()

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_page_"))
async def drop_page(callback: types.CallbackQuery, state: FSMContext):
    parts=callback.data.split('_'); category=parts[2]; page=int(parts[3]); data=await state.get_data()
    await callback.message.edit_reply_markup(reply_markup=await get_drop_list_keyboard(data['mob_id'],category,data['drop_filter_value'],page)); await state.update_data(drop_page=page); await callback.answer()

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_toggle_"))
async def toggle_drop(callback: types.CallbackQuery, state: FSMContext):
    parts=callback.data.split('_'); category=parts[2]; item_id=int(parts[3]); page=int(parts[4]); data=await state.get_data(); mob_id=data['mob_id']
    if await db.get_drop_status(mob_id,category,item_id): await db.remove_drop(mob_id,category,item_id); await callback.answer("❌ Дроп убран")
    else: await db.add_drop(mob_id,category,item_id); await callback.answer("✅ Дроп добавлен")
    await callback.message.edit_reply_markup(reply_markup=await get_drop_list_keyboard(mob_id,category,data['drop_filter_value'],page))

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("back_to_drop_filters_"))
async def back_to_drop_filters(callback: types.CallbackQuery, state: FSMContext):
    category=callback.data.rsplit('_',1)[1]
    await callback.message.edit_text("Выберите категорию:",reply_markup=build_drop_filters_keyboard(category)); await state.set_state(MobStates.drop_category); await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите тип дропа:",reply_markup=build_drop_categories_keyboard()); await callback.answer()

# ---------- Добавление моба ----------
@admin_router.callback_query(MobStates.edit_select, F.data == "mob_add_start")
async def start_add_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await callback.message.edit_text("Введите название моба:")
    await state.set_state(MobStates.add_name)
    await callback.answer()

@admin_router.message(MobStates.add_name, F.text)
async def add_mob_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите эмодзи моба:")
    await state.set_state(MobStates.add_emoji)

@admin_router.message(MobStates.add_emoji, F.text)
async def add_mob_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(emoji=emoji)
    await message.answer("Введите HP:")
    await state.set_state(MobStates.add_hp)

@admin_router.message(MobStates.add_hp, F.text)
async def add_mob_hp(message: types.Message, state: FSMContext):
    try:
        hp = int(message.text.strip())
        if hp < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(hp=hp)
    await message.answer("Введите dust_min:")
    await state.set_state(MobStates.add_dust_min)

@admin_router.message(MobStates.add_dust_min, F.text)
async def add_mob_dust_min(message: types.Message, state: FSMContext):
    try:
        dust_min = int(message.text.strip())
        if dust_min < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(dust_min=dust_min)
    await message.answer("Введите dust_max:")
    await state.set_state(MobStates.add_dust_max)

@admin_router.message(MobStates.add_dust_max, F.text)
async def add_mob_dust_max(message: types.Message, state: FSMContext):
    try:
        dust_max = int(message.text.strip())
        if dust_max < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    data = await state.get_data()
    if dust_max < data['dust_min']:
        await message.answer("dust_max не может быть меньше dust_min")
        return
    await state.update_data(dust_max=dust_max)
    await message.answer("Введите опыт (exp):")
    await state.set_state(MobStates.add_exp)

@admin_router.message(MobStates.add_exp, F.text)
async def add_mob_exp(message: types.Message, state: FSMContext):
    try:
        exp = int(message.text.strip())
        if exp < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(exp=exp)
    locations = await db.get_locations()
    if not locations:
        await message.answer("Нет локаций.")
        await state.clear()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{loc['emoji']} {loc['name']}", callback_data=f"loc_{loc['id']}")] for loc in locations
    ])
    await message.answer("Выберите локацию:", reply_markup=keyboard)
    await state.set_state(MobStates.add_location)

@admin_router.callback_query(MobStates.add_location, F.data.startswith("loc_"))
async def add_mob_location(callback: types.CallbackQuery, state: FSMContext):
    location_id = int(callback.data.split("_")[1])
    data = await state.get_data()
    try:
        await db.execute_query(
            "INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id) VALUES (?,?,?,?,?,?,?)",
            (data['name'], data['emoji'], data['hp'], data['dust_min'], data['dust_max'], data['exp'], location_id)
        )
        await callback.message.edit_text("✅ Моб добавлен.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_mob_locations_keyboard()
    await callback.message.answer(
        "🐾 Управление мобами:\nВыберите моба или добавьте нового:",
        reply_markup=keyboard,
    )
    await state.set_state(MobStates.edit_select)
    await callback.answer()


register_generic_handlers(admin_router, lambda: ENTITY_CONFIGS)

# ============================================================
# Основная команда для админ-панели
# ============================================================
@admin_router.message(Command("kombat"))
async def admin_panel(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await state.clear()
    await message.answer("🔧 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML",
                         reply_markup=get_admin_main_keyboard())
