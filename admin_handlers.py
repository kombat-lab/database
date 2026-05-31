import os
import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db
from utils import is_valid_emoji, clean_username
from admin_utils import (
    ADMIN_ITEMS_PER_PAGE,
    get_admin_main_keyboard,
    admin_close,
    admin_cancel_edit,
    render_entity_list,
    show_edit_menu,
    register_generic_handlers,
)
from stats_handlers import stats_router

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

# Подключаем роутер статистики
admin_router.include_router(stats_router)

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
    'list_title': "📦 Управление ресурсами:\nВыберите ресурс для редактирования или добавьте новый:",
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
        ('emoji', '😀 Эмодзи')
    ],
    'integer_fields': [],
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
    'display_format': lambda d: f"{d.get('emoji','')} {d.get('name','')} [{ENTITY_CONFIGS['gear']['display_mapping']['rarity'].get(d.get('rarity','common'), d.get('rarity','common'))}]"
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
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await render_entity_list(message, state, ENTITY_CONFIGS['resource'], 1)
    await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ СНАРЯЖЕНИЯ
# ============================================================

@admin_router.callback_query(F.data == "admin_manage_gear")
async def manage_gear(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS['gear'], 1)

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("gear_edit_"))
async def gear_edit_item(callback: types.CallbackQuery, state: FSMContext):
    gear_id = int(callback.data.split("_")[2])
    gear = await db.get_gear_by_id(gear_id)
    if not gear:
        await callback.message.edit_text("Снаряжение не найдено.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, gear_id, ENTITY_CONFIGS['gear'], gear)

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("page_"))
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
async def gear_save(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    data = await state.get_data()
    try:
        await db.add_gear(data['gear_name'], data['gear_rarity'], data['gear_slot'], emoji)
        await message.answer("✅ Снаряжение добавлено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())

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

async def get_mob_list_keyboard(page):
    from admin_utils import build_paginated_keyboard
    offset = (page-1)*ADMIN_ITEMS_PER_PAGE
    mobs = await db.execute_query("SELECT id, name, emoji FROM mobs ORDER BY id LIMIT ? OFFSET ?",
                                  (ADMIN_ITEMS_PER_PAGE+1, offset))
    has_next = len(mobs) > ADMIN_ITEMS_PER_PAGE
    mobs = mobs[:ADMIN_ITEMS_PER_PAGE]
    items = [{'id': m['id'], 'name': m['name'], 'emoji': m['emoji']} for m in mobs]
    return build_paginated_keyboard(items, page, has_next, "edit_mob", 
                                    extra_buttons=[[InlineKeyboardButton(text="➕ Добавить моба", callback_data="mob_add_start")]])

@admin_router.callback_query(F.data == "admin_edit_mob")
async def start_edit_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.clear()
    keyboard = await get_mob_list_keyboard(1)
    await callback.message.edit_text("Выберите моба для редактирования:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)

@admin_router.callback_query(MobStates.edit_select, F.data.startswith("page_"))
async def mob_list_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[1])
    keyboard = await get_mob_list_keyboard(page)
    await callback.message.edit_text("Выберите моба для редактирования:", reply_markup=keyboard)
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
        keyboard = await get_mob_list_keyboard(1)
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
        except:
            pass

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.exception("Ошибка при обновлении поля моба")

@admin_router.callback_query(F.data == "back_to_mob_list")
async def back_to_mob_list_from_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = await get_mob_list_keyboard(1)
    await callback.message.edit_text(
        "Выберите моба для редактирования:",
        reply_markup=keyboard
    )
    await state.set_state(MobStates.edit_select)
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

@admin_router.callback_query(F.data == "confirm_mob_delete")
async def mob_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    await db.delete_mob(mob_id)
    await callback.message.edit_text("✅ Моб удалён.")
    keyboard = await get_mob_list_keyboard(1)
    await callback.message.answer("Выберите моба для редактирования:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)

# ------------------- Управление дропом -------------------
async def get_drop_list_keyboard(mob_id: int, category: str, page: int) -> InlineKeyboardMarkup:
    from admin_utils import build_paginated_keyboard, ADMIN_ITEMS_PER_PAGE
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    if category == 'resource':
        items = await db.get_resources_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
        has_next = len(items) > ADMIN_ITEMS_PER_PAGE
        items = items[:ADMIN_ITEMS_PER_PAGE]
    elif category == 'gear':
        items = await db.execute_query(
            "SELECT id, name, emoji, slot FROM gear WHERE rarity IN ('common', 'rare') ORDER BY id LIMIT ? OFFSET ?",
            (ADMIN_ITEMS_PER_PAGE + 1, offset)
        )
        has_next = len(items) > ADMIN_ITEMS_PER_PAGE
        items = items[:ADMIN_ITEMS_PER_PAGE]
    elif category == 'card':
        items = await db.execute_query(
            "SELECT id, name, emoji, slot FROM cards ORDER BY id LIMIT ? OFFSET ?",
            (ADMIN_ITEMS_PER_PAGE + 1, offset)
        )
        has_next = len(items) > ADMIN_ITEMS_PER_PAGE
        items = items[:ADMIN_ITEMS_PER_PAGE]
    else:
        return InlineKeyboardMarkup(inline_keyboard=[])

    keyboard = []
    for item in items:
        has_drop = await db.get_drop_status(mob_id, category, item['id'])
        status = "✅" if has_drop else "❌"
        text = f"{status} {item.get('emoji', '')} {item['name']}"
        if category == 'gear' and item.get('slot'):
            text += f" ({item['slot']})"
        elif category == 'card' and item.get('slot'):
            text += f" (слот: {item['slot']})"
        callback_data = f"drop_toggle_{category}_{item['id']}_{page}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=callback_data)])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"drop_page_{category}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"drop_page_{category}_{page+1}"))
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_drop_categories")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(MobStates.edit_field, F.data == "mob_drop_menu")
async def mob_drop_category(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await callback.answer("Ошибка: моб не найден", show_alert=True)
        return
    await state.update_data(mob_id=mob_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка (common/rare)", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🃏 Карты", callback_data="drop_category_card")],
        [InlineKeyboardButton(text="🔙 Назад к мобу", callback_data="back_to_mob_list")]
    ])
    await callback.message.edit_text("Выберите категорию дропа:", reply_markup=keyboard)
    await state.set_state(MobStates.drop_category)

