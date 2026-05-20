import os
import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]
ADMIN_ITEMS_PER_PAGE = 10

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

def is_valid_emoji(s: str) -> bool:
    if not s or len(s) > 2:
        return False
    return all(not ch.isalnum() for ch in s)

def clean_username(username: str) -> str:
    return username.lstrip('@') if username else ''

# ============================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ CRUD
# ============================================================

async def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить моба", callback_data="admin_add_mob")],
        [InlineKeyboardButton(text="✏️ Редактировать моба", callback_data="admin_edit_mob")],
        [InlineKeyboardButton(text="🗑 Удалить моба", callback_data="admin_delete_mob")],
        [InlineKeyboardButton(text="📦 Управление ресурсами", callback_data="admin_manage_resources")],
        [InlineKeyboardButton(text="⚔️ Управление снаряжением", callback_data="admin_manage_gear")],
        [InlineKeyboardButton(text="📜 Управление рецептами", callback_data="admin_manage_recipes")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@admin_router.message(Command("kombat"))
async def admin_panel(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("🔧 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML",
                         reply_markup=await get_admin_main_keyboard())

@admin_router.callback_query(F.data == "admin_close")
async def admin_close(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@admin_router.callback_query(F.data == "admin_cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# -----------------------------------------------------------------
# Универсальные строители клавиатур и обработчики списков
# -----------------------------------------------------------------

def build_paginated_keyboard(items, page, has_next, item_callback_prefix, extra_buttons=None):
    """Строит клавиатуру со списком кнопок + навигация + доп. кнопки"""
    keyboard = []
    for item in items:
        text = f"{item.get('emoji', '')} {item['name']}" + (f" (ID {item['id']})" if 'id' in item else "")
        if 'rarity' in item:
            text += f" [{item['rarity']}]"
        if 'slot' in item:
            text += f" ({item['slot']})"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"{item_callback_prefix}_{item['id']}")])
    
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"page_{page+1}"))
    if nav:
        keyboard.append(nav)
    
    if extra_buttons:
        for btn_row in extra_buttons:
            keyboard.append(btn_row)
    
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def render_entity_list(callback, state, entity_config, page=1):
    """Универсальный рендер списка сущностей"""
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items = await entity_config['get_page_func'](offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    
    extra = []
    if entity_config.get('add_button'):
        extra.append([InlineKeyboardButton(text=entity_config['add_button_text'], callback_data=entity_config['add_callback'])])
    
    keyboard = build_paginated_keyboard(
        items, page, has_next,
        entity_config['item_callback_prefix'],
        extra_buttons=extra
    )
    await callback.message.edit_text(entity_config['list_title'], reply_markup=keyboard)
    await state.update_data(entity_type=entity_config['name'], current_page=page)
    await state.set_state(entity_config['list_state'])
    await callback.answer()

# -----------------------------------------------------------------
# Универсальные обработчики для редактирования полей
# -----------------------------------------------------------------

class GenericEditStates(StatesGroup):
    select_item = State()
    select_field = State()
    new_value = State()
    confirm_delete = State()

async def start_edit_entity(callback, state, entity_config):
    """Запуск процесса редактирования: показать список элементов"""
    await state.clear()
    await state.update_data(entity_config=entity_config['name'])
    await render_entity_list(callback, state, entity_config, 1)

async def show_edit_menu(callback, state, entity_id, entity_config, entity_data):
    """Показывает меню редактирования для конкретного элемента"""
    fields = entity_config['edit_fields']
    keyboard = []
    for field_name, field_label in fields:
        current_value = entity_data.get(field_name, '?')
        keyboard.append([InlineKeyboardButton(
            text=f"{field_label}: {current_value}",
            callback_data=f"edit_field_{field_name}"
        )])
    if entity_config.get('extra_edit_buttons'):
        for btn in entity_config['extra_edit_buttons'](entity_id):
            keyboard.append(btn)
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_entity")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    
    await callback.message.edit_text(
        f"Редактирование {entity_config['name_ru']} ID {entity_id}:\n{entity_config['display_format'](entity_data)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
    )
    await state.update_data(entity_id=entity_id, editing_entity=entity_config['name'])
    await state.set_state(GenericEditStates.select_field)

# -----------------------------------------------------------------
# УНИВЕРСАЛЬНЫЕ ОБРАБОТЧИКИ (Callback'и)
# -----------------------------------------------------------------

@admin_router.callback_query(GenericEditStates.select_field, F.data.startswith("edit_field_"))
async def generic_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[2]
    await state.update_data(edit_field=field)
    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(GenericEditStates.new_value)
    await callback.answer()

@admin_router.message(GenericEditStates.new_value, F.text)
async def generic_update_field(message: types.Message, state: FSMContext):
    data = await state.get_data()
    entity_type = data['editing_entity']
    entity_id = data['entity_id']
    field = data['edit_field']
    new_value = message.text.strip()
    
    # Валидация в зависимости от поля и типа сущности
    config = ENTITY_CONFIGS[entity_type]
    if field in config.get('integer_fields', []):
        try:
            new_value = int(new_value)
            if new_value < 0:
                raise ValueError
        except:
            await message.answer("Ошибка: введите положительное целое число.")
            return
    if field == 'emoji' and not is_valid_emoji(new_value):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра).")
        return
    if field == 'name' and not new_value:
        await message.answer("Название не может быть пустым.")
        return
    if field == 'rarity' and new_value not in ('common','rare','epic'):
        await message.answer("Редкость должна быть common, rare или epic.")
        return
    if field == 'type' and new_value not in ('craft','consumable','scroll_recipe','currency'):
        await message.answer("Неверный тип ресурса.")
        return
    
    # Выполняем обновление через конфигурационную функцию
    try:
        await config['update_field_func'](entity_id, field, new_value)
        await message.answer(f"✅ Поле <b>{field}</b> обновлено на <code>{new_value}</code>.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    
    # Показать обновлённое меню
    entity_data = await config['get_by_id_func'](entity_id)
    if not entity_data:
        await message.answer("Сущность не найдена. Возврат в список.")
        await render_entity_list(message, state, config, 1)
        return
    await show_edit_menu(message, state, entity_id, config, entity_data)

@admin_router.callback_query(GenericEditStates.select_field, F.data == "delete_entity")
async def generic_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    entity_type = data['editing_entity']
    entity_id = data['entity_id']
    config = ENTITY_CONFIGS[entity_type]
    entity = await config['get_by_id_func'](entity_id)
    if not entity:
        await callback.message.edit_text("Сущность не найдена.")
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_list")]
    ])
    await callback.message.edit_text(
        f"⚠️ Удалить {config['name_ru']} <b>{entity['name']}</b> (ID {entity_id})?\nЭто действие необратимо.",
        parse_mode="HTML", reply_markup=keyboard
    )
    await state.set_state(GenericEditStates.confirm_delete)
    await callback.answer()

@admin_router.callback_query(GenericEditStates.confirm_delete, F.data == "confirm_delete_yes")
async def generic_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    entity_type = data['editing_entity']
    entity_id = data['entity_id']
    config = ENTITY_CONFIGS[entity_type]
    try:
        await config['delete_func'](entity_id)
        await callback.message.edit_text("✅ Успешно удалено.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await render_entity_list(callback, state, config, 1)

@admin_router.callback_query(GenericEditStates.select_field, F.data == "back_to_list")
async def generic_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    entity_type = data.get('editing_entity')
    if entity_type in ENTITY_CONFIGS:
        await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], 1)
    else:
        await callback.message.edit_text("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# -----------------------------------------------------------------
# КОНФИГУРАЦИИ СУЩНОСТЕЙ
# -----------------------------------------------------------------

ENTITY_CONFIGS = {}

# ------ Ресурсы ------
class ResourceListStates(StatesGroup):
    list_page = State()

async def resource_get_page(offset, limit):
    return await db.get_resources_page(offset, limit)

async def resource_update_field(res_id, field, value):
    if field == 'name':
        await db.update_resource(res_id, value, None, None)
    elif field == 'emoji':
        current = await db.get_resource_by_id(res_id)
        await db.update_resource(res_id, current['name'], value, None)
    elif field == 'type':
        current = await db.get_resource_by_id(res_id)
        await db.update_resource(res_id, current['name'], current['emoji'], value)

async def resource_get_by_id(res_id):
    return await db.get_resource_by_id(res_id)

async def resource_delete(res_id):
    await db.delete_resource(res_id)

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
        ('type', '🏷 Тип')
    ],
    'integer_fields': [],
    'display_format': lambda d: f"{d.get('emoji','')} {d.get('name','')} (тип: {d.get('type','craft')})"
}

# ------ Снаряжение ------
class GearListStates(StatesGroup):
    list_page = State()

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
    'display_format': lambda d: f"{d.get('emoji','')} {d.get('name','')} [{d.get('rarity','')}]"
}

# -----------------------------------------------------------------
# ОБРАБОТЧИКИ ДЛЯ РЕСУРСОВ (ДОБАВЛЕНИЕ, РЕДАКТИРОВАНИЕ)
# -----------------------------------------------------------------

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