@admin_router.callback_query(MobStates.drop_category, F.data == "back_to_mob_list")
async def back_to_mob_edit_from_drop_category(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await callback.message.edit_text("Ошибка: моб не найден.")
        await state.clear()
        await callback.answer()
        return
    mob = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    mob = mob[0]
    fields = [
        ('name', f"Имя: {mob['name']}"),
        ('emoji', f"Эмодзи: {mob['emoji']}"),
        ('hp', f"HP: {mob['hp']}"),
        ('dust_min', f"Пыль мин: {mob['dust_min']}"),
        ('dust_max', f"Пыль макс: {mob['dust_max']}"),
        ('exp', f"Опыт: {mob['exp']}"),
        ('location_id', f"ID локации: {mob['location_id']}")
    ]
    keyboard = []
    for field, label in fields:
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"mob_edit_field_{field}")])
    keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="mob_drop_menu")])
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить моба", callback_data="mob_delete")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_mob_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(
        f"Редактирование моба ID {mob_id}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await callback.answer("❌ Ошибка: моб не найден", show_alert=True)
        return
    keyboard = await get_drop_list_keyboard(mob_id, category, 1)
    await callback.message.edit_text(
        f"Управление дропом: {category}\n✅ - падает, ❌ - не падает",
        reply_markup=keyboard
    )
    await state.update_data(drop_category=category, drop_page=1)
    await state.set_state(MobStates.drop_list_page)

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_page_"))
async def drop_page(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    category = parts[2]
    page = int(parts[3])
    data = await state.get_data()
    mob_id = data['mob_id']
    keyboard = await get_drop_list_keyboard(mob_id, category, page)
    await callback.message.edit_text(f"Управление дропом: {category}", reply_markup=keyboard)
    await state.update_data(drop_page=page)

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_toggle_"))
async def toggle_drop(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    category = parts[2]
    item_id = int(parts[3])
    page = int(parts[4])

    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await callback.answer("❌ Ошибка: моб не найден", show_alert=True)
        return

    try:
        has_drop = await db.get_drop_status(mob_id, category, item_id)
        if has_drop:
            await db.remove_drop(mob_id, category, item_id)
            await callback.answer("❌ Дроп убран", show_alert=False)
        else:
            await db.add_drop(mob_id, category, item_id)
            await callback.answer("✅ Дроп добавлен", show_alert=False)
    except Exception as e:
        logger.error(f"Toggle drop error: {e}")
        await callback.answer(f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
        return

    keyboard = await get_drop_list_keyboard(mob_id, category, page)
    await callback.message.edit_reply_markup(reply_markup=keyboard)

@admin_router.callback_query(MobStates.drop_list_page, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await callback.answer("❌ Моб не найден", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🃏 Карты", callback_data="drop_category_card")],
        [InlineKeyboardButton(text="🔙 Назад к мобу", callback_data="back_to_mob_list")]
    ])
    await callback.message.edit_text("Выберите категорию дропа:", reply_markup=keyboard)
    await state.set_state(MobStates.drop_category)
    await callback.answer()

# ---------- Добавление моба ----------
@admin_router.callback_query(F.data == "admin_add_mob")
async def start_add_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await callback.message.edit_text("Введите название моба:")
    await state.set_state(MobStates.add_name)

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
    except:
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
    except:
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
    except:
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
    except:
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
    await callback.message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
    await callback.answer()

# ---------- Удаление моба ----------
class DeleteMobStates(StatesGroup):
    select = State()
    confirm = State()

async def get_delete_mob_keyboard(page):
    from admin_utils import build_paginated_keyboard
    offset = (page-1)*ADMIN_ITEMS_PER_PAGE
    mobs = await db.execute_query("SELECT id, name FROM mobs ORDER BY id LIMIT ? OFFSET ?",
                                  (ADMIN_ITEMS_PER_PAGE+1, offset))
    has_next = len(mobs) > ADMIN_ITEMS_PER_PAGE
    mobs = mobs[:ADMIN_ITEMS_PER_PAGE]
    items = [{'id': m['id'], 'name': m['name']} for m in mobs]
    return build_paginated_keyboard(items, page, has_next, "del_mob")

@admin_router.callback_query(F.data == "admin_delete_mob")
async def start_delete_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    keyboard = await get_delete_mob_keyboard(1)
    await callback.message.edit_text("Выберите моба для удаления:", reply_markup=keyboard)
    await state.set_state(DeleteMobStates.select)

@admin_router.callback_query(DeleteMobStates.select, F.data.startswith("page_"))
async def delete_mob_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[1])
    keyboard = await get_delete_mob_keyboard(page)
    await callback.message.edit_text("Выберите моба для удаления:", reply_markup=keyboard)

@admin_router.callback_query(DeleteMobStates.select, F.data.startswith("del_mob_"))
async def confirm_delete_mob(callback: types.CallbackQuery, state: FSMContext):
    mob_id = int(callback.data.split("_")[2])
    mob = await db.execute_query("SELECT name FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await callback.answer()
        return
    await state.update_data(mob_id=mob_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_del_mob")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_edit")]
    ])
    await callback.message.edit_text(f"Удалить моба <b>{mob[0]['name']}</b>?", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(DeleteMobStates.confirm)

@admin_router.callback_query(DeleteMobStates.confirm, F.data == "confirm_del_mob")
async def delete_mob_final(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    await db.delete_mob(mob_id)
    await callback.message.edit_text("✅ Моб удалён.")
    await state.clear()
    await callback.message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
    await callback.answer()

# ============================================================
# УПРАВЛЕНИЕ РЕЦЕПТАМИ
# ============================================================

class RecipeStates(StatesGroup):
    list_type = State()
    list_page = State()
    view_recipe = State()
    add_confirm = State()
    add_ingredient = State()
    add_ingredient_page = State()
    add_owner = State()
    edit_ingredient = State()
    edit_ingredient_quantity = State()
    delete_confirm = State()

async def get_recipe_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Снаряжение (gear)", callback_data="recipe_type_gear")],
        [InlineKeyboardButton(text="📦 Ресурсы (resource)", callback_data="recipe_type_resource")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")]
    ])

@admin_router.callback_query(F.data == "admin_manage_recipes")
async def manage_recipes_type(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)

async def get_recipe_list_keyboard(result_type: str, page: int):
    offset = (page-1)*ADMIN_ITEMS_PER_PAGE
    recipes = await db.get_all_recipes(result_type, offset, ADMIN_ITEMS_PER_PAGE+1)
    has_next = len(recipes) > ADMIN_ITEMS_PER_PAGE
    recipes = recipes[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for r in recipes:
        if result_type == 'gear':
            text = f"{r['result_emoji']} {r['result_name']} (ID рец.{r['id']}) | ингр:{r['ingredient_count']} влад:{r['owner_count']}"
        else:
            text = f"{r['result_emoji']} {r['result_name']} (ID рец.{r['id']}) | ингр:{r['ingredient_count']}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"recipe_view_{r['id']}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"recipe_page_{result_type}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"recipe_page_{result_type}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="➕ Добавить рецепт", callback_data=f"recipe_add_{result_type}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Выбрать другой тип", callback_data="recipe_back_to_type")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(RecipeStates.list_type, F.data.startswith("recipe_type_"))
async def recipe_list(callback: types.CallbackQuery, state: FSMContext):
    result_type = callback.data.split("_")[2]
    await state.update_data(recipe_result_type=result_type, recipe_page=1)
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await callback.message.edit_text(f"Рецепты для {result_type.upper()}:", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_page_"))
async def recipe_list_page(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    result_type = parts[2]
    page = int(parts[3])
    await state.update_data(recipe_result_type=result_type, recipe_page=page)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты для {result_type.upper()}:", reply_markup=keyboard)

@admin_router.callback_query(RecipeStates.list_page, F.data == "recipe_back_to_type")
async def recipe_back_to_type(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)

async def show_recipe(target, recipe: dict, state: FSMContext):
    if recipe['result_type'] == 'gear':
        gear = await db.get_gear_by_id(recipe['result_id'])
        result_info = f"{gear['emoji']} {gear['name']}" if gear else f"ID {recipe['result_id']}"
    else:
        res = await db.get_resource_by_id(recipe['result_id'])
        result_info = f"{res['emoji']} {res['name']}" if res else f"ID {recipe['result_id']}"
    text = f"📜 Рецепт ID {recipe['id']}\n🎁 Результат: {result_info} (количество: {recipe['quantity']})\n\n"
    text += "<b>Ингредиенты:</b>\n"
    for ing in recipe['ingredients']:
        text += f"  {ing['emoji']} {ing['name']} — {ing['quantity']} шт.\n"
    if not recipe['ingredients']:
        text += "<i>Нет ингредиентов</i>\n"

    if recipe['result_type'] == 'gear':
        text += "\n👥 <b>Владельцы:</b>\n"
        for owner in recipe['owners']:
            text += f"  @{clean_username(owner)}\n"
        if not recipe['owners']:
            text += "<i>Нет владельцев</i>\n"

    keyboard = []
    if recipe['result_type'] == 'gear':
        keyboard.append([InlineKeyboardButton(text="👤 Добавить владельца", callback_data="recipe_add_owner")])
    keyboard.append([InlineKeyboardButton(text="➕ Добавить ингредиент", callback_data="recipe_add_ingredient")])
    keyboard.append([InlineKeyboardButton(text="✏️ Редактировать ингредиенты", callback_data="recipe_edit_ingredients")])
    keyboard.append([InlineKeyboardButton(text="❌ Удалить рецепт", callback_data="recipe_delete")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="recipe_back_to_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])

    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.view_recipe)

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_view_"))
async def recipe_view(callback: types.CallbackQuery, state: FSMContext):
    recipe_id = int(callback.data.split("_")[2])
    recipe = await db.get_recipe_details(recipe_id)
    if not recipe:
        await callback.message.edit_text("Рецепт не найден.")
        return
    await state.update_data(recipe_id=recipe_id, recipe_result_type=recipe['result_type'])
    await show_recipe(callback, recipe, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_back_to_list")
async def recipe_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    result_type = data.get('recipe_result_type', 'gear')
    page = data.get('recipe_page', 1)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты для {result_type.upper()}:", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_add_"))
async def recipe_add_choose_item(callback: types.CallbackQuery, state: FSMContext):
    result_type = callback.data.split("_")[2]
    await state.update_data(new_recipe_type=result_type)
    if result_type == 'gear':
        all_items = await db.get_all_gear_simple()
        existing = await db.execute_query("SELECT result_id FROM recipes WHERE result_type='gear'")
    else:
        all_items = await db.get_all_resources_simple()
        existing = await db.execute_query("SELECT result_id FROM recipes WHERE result_type='resource'")
    existing_ids = {e['result_id'] for e in existing}
    available = [it for it in all_items if it['id'] not in existing_ids]
    if not available:
        await callback.message.edit_text("Для всех элементов уже есть рецепты.")
        return
    keyboard = [[InlineKeyboardButton(text=f"{it['emoji']} {it['name']}", callback_data=f"recipe_new_target_{it['id']}")] for it in available]
    await callback.message.edit_text("Выберите элемент для создания рецепта:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.add_confirm)

@admin_router.callback_query(RecipeStates.add_confirm, F.data.startswith("recipe_new_target_"))
async def recipe_create(callback: types.CallbackQuery, state: FSMContext):
    result_id = int(callback.data.split("_")[3])
    data = await state.get_data()
    result_type = data['new_recipe_type']
    try:
        recipe_id = await db.create_recipe(result_type, result_id, 1)
        await callback.message.edit_text(f"✅ Рецепт создан (ID {recipe_id}). Теперь добавьте ингредиенты и владельцев.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        return
    recipe = await db.get_recipe_details(recipe_id)
    await state.update_data(recipe_id=recipe_id, recipe_result_type=result_type, recipe_page=1)
    await show_recipe(callback, recipe, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_ingredient")
async def recipe_add_ingredient_select(callback: types.CallbackQuery, state: FSMContext):
    resources = await db.get_all_resources_simple()
    if not resources:
        await callback.answer("Нет ресурсов", show_alert=True)
        return
    await state.update_data(ingredient_resources=resources, ingredient_page=1)
    await show_ingredient_page(callback, resources, 1, state)

async def show_ingredient_page(target, resources, page, state):
    from admin_utils import ADMIN_ITEMS_PER_PAGE
    per_page = ADMIN_ITEMS_PER_PAGE
    start = (page-1)*per_page
    end = start+per_page
    page_items = resources[start:end]
    has_next = end < len(resources)
    keyboard = []
    for r in page_items:
        keyboard.append([InlineKeyboardButton(text=f"{r['emoji']} {r['name']}", callback_data=f"recipe_ing_select_{r['id']}_{page}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"recipe_ing_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"recipe_ing_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Готово", callback_data="recipe_finish_adding")])
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text("Выберите ресурс для добавления в ингредиенты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await target.answer("Выберите ресурс:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.add_ingredient)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_page_"))
async def recipe_ing_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[3])
    data = await state.get_data()
    resources = data.get('ingredient_resources')
    if not resources:
        await callback.answer("Ошибка", show_alert=True)
        return
    await show_ingredient_page(callback, resources, page, state)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_select_"))
async def recipe_ing_quantity(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    resource_id = int(parts[3])
    page = int(parts[4]) if len(parts)>4 else 1
    await state.update_data(temp_resource_id=resource_id, ingredient_return_page=page, edit_action='add')
    await callback.message.edit_text("Введите количество (целое число):")
    await state.set_state(RecipeStates.edit_ingredient_quantity)

@admin_router.message(RecipeStates.edit_ingredient_quantity, F.text)
async def recipe_ing_save_quantity(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty <= 0: raise ValueError
    except:
        await message.answer("Введите положительное целое число.")
        return
    data = await state.get_data()
    recipe_id = data['recipe_id']
    action = data.get('edit_action')
    if action == 'add':
        resource_id = data['temp_resource_id']
        await db.add_ingredient(recipe_id, resource_id, qty)
        await message.answer("✅ Ингредиент добавлен. Выберите следующий или нажмите 'Готово'.")
        resources = data.get('ingredient_resources')
        page = data.get('ingredient_return_page', 1)
        if resources:
            await show_ingredient_page(message, resources, page, state)
            return
    elif action == 'change':
        resource_id = data['edit_resource_id']
        await db.update_ingredient(recipe_id, resource_id, qty)
        await message.answer("✅ Количество обновлено.")
    else:
        await message.answer("Ошибка.")
        return
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(message, recipe, state)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data == "recipe_finish_adding")
async def recipe_finish_adding(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_owner")
async def recipe_add_owner_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data.get('recipe_id')
    recipe = await db.get_recipe_details(recipe_id)
    if recipe and recipe['result_type'] != 'gear':
        await callback.answer("Владельцы добавляются только для рецептов снаряжения.", show_alert=True)
        return
    await callback.message.edit_text("Введите username владельца (без @):")
    await state.set_state(RecipeStates.add_owner)

@admin_router.message(RecipeStates.add_owner, F.text)
async def recipe_add_owner_save(message: types.Message, state: FSMContext):
    username = message.text.strip().lstrip('@')
    if not username:
        await message.answer("Имя не может быть пустым.")
        return
    data = await state.get_data()
    recipe_id = data['recipe_id']
    try:
        await db.add_recipe_owner(recipe_id, username)
        await message.answer(f"✅ Владелец @{username} добавлен.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(message, recipe, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_edit_ingredients")
async def recipe_edit_ingredients_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    if not recipe['ingredients']:
        await callback.answer("Нет ингредиентов", show_alert=True)
        return
    keyboard = []
    for ing in recipe['ingredients']:
        keyboard.append([InlineKeyboardButton(text=f"{ing['emoji']} {ing['name']} — {ing['quantity']} шт.", callback_data=f"recipe_edit_ing_{ing['resource_id']}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к рецепту", callback_data="recipe_back_to_view")])
    await callback.message.edit_text("Выберите ингредиент для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.edit_ingredient)

@admin_router.callback_query(RecipeStates.edit_ingredient, F.data.startswith("recipe_edit_ing_"))
async def recipe_edit_ing_options(callback: types.CallbackQuery, state: FSMContext):
    resource_id = int(callback.data.split("_")[3])
    await state.update_data(edit_resource_id=resource_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить количество", callback_data="recipe_ing_change")],
        [InlineKeyboardButton(text="❌ Удалить ингредиент", callback_data="recipe_ing_delete")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="recipe_back_to_edit_list")]
    ])
    await callback.message.edit_text("Что сделать?", reply_markup=keyboard)
    await state.set_state(RecipeStates.edit_ingredient_quantity)

@admin_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_change")
async def recipe_ing_change_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новое количество:")
    await state.update_data(edit_action='change')

@admin_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_delete")
async def recipe_ing_delete(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    resource_id = data['edit_resource_id']
    await db.remove_ingredient(recipe_id, resource_id)
    await callback.answer("Ингредиент удалён", show_alert=True)
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)

@admin_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_back_to_edit_list")
async def recipe_back_to_edit_list(callback: types.CallbackQuery, state: FSMContext):
    await recipe_edit_ingredients_list(callback, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_back_to_view")
async def recipe_back_to_view(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_delete")
async def recipe_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="recipe_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_back_to_view")]
    ])
    await callback.message.edit_text("Удалить рецепт?", reply_markup=keyboard)
    await state.set_state(RecipeStates.delete_confirm)

@admin_router.callback_query(RecipeStates.delete_confirm, F.data == "recipe_delete_yes")
async def recipe_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    result_type = data.get('recipe_result_type', 'gear')
    await db.delete_recipe(recipe_id)
    await callback.message.edit_text("✅ Рецепт удалён.")
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await callback.message.answer(f"Рецепты для {result_type.upper()}:", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

# ============================================================
# Регистрация универсальных обработчиков (CRUD)
# ============================================================
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