@admin_router.message(ResourceAddStates.name, F.text)
async def resource_add_emoji(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(res_name=name)
    await message.answer("Введите эмодзи (один символ):")
    await state.set_state(ResourceAddStates.emoji)

@admin_router.message(ResourceAddStates.emoji, F.text)
async def resource_add_type(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ.")
        return
    await state.update_data(res_emoji=emoji)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Для крафта", callback_data="res_type_craft")],
        [InlineKeyboardButton(text="✨ Расходуемый", callback_data="res_type_consumable")],
        [InlineKeyboardButton(text="📜 Рецепт экипировки", callback_data="res_type_scroll_recipe")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="res_type_currency")]
    ])
    await message.answer("Выберите тип ресурса:", reply_markup=keyboard)
    await state.set_state(ResourceAddStates.type)

@admin_router.callback_query(ResourceAddStates.type, F.data.startswith("res_type_"))
async def resource_save(callback: types.CallbackQuery, state: FSMContext):
    type_map = {"craft": "craft", "consumable": "consumable", "scroll_recipe": "scroll_recipe", "currency": "currency"}
    resource_type = type_map.get(callback.data.split("_")[2], "craft")
    data = await state.get_data()
    try:
        await db.add_resource(data['res_name'], data['res_emoji'], resource_type)
        await callback.message.edit_text("✅ Ресурс добавлен.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS['resource'], 1)

# -----------------------------------------------------------------
# ОБРАБОТЧИКИ ДЛЯ СНАРЯЖЕНИЯ (ДОБАВЛЕНИЕ)
# -----------------------------------------------------------------

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
        [InlineKeyboardButton(text="🔵 Сверхредкое (epic)", callback_data="rarity_epic")]
    ])
    await message.answer("Выберите редкость:", reply_markup=keyboard)
    await state.set_state(GearAddStates.rarity)

@admin_router.callback_query(GearAddStates.rarity, F.data.startswith("rarity_"))
async def gear_add_slot(callback: types.CallbackQuery, state: FSMContext):
    rarity = callback.data.split("_")[1]
    await state.update_data(gear_rarity=rarity)
    slots = [
        ("оружие", "🗡 Оружие"), ("щит", "🛡 Щит"), ("голова", "🪖 Голова"),
        ("торс", "🦺 Торс"), ("руки", "🧤 Руки"), ("ноги", "🩳 Ноги"),
        ("спина", "🧣 Спина"), ("аксессуар", "📖 Аксессуар"), ("плечи", "🪹 Плечи")
    ]
    keyboard = [[InlineKeyboardButton(text=label, callback_data=f"slot_{name}")] for name, label in slots]
    await callback.message.edit_text("Выберите слот:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(GearAddStates.slot)

@admin_router.callback_query(GearAddStates.slot, F.data.startswith("slot_"))
async def gear_add_emoji(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split("_")[1]
    await state.update_data(gear_slot=slot)
    await callback.message.edit_text("Введите эмодзи (один символ):")
    await state.set_state(GearAddStates.emoji)

@admin_router.message(GearAddStates.emoji, F.text)
async def gear_save(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ.")
        return
    data = await state.get_data()
    try:
        await db.add_gear(data['gear_name'], data['gear_rarity'], data['gear_slot'], emoji)
        await message.answer("✅ Снаряжение добавлено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await render_entity_list(message, state, ENTITY_CONFIGS['gear'], 1)

# -----------------------------------------------------------------
# УПРАВЛЕНИЕ МОБАМИ (оставлено почти как было, но с использованием общих утилит для пагинации)
# -----------------------------------------------------------------
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
    field = callback.data.split("_")[3]
    await state.update_data(edit_field=field)
    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(MobStates.edit_new_value)

@admin_router.message(MobStates.edit_new_value, F.text)
async def mob_update_field(message: types.Message, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    field = data['edit_field']
    new_value = message.text.strip()
    if field in ('hp', 'dust_min', 'dust_max', 'exp', 'location_id'):
        try:
            new_value = int(new_value)
            if new_value < 0: raise ValueError
        except:
            await message.answer("Введите положительное целое число.")
            return
    if field == 'emoji' and not is_valid_emoji(new_value):
        await message.answer("Эмодзи должен быть один символ.")
        return
    if field == 'name' and not new_value:
        await message.answer("Имя не может быть пустым.")
        return
    try:
        await db.update_mob_field(mob_id, field, new_value)
        await message.answer(f"✅ Поле {field} обновлено.")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    # Показать меню редактирования заново
    mob = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await message.answer("Моб пропал.")
        await state.clear()
        return
    mob = mob[0]
    await state.update_data(mob_id=mob_id)
    fields = [(f, eval(f"mob['{f}']")) for f in ('name','emoji','hp','dust_min','dust_max','exp','location_id')]
    keyboard = [[InlineKeyboardButton(text=f"{label}: {value}", callback_data=f"mob_edit_field_{field}")] for field, (label, value) in zip(('name','emoji','hp','dust_min','dust_max','exp','location_id'), fields)]
    keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="mob_drop_menu")])
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить моба", callback_data="mob_delete")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_mob_list")])
    await message.answer(f"Редактирование моба ID {mob_id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(MobStates.edit_field)

@admin_router.callback_query(MobStates.edit_field, F.data == "back_to_mob_list")
async def mob_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    keyboard = await get_mob_list_keyboard(1)
    await callback.message.edit_text("Выберите моба для редактирования:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)

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
    await state.set_state(MobStates.edit_field)  # переиспользуем состояние, дальше обработаем

@admin_router.callback_query(F.data == "confirm_mob_delete")
async def mob_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    await db.delete_mob(mob_id)
    await callback.message.edit_text("✅ Моб удалён.")
    keyboard = await get_mob_list_keyboard(1)
    await callback.message.answer("Выберите моба для редактирования:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)

# ==================== УПРАВЛЕНИЕ ДРОПОМ ====================
async def get_drop_list_keyboard(mob_id: int, category: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    if category == 'resource':
        items = await db.get_resources_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
        has_next = len(items) > ADMIN_ITEMS_PER_PAGE
        items = items[:ADMIN_ITEMS_PER_PAGE]
    elif category == 'gear':
        # Изменено: теперь получаем и common, и rare экипировку
        items = await db.execute_query(
            "SELECT id, name, emoji, slot FROM gear WHERE rarity IN ('common', 'rare') ORDER BY id LIMIT ? OFFSET ?",
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
        if category == 'gear' and 'slot' in item:
            text += f" ({item['slot']})"
        callback_data = f"drop_toggle_{category}_{item['id']}_{page}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=callback_data)])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"drop_page_{category}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"drop_page_{category}_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data="back_to_drop_categories")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(MobStates.edit_field, F.data == "mob_drop_menu")
async def mob_drop_category(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка (common/rare)", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_mob_list")]
    ])
    await callback.message.edit_text("Выберите категорию дропа:", reply_markup=keyboard)
    await state.set_state(MobStates.drop_category)

@admin_router.callback_query(MobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    data = await state.get_data()
    mob_id = data['mob_id']
    keyboard = await get_drop_list_keyboard(mob_id, category, 1)
    await callback.message.edit_text(f"Управление дропом: {category}\n✅ - падает, ❌ - не падает", reply_markup=keyboard)
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
    mob_id = data['mob_id']
    has_drop = await db.get_drop_status(mob_id, category, item_id)
    if has_drop:
        await db.remove_drop(mob_id, category, item_id)
    else:
        await db.add_drop(mob_id, category, item_id)
    keyboard = await get_drop_list_keyboard(mob_id, category, page)
    await callback.message.edit_text(f"Управление дропом: {category}", reply_markup=keyboard)

@admin_router.callback_query(MobStates.drop_list_page, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext):
    await mob_drop_category(callback, state)

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
    await message.answer("Введите эмодзи моба (один символ):")
    await state.set_state(MobStates.add_emoji)

@admin_router.message(MobStates.add_emoji, F.text)
async def add_mob_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть один символ.")
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
    await callback.message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# ---------- Удаление моба (через отдельный пункт меню) ----------
class DeleteMobStates(StatesGroup):
    select = State()
    confirm = State()

async def get_delete_mob_keyboard(page):
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
    await callback.message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# ==================== УПРАВЛЕНИЕ РЕЦЕПТАМИ (оставлено почти без изменений, чтобы не сломать) ====================
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
        text = f"{r['result_emoji']} {r['result_name']} (ID рец.{r['id']}) | ингр:{r['ingredient_count']} влад:{r['owner_count']}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"recipe_view_{r['id']}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"recipe_page_{result_type}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"recipe_page_{result_type}_{page+1}"))
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
    text += "\n👥 <b>Владельцы:</b>\n"
    for owner in recipe['owners']:
        text += f"  @{clean_username(owner)}\n"
    if not recipe['owners']:
        text += "<i>Нет владельцев</i>\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить ингредиент", callback_data="recipe_add_ingredient")],
        [InlineKeyboardButton(text="👤 Добавить владельца", callback_data="recipe_add_owner")],
        [InlineKeyboardButton(text="✏️ Редактировать ингредиенты", callback_data="recipe_edit_ingredients")],
        [InlineKeyboardButton(text="❌ Удалить рецепт", callback_data="recipe_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="recipe_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=keyboard)
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
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"recipe_ing_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"recipe_ing_page_{page+1}"))
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
