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

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()
ADMIN_ITEMS_PER_PAGE = 10

def is_valid_emoji(s: str) -> bool:
    if not s:
        return False
    if len(s) > 2:
        return False
    return all(not ch.isalnum() for ch in s)

def clean_username(username: str) -> str:
    """Убирает @ в начале, если есть."""
    return username.lstrip('@') if username else ''

# ==================== ГЛАВНОЕ МЕНЮ ====================
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

# ==================== УПРАВЛЕНИЕ РЕСУРСАМИ ====================
class ResourceStates(StatesGroup):
    list_page = State()
    add_name = State()
    add_emoji = State()
    add_type = State()
    edit_select = State()
    edit_name = State()
    edit_emoji = State()
    edit_type = State()
    delete_confirm = State()

async def get_resources_list_keyboard(page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    resources = await db.get_resources_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(resources) > ADMIN_ITEMS_PER_PAGE
    resources = resources[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    type_emoji_map = {
        'craft': '📦',
        'consumable': '✨',
        'scroll_recipe': '📜',
        'currency': '💰'
    }
    for res in resources:
        t_emoji = type_emoji_map.get(res.get('type', 'craft'), '📦')
        keyboard.append([InlineKeyboardButton(
            text=f"{t_emoji} {res['emoji']} {res['name']} (ID {res['id']})",
            callback_data=f"resource_edit_{res['id']}"
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"resource_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"resource_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="➕ Добавить ресурс", callback_data="resource_add")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(F.data == "admin_manage_resources")
async def manage_resources(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = await get_resources_list_keyboard(1)
    await callback.message.edit_text("📦 Управление ресурсами:\nВыберите ресурс для редактирования или добавьте новый:", reply_markup=keyboard)
    await state.set_state(ResourceStates.list_page)
    await callback.answer()

@admin_router.callback_query(ResourceStates.list_page, F.data.startswith("resource_page_"))
async def resource_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[2])
    keyboard = await get_resources_list_keyboard(page)
    await callback.message.edit_text("📦 Управление ресурсами:\nВыберите ресурс для редактирования или добавьте новый:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(ResourceStates.list_page, F.data == "resource_add")
async def resource_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название нового ресурса:")
    await state.set_state(ResourceStates.add_name)
    await callback.answer()

@admin_router.message(ResourceStates.add_name, F.text)
async def resource_add_emoji(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова:")
        return
    await state.update_data(res_name=name)
    await message.answer("Теперь введите эмодзи для ресурса (один символ, например 🍎):")
    await state.set_state(ResourceStates.add_emoji)

@admin_router.message(ResourceStates.add_emoji, F.text)
async def resource_add_emoji_save(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра). Попробуйте снова:")
        return
    await state.update_data(res_emoji=emoji)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Для крафта", callback_data="res_type_craft")],
        [InlineKeyboardButton(text="✨ Расходуемый (свиток усиления)", callback_data="res_type_consumable")],
        [InlineKeyboardButton(text="📜 Рецепт экипировки (свиток)", callback_data="res_type_scroll_recipe")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="res_type_currency")]
    ])
    await message.answer("Выберите тип ресурса:", reply_markup=keyboard)
    await state.set_state(ResourceStates.add_type)

@admin_router.callback_query(ResourceStates.add_type, F.data.startswith("res_type_"))
async def resource_save_type(callback: types.CallbackQuery, state: FSMContext):
    type_map = {
        "res_type_craft": "craft",
        "res_type_consumable": "consumable",
        "res_type_scroll_recipe": "scroll_recipe",
        "res_type_currency": "currency"
    }
    resource_type = type_map.get(callback.data, "craft")
    data = await state.get_data()
    name = data['res_name']
    emoji = data['res_emoji']
    try:
        await db.add_resource(name, emoji, resource_type)
        await callback.message.edit_text(f"✅ Ресурс <b>{name}</b> добавлен (тип: {resource_type}).", parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_resources_list_keyboard(1)
    await callback.message.answer("📦 Управление ресурсами:", reply_markup=keyboard)
    await state.set_state(ResourceStates.list_page)
    await callback.answer()

@admin_router.callback_query(ResourceStates.list_page, F.data.startswith("resource_edit_"))
async def resource_edit_menu(callback: types.CallbackQuery, state: FSMContext):
    resource_id = int(callback.data.split("_")[2])
    res = await db.get_resource_by_id(resource_id)
    if not res:
        await callback.message.edit_text("Ресурс не найден.")
        await callback.answer()
        return
    await state.update_data(res_id=resource_id, res_name=res['name'], res_emoji=res['emoji'], res_type=res.get('type', 'craft'))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🏷 Изменить тип", callback_data="resource_edit_type")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await callback.message.edit_text(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']}, тип: {res.get('type', 'craft')})\nЧто хотите сделать?", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_select)
    await callback.answer()

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_back_to_list")
async def resource_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    keyboard = await get_resources_list_keyboard(1)
    await callback.message.edit_text("📦 Управление ресурсами:", reply_markup=keyboard)
    await state.set_state(ResourceStates.list_page)
    await callback.answer()

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_edit_name")
async def resource_edit_name_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новое название ресурса:")
    await state.set_state(ResourceStates.edit_name)
    await callback.answer()

@admin_router.message(ResourceStates.edit_name, F.text)
async def resource_update_name(message: types.Message, state: FSMContext):
    new_name = message.text.strip()
    if not new_name:
        await message.answer("Название не может быть пустым. Попробуйте снова:")
        return
    data = await state.get_data()
    res_id = data['res_id']
    current_emoji = data['res_emoji']
    try:
        await db.update_resource(res_id, new_name, current_emoji, None)
        await message.answer(f"✅ Название ресурса обновлено на <b>{new_name}</b>.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    res = await db.get_resource_by_id(res_id)
    if not res:
        await message.answer("Ресурс пропал. Возврат в список.")
        keyboard = await get_resources_list_keyboard(1)
        await message.answer("📦 Управление ресурсами:", reply_markup=keyboard)
        await state.set_state(ResourceStates.list_page)
        return
    await state.update_data(res_name=res['name'], res_emoji=res['emoji'], res_type=res['type'])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🏷 Изменить тип", callback_data="resource_edit_type")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await message.answer(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']}, тип: {res['type']})\nЧто хотите сделать?", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_select)

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_edit_emoji")
async def resource_edit_emoji_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новый эмодзи для ресурса (один символ):")
    await state.set_state(ResourceStates.edit_emoji)
    await callback.answer()

@admin_router.message(ResourceStates.edit_emoji, F.text)
async def resource_update_emoji(message: types.Message, state: FSMContext):
    new_emoji = message.text.strip()
    if not is_valid_emoji(new_emoji):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра). Попробуйте снова:")
        return
    data = await state.get_data()
    res_id = data['res_id']
    current_name = data['res_name']
    try:
        await db.update_resource(res_id, current_name, new_emoji, None)
        await message.answer(f"✅ Эмодзи ресурса обновлён на {new_emoji}.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    res = await db.get_resource_by_id(res_id)
    if not res:
        await message.answer("Ресурс пропал. Возврат в список.")
        keyboard = await get_resources_list_keyboard(1)
        await message.answer("📦 Управление ресурсами:", reply_markup=keyboard)
        await state.set_state(ResourceStates.list_page)
        return
    await state.update_data(res_name=res['name'], res_emoji=res['emoji'], res_type=res['type'])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🏷 Изменить тип", callback_data="resource_edit_type")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await message.answer(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']}, тип: {res['type']})\nЧто хотите сделать?", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_select)

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_edit_type")
async def resource_edit_type_prompt(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Для крафта", callback_data="res_update_type_craft")],
        [InlineKeyboardButton(text="✨ Расходуемый", callback_data="res_update_type_consumable")],
        [InlineKeyboardButton(text="📜 Рецепт экипировки", callback_data="res_update_type_scroll_recipe")],
        [InlineKeyboardButton(text="💰 Валюта", callback_data="res_update_type_currency")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="resource_back_to_list")]
    ])
    await callback.message.edit_text("Выберите новый тип ресурса:", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_type)
    await callback.answer()

@admin_router.callback_query(ResourceStates.edit_type, F.data.startswith("res_update_type_"))
async def resource_update_type(callback: types.CallbackQuery, state: FSMContext):
    type_map = {
        "res_update_type_craft": "craft",
        "res_update_type_consumable": "consumable",
        "res_update_type_scroll_recipe": "scroll_recipe",
        "res_update_type_currency": "currency"
    }
    new_type = type_map.get(callback.data, "craft")
    data = await state.get_data()
    res_id = data['res_id']
    current_name = data['res_name']
    current_emoji = data['res_emoji']
    try:
        await db.update_resource(res_id, current_name, current_emoji, new_type)
        await callback.message.edit_text(f"✅ Тип ресурса обновлён на <b>{new_type}</b>.", parse_mode="HTML")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        return
    res = await db.get_resource_by_id(res_id)
    if not res:
        await callback.message.edit_text("Ресурс пропал.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(res_type=res['type'])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🏷 Изменить тип", callback_data="resource_edit_type")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await callback.message.edit_text(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']}, тип: {res['type']})\nЧто хотите сделать?", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_select)
    await callback.answer()

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_delete")
async def resource_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    res_id = data['res_id']
    res_name = data['res_name']
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="resource_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="resource_back_to_list")]
    ])
    await callback.message.edit_text(f"⚠️ Вы уверены, что хотите удалить ресурс <b>{res_name}</b> (ID {res_id})?\nЭто также удалит его из дропов и рецептов.", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(ResourceStates.delete_confirm)
    await callback.answer()

@admin_router.callback_query(ResourceStates.delete_confirm, F.data == "resource_delete_yes")
async def resource_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    res_id = data['res_id']
    try:
        await db.delete_resource(res_id)
        await callback.message.edit_text("✅ Ресурс успешно удалён.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_resources_list_keyboard(1)
    await callback.message.answer("📦 Управление ресурсами:", reply_markup=keyboard)
    await state.set_state(ResourceStates.list_page)
    await callback.answer()

# ==================== ДОБАВЛЕНИЕ МОБА ====================
class AddMobStates(StatesGroup):
    name = State()
    emoji = State()
    hp = State()
    dust_min = State()
    dust_max = State()
    exp = State()
    location_id = State()

@admin_router.callback_query(F.data == "admin_add_mob")
async def start_add_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await callback.message.edit_text("Введите название моба (например, <b>Лесной волк</b>):", parse_mode="HTML")
    await state.set_state(AddMobStates.name)
    await callback.answer()

@admin_router.message(AddMobStates.name, F.text)
async def add_mob_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова:")
        return
    await state.update_data(name=name)
    await message.answer("Введите эмодзи моба (один символ, например 🐺):")
    await state.set_state(AddMobStates.emoji)

@admin_router.message(AddMobStates.emoji, F.text)
async def add_mob_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра). Попробуйте снова:")
        return
    await state.update_data(emoji=emoji)
    await message.answer("Введите HP (целое положительное число):")
    await state.set_state(AddMobStates.hp)

@admin_router.message(AddMobStates.hp, F.text)
async def add_mob_hp(message: types.Message, state: FSMContext):
    try:
        hp = int(message.text.strip())
        if hp < 0:
            raise ValueError
    except ValueError:
        await message.answer("Ошибка: введите целое положительное число.")
        return
    await state.update_data(hp=hp)
    await message.answer("Введите минимальное количество пыли (dust_min):")
    await state.set_state(AddMobStates.dust_min)

@admin_router.message(AddMobStates.dust_min, F.text)
async def add_mob_dust_min(message: types.Message, state: FSMContext):
    try:
        dust_min = int(message.text.strip())
        if dust_min < 0:
            raise ValueError
    except ValueError:
        await message.answer("Ошибка: введите целое положительное число.")
        return
    await state.update_data(dust_min=dust_min)
    await message.answer("Введите максимальное количество пыли (dust_max):")
    await state.set_state(AddMobStates.dust_max)

@admin_router.message(AddMobStates.dust_max, F.text)
async def add_mob_dust_max(message: types.Message, state: FSMContext):
    try:
        dust_max = int(message.text.strip())
        if dust_max < 0:
            raise ValueError
    except ValueError:
        await message.answer("Ошибка: введите целое положительное число.")
        return
    data = await state.get_data()
    if dust_max < data['dust_min']:
        await message.answer("dust_max не может быть меньше dust_min. Повторите:")
        return
    await state.update_data(dust_max=dust_max)
    await message.answer("Введите опыт (exp):")
    await state.set_state(AddMobStates.exp)

@admin_router.message(AddMobStates.exp, F.text)
async def add_mob_exp(message: types.Message, state: FSMContext):
    try:
        exp = int(message.text.strip())
        if exp < 0:
            raise ValueError
    except ValueError:
        await message.answer("Ошибка: введите целое положительное число.")
        return
    await state.update_data(exp=exp)
    locations = await db.get_locations()
    if not locations:
        await message.answer("❌ Нет локаций в БД. Сначала добавьте локации через SQL.")
        await state.clear()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{loc['emoji']} {loc['name']}", callback_data=f"loc_{loc['id']}")] for loc in locations
    ])
    await message.answer("Выберите локацию:", reply_markup=keyboard)
    await state.set_state(AddMobStates.location_id)

@admin_router.callback_query(AddMobStates.location_id, F.data.startswith("loc_"))
async def add_mob_location(callback: types.CallbackQuery, state: FSMContext):
    location_id = int(callback.data.split("_")[1])
    await state.update_data(location_id=location_id)
    data = await state.get_data()
    query = """
        INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """
    try:
        await db.execute_query(
            query,
            (data['name'], data['emoji'], data['hp'], data['dust_min'], data['dust_max'], data['exp'], location_id)
        )
        last_id = await db.execute_query("SELECT last_insert_rowid() as id")
        mob_id = last_id[0]['id']
        await callback.message.edit_text(
            f"✅ Моб <b>{data['name']}</b> добавлен (ID: {mob_id}).\n\n"
            "Теперь вы можете добавить дропы через редактирование.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка добавления моба")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await callback.message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# ==================== РЕДАКТИРОВАНИЕ МОБА ====================
class EditMobStates(StatesGroup):
    select_mob = State()
    select_field = State()
    new_value = State()
    drop_category = State()
    drop_list_page = State()

async def get_mob_selection_keyboard(page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    mobs = await db.execute_query(
        "SELECT id, name FROM mobs ORDER BY id LIMIT ? OFFSET ?",
        (ADMIN_ITEMS_PER_PAGE + 1, offset)
    )
    has_next = len(mobs) > ADMIN_ITEMS_PER_PAGE
    mobs = mobs[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for mob in mobs:
        keyboard.append([InlineKeyboardButton(
            text=f"{mob['name']} (ID {mob['id']})",
            callback_data=f"edit_mob_{mob['id']}"
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"edit_mob_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"edit_mob_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(F.data == "admin_edit_mob")
async def start_edit_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.update_data(edit_page=1)
    keyboard = await get_mob_selection_keyboard(1)
    await callback.message.edit_text("Выберите моба для редактирования:", reply_markup=keyboard)
    await state.set_state(EditMobStates.select_mob)
    await callback.answer()

@admin_router.callback_query(EditMobStates.select_mob, F.data.startswith("edit_mob_page_"))
async def edit_mob_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[3])
    await state.update_data(edit_page=page)
    keyboard = await get_mob_selection_keyboard(page)
    await callback.message.edit_text("Выберите моба для редактирования:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(EditMobStates.select_mob, F.data.startswith("edit_mob_") & ~F.data.startswith("edit_mob_page_"))
async def select_mob_for_edit(callback: types.CallbackQuery, state: FSMContext):
    mob_id = int(callback.data.split("_")[2])
    mob = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    mob = mob[0]
    await state.update_data(mob_id=mob_id, mob_name=mob['name'])
    fields = [
        ("name", f"Имя: {mob['name']}"),
        ("emoji", f"Эмодзи: {mob['emoji']}"),
        ("hp", f"HP: {mob['hp']}"),
        ("dust_min", f"Пыль мин: {mob['dust_min']}"),
        ("dust_max", f"Пыль макс: {mob['dust_max']}"),
        ("exp", f"Опыт: {mob['exp']}"),
        ("location_id", f"ID локации: {mob['location_id']}")
    ]
    keyboard = []
    for field, label in fields:
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"edit_field_{field}")])
    keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="edit_drop_menu")])
    keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(f"Редактирование моба ID {mob_id}\nВыберите поле или управление дропом:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(EditMobStates.select_field)
    await callback.answer()

@admin_router.callback_query(EditMobStates.select_field, F.data.startswith("edit_field_"))
async def select_field_to_edit(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[2]
    await state.update_data(edit_field=field)
    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(EditMobStates.new_value)
    await callback.answer()

@admin_router.message(EditMobStates.new_value, F.text)
async def set_new_value(message: types.Message, state: FSMContext):
    new_value = message.text.strip()
    data = await state.get_data()
    mob_id = data['mob_id']
    field = data['edit_field']
    if field in ('hp', 'dust_min', 'dust_max', 'exp', 'location_id'):
        try:
            new_value = int(new_value)
            if new_value < 0:
                raise ValueError
        except ValueError:
            await message.answer("Ошибка: поле должно быть положительным целым числом. Попробуйте снова:")
            return
    if field == 'emoji' and not is_valid_emoji(new_value):
        await message.answer("Ошибка: эмодзи должен быть ровно один символ (не буква и не цифра).")
        return
    if field == 'name' and not new_value:
        await message.answer("Имя не может быть пустым.")
        return
    try:
        await db.update_mob_field(mob_id, field, new_value)
        await message.answer(f"✅ Поле <b>{field}</b> успешно обновлено на <code>{new_value}</code>.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка обновления: {e}")
        return
    mob = await db.execute_query("SELECT * FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await message.answer("Моб пропал. Возврат в админку.")
        await state.clear()
        await message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
        return
    mob = mob[0]
    fields = [
        ("name", f"Имя: {mob['name']}"),
        ("emoji", f"Эмодзи: {mob['emoji']}"),
        ("hp", f"HP: {mob['hp']}"),
        ("dust_min", f"Пыль мин: {mob['dust_min']}"),
        ("dust_max", f"Пыль макс: {mob['dust_max']}"),
        ("exp", f"Опыт: {mob['exp']}"),
        ("location_id", f"ID локации: {mob['location_id']}")
    ]
    keyboard = []
    for f, label in fields:
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"edit_field_{f}")])
    keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="edit_drop_menu")])
    keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_cancel_edit")])
    await message.answer(f"Редактирование моба ID {mob_id}\nВыберите поле или управление дропом:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(EditMobStates.select_field)

# ==================== УПРАВЛЕНИЕ ДРОПОМ ====================
async def get_drop_list_keyboard(mob_id: int, category: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    if category == 'resource':
        items = await db.get_resources_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
        has_next = len(items) > ADMIN_ITEMS_PER_PAGE
        items = items[:ADMIN_ITEMS_PER_PAGE]
    elif category == 'gear':
        items = await db.get_common_gear_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
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

@admin_router.callback_query(EditMobStates.select_field, F.data == "edit_drop_menu")
async def drop_category_menu(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка (common)", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🔙 Назад к редактированию", callback_data="back_to_edit_mob")]
    ])
    await callback.message.edit_text("Выберите категорию дропа:", reply_markup=keyboard)
    await state.set_state(EditMobStates.drop_category)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_category, F.data == "back_to_edit_mob")
async def back_to_edit_mob_from_drop(callback: types.CallbackQuery, state: FSMContext):
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
        ("name", f"Имя: {mob['name']}"),
        ("emoji", f"Эмодзи: {mob['emoji']}"),
        ("hp", f"HP: {mob['hp']}"),
        ("dust_min", f"Пыль мин: {mob['dust_min']}"),
        ("dust_max", f"Пыль макс: {mob['dust_max']}"),
        ("exp", f"Опыт: {mob['exp']}"),
        ("location_id", f"ID локации: {mob['location_id']}")
    ]
    keyboard = []
    for field, label in fields:
        keyboard.append([InlineKeyboardButton(text=label, callback_data=f"edit_field_{field}")])
    keyboard.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="edit_drop_menu")])
    keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(f"Редактирование моба ID {mob_id}\nВыберите поле или управление дропом:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(EditMobStates.select_field)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    data = await state.get_data()
    mob_id = data['mob_id']
    await state.update_data(drop_category=category, drop_page=1)
    keyboard = await get_drop_list_keyboard(mob_id, category, 1)
    await callback.message.edit_text(f"Управление дропом: {category.upper()}\n✅ — уже падает, ❌ — не падает\nНажмите на предмет, чтобы добавить/удалить:", reply_markup=keyboard)
    await state.set_state(EditMobStates.drop_list_page)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_list_page, F.data.startswith("drop_page_"))
async def drop_list_page(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    category = parts[2]
    page = int(parts[3])
    data = await state.get_data()
    mob_id = data['mob_id']
    await state.update_data(drop_page=page)
    keyboard = await get_drop_list_keyboard(mob_id, category, page)
    await callback.message.edit_text(f"Управление дропом: {category.upper()}\n✅ — уже падает, ❌ — не падает\nНажмите на предмет, чтобы добавить/удалить:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_list_page, F.data.startswith("drop_toggle_"))
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
    await callback.message.edit_text(f"Управление дропом: {category.upper()}\n✅ — уже падает, ❌ — не падает\nНажмите на предмет, чтобы добавить/удалить:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_list_page, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext):
    await drop_category_menu(callback, state)

# ==================== УДАЛЕНИЕ МОБА ====================
class DeleteMobStates(StatesGroup):
    select_mob = State()
    confirm = State()

async def get_delete_mob_selection_keyboard(page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    mobs = await db.execute_query(
        "SELECT id, name FROM mobs ORDER BY id LIMIT ? OFFSET ?",
        (ADMIN_ITEMS_PER_PAGE + 1, offset)
    )
    has_next = len(mobs) > ADMIN_ITEMS_PER_PAGE
    mobs = mobs[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for mob in mobs:
        keyboard.append([InlineKeyboardButton(
            text=f"{mob['name']} (ID {mob['id']})",
            callback_data=f"del_mob_{mob['id']}"
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"delete_mob_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"delete_mob_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(F.data == "admin_delete_mob")
async def start_delete_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    await state.update_data(delete_page=1)
    keyboard = await get_delete_mob_selection_keyboard(1)
    await callback.message.edit_text("Выберите моба для УДАЛЕНИЯ:", reply_markup=keyboard)
    await state.set_state(DeleteMobStates.select_mob)
    await callback.answer()

@admin_router.callback_query(DeleteMobStates.select_mob, F.data.startswith("delete_mob_page_"))
async def delete_mob_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[3])
    await state.update_data(delete_page=page)
    keyboard = await get_delete_mob_selection_keyboard(page)
    await callback.message.edit_text("Выберите моба для УДАЛЕНИЯ:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(DeleteMobStates.select_mob, F.data.startswith("del_mob_"))
async def confirm_delete_mob(callback: types.CallbackQuery, state: FSMContext):
    mob_id = int(callback.data.split("_")[2])
    mob = await db.execute_query("SELECT name FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    mob_name = mob[0]['name']
    await state.update_data(mob_id=mob_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_edit")]
    ])
    await callback.message.edit_text(f"⚠️ Вы уверены, что хотите удалить моба <b>{mob_name}</b> (ID {mob_id})?\nЭто действие необратимо.", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(DeleteMobStates.confirm)
    await callback.answer()

@admin_router.callback_query(DeleteMobStates.confirm, F.data == "confirm_delete_yes")
async def delete_mob(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    try:
        await db.delete_mob(mob_id)
        await callback.message.edit_text("✅ Моб успешно удалён.")
    except Exception as e:
        logger.exception("Ошибка удаления моба")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await callback.message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# ==================== УПРАВЛЕНИЕ СНАРЯЖЕНИЕМ ====================
class GearStates(StatesGroup):
    list_page = State()
    add_name = State()
    add_rarity = State()
    add_slot = State()
    add_emoji = State()
    edit_select = State()
    edit_field = State()
    new_value = State()
    delete_confirm = State()

async def get_gear_list_keyboard(page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items = await db.get_all_gear(offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for gear in items:
        keyboard.append([InlineKeyboardButton(
            text=f"{gear['emoji']} {gear['name']} (ID {gear['id']}) [{gear['rarity']}]",
            callback_data=f"gear_edit_{gear['id']}"
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"gear_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"gear_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="➕ Добавить снаряжение", callback_data="gear_add")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(F.data == "admin_manage_gear")
async def manage_gear(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    keyboard = await get_gear_list_keyboard(1)
    await callback.message.edit_text("⚔️ Управление снаряжением:\nВыберите предмет для редактирования или добавьте новый:", reply_markup=keyboard)
    await state.set_state(GearStates.list_page)
    await callback.answer()

@admin_router.callback_query(GearStates.list_page, F.data.startswith("gear_page_"))
async def gear_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[2])
    keyboard = await get_gear_list_keyboard(page)
    await callback.message.edit_text("⚔️ Управление снаряжением:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(GearStates.list_page, F.data == "gear_add")
async def gear_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название снаряжения:")
    await state.set_state(GearStates.add_name)
    await callback.answer()

@admin_router.message(GearStates.add_name, F.text)
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
    await state.set_state(GearStates.add_rarity)

@admin_router.callback_query(GearStates.add_rarity, F.data.startswith("rarity_"))
async def gear_add_slot(callback: types.CallbackQuery, state: FSMContext):
    rarity = callback.data.split("_")[1]
    await state.update_data(gear_rarity=rarity)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗡 Оружие (оружие)", callback_data="slot_оружие")],
        [InlineKeyboardButton(text="🛡 Щит (щит)", callback_data="slot_щит")],
        [InlineKeyboardButton(text="🪖 Голова (голова)", callback_data="slot_голова")],
        [InlineKeyboardButton(text="🦺 Торс (торс)", callback_data="slot_торс")],
        [InlineKeyboardButton(text="🧤 Руки (руки)", callback_data="slot_руки")],
        [InlineKeyboardButton(text="🩳 Ноги (ноги)", callback_data="slot_ноги")],
        [InlineKeyboardButton(text="🧣 Спина (спина)", callback_data="slot_спина")],
        [InlineKeyboardButton(text="📖 Аксессуар (аксессуар)", callback_data="slot_аксессуар")],
        [InlineKeyboardButton(text="🪹 Плечи (плечи)", callback_data="slot_плечи")]
    ])
    await callback.message.edit_text("Выберите слот:", reply_markup=keyboard)
    await state.set_state(GearStates.add_slot)

@admin_router.callback_query(GearStates.add_slot, F.data.startswith("slot_"))
async def gear_add_emoji(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split("_")[1]
    await state.update_data(gear_slot=slot)
    await callback.message.edit_text("Введите эмодзи для снаряжения (один символ):")
    await state.set_state(GearStates.add_emoji)
    await callback.answer()

@admin_router.message(GearStates.add_emoji, F.text)
async def gear_save(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ.")
        return
    data = await state.get_data()
    try:
        await db.add_gear(data['gear_name'], data['gear_rarity'], data['gear_slot'], emoji)
        await message.answer(f"✅ Снаряжение <b>{data['gear_name']}</b> добавлено.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_gear_list_keyboard(1)
    await message.answer("⚔️ Управление снаряжением:", reply_markup=keyboard)
    await state.set_state(GearStates.list_page)

@admin_router.callback_query(GearStates.list_page, F.data.startswith("gear_edit_"))
async def gear_edit_menu(callback: types.CallbackQuery, state: FSMContext):
    gear_id = int(callback.data.split("_")[2])
    gear = await db.get_gear_by_id(gear_id)
    if not gear:
        await callback.message.edit_text("Предмет не найден.")
        await callback.answer()
        return
    await state.update_data(gear_id=gear_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✏️ Название: {gear['name']}", callback_data="gear_edit_name")],
        [InlineKeyboardButton(text=f"⭐ Редкость: {gear['rarity']}", callback_data="gear_edit_rarity")],
        [InlineKeyboardButton(text=f"🔧 Слот: {gear['slot']}", callback_data="gear_edit_slot")],
        [InlineKeyboardButton(text=f"😀 Эмодзи: {gear['emoji']}", callback_data="gear_edit_emoji")],
        [InlineKeyboardButton(text="🗑 Удалить предмет", callback_data="gear_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="gear_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await callback.message.edit_text(f"Редактирование: {gear['emoji']} {gear['name']}\nВыберите поле:", reply_markup=keyboard)
    await state.set_state(GearStates.edit_select)
    await callback.answer()

@admin_router.callback_query(GearStates.edit_select, F.data == "gear_back_to_list")
async def gear_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    keyboard = await get_gear_list_keyboard(1)
    await callback.message.edit_text("⚔️ Управление снаряжением:", reply_markup=keyboard)
    await state.set_state(GearStates.list_page)
    await callback.answer()

@admin_router.callback_query(GearStates.edit_select, F.data.startswith("gear_edit_"))
async def gear_edit_field(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[2]  # name, rarity, slot, emoji
    await state.update_data(gear_edit_field=field)
    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(GearStates.new_value)
    await callback.answer()

@admin_router.message(GearStates.new_value, F.text)
async def gear_update_field(message: types.Message, state: FSMContext):
    new_value = message.text.strip()
    data = await state.get_data()
    gear_id = data['gear_id']
    field = data['gear_edit_field']
    if field == 'emoji' and not is_valid_emoji(new_value):
        await message.answer("Эмодзи должен быть ровно один символ.")
        return
    if field == 'name' and not new_value:
        await message.answer("Название не может быть пустым.")
        return
    try:
        if field == 'name':
            await db.update_gear(gear_id, name=new_value)
        elif field == 'rarity':
            if new_value not in ('common', 'rare', 'epic'):
                await message.answer("Редкость должна быть common, rare или epic.")
                return
            await db.update_gear(gear_id, rarity=new_value)
        elif field == 'slot':
            await db.update_gear(gear_id, slot=new_value)
        elif field == 'emoji':
            await db.update_gear(gear_id, emoji=new_value)
        await message.answer(f"✅ Поле <b>{field}</b> обновлено на <code>{new_value}</code>.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        return
    gear = await db.get_gear_by_id(gear_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"✏️ Название: {gear['name']}", callback_data="gear_edit_name")],
        [InlineKeyboardButton(text=f"⭐ Редкость: {gear['rarity']}", callback_data="gear_edit_rarity")],
        [InlineKeyboardButton(text=f"🔧 Слот: {gear['slot']}", callback_data="gear_edit_slot")],
        [InlineKeyboardButton(text=f"😀 Эмодзи: {gear['emoji']}", callback_data="gear_edit_emoji")],
        [InlineKeyboardButton(text="🗑 Удалить предмет", callback_data="gear_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="gear_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await message.answer(f"Редактирование: {gear['emoji']} {gear['name']}\nВыберите поле:", reply_markup=keyboard)
    await state.set_state(GearStates.edit_select)

@admin_router.callback_query(GearStates.edit_select, F.data == "gear_delete")
async def gear_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gear_id = data['gear_id']
    gear = await db.get_gear_by_id(gear_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="gear_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="gear_back_to_list")]
    ])
    await callback.message.edit_text(f"⚠️ Удалить <b>{gear['name']}</b> (ID {gear_id})? Это удалит его из дропов и рецептов.", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(GearStates.delete_confirm)
    await callback.answer()

@admin_router.callback_query(GearStates.delete_confirm, F.data == "gear_delete_yes")
async def gear_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gear_id = data['gear_id']
    try:
        await db.delete_gear(gear_id)
        await callback.message.edit_text("✅ Снаряжение удалено.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_gear_list_keyboard(1)
    await callback.message.answer("⚔️ Управление снаряжением:", reply_markup=keyboard)
    await state.set_state(GearStates.list_page)
    await callback.answer()

# ==================== УПРАВЛЕНИЕ РЕЦЕПТАМИ ====================
class RecipeStates(StatesGroup):
    list_type = State()
    list_page = State()
    view_recipe = State()
    add_confirm = State()
    add_ingredient = State()
    add_owner = State()
    edit_ingredient = State()
    edit_ingredient_quantity = State()
    delete_confirm = State()

async def get_recipe_type_keyboard() -> InlineKeyboardMarkup:
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
    await callback.answer()

async def get_recipe_list_keyboard(result_type: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    recipes = await db.get_all_recipes(result_type, offset, ADMIN_ITEMS_PER_PAGE + 1)
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
    await callback.message.edit_text(f"Рецепты для {result_type.upper()}:\nВыберите рецепт для просмотра/редактирования:", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)
    await callback.answer()

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_page_"))
async def recipe_list_page(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    result_type = parts[2]
    page = int(parts[3])
    await state.update_data(recipe_result_type=result_type, recipe_page=page)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты для {result_type.upper()}:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(RecipeStates.list_page, F.data == "recipe_back_to_type")
async def recipe_back_to_type(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)
    await callback.answer()

async def show_recipe(target, recipe: dict, state: FSMContext):
    """Универсальный показ карточки рецепта"""
    if recipe['result_type'] == 'gear':
        gear = await db.get_gear_by_id(recipe['result_id'])
        result_info = f"{gear['emoji']} {gear['name']}" if gear else f"ID {recipe['result_id']}"
    else:
        res = await db.get_resource_by_id(recipe['result_id'])
        result_info = f"{res['emoji']} {res['name']}" if res else f"ID {recipe['result_id']}"
    text = f"📜 Рецепт ID {recipe['id']}\n🎁 Результат: {result_info} (количество: {recipe['quantity']})\n\n"
    text += "<b>Ингредиенты:</b>\n"
    if recipe['ingredients']:
        for ing in recipe['ingredients']:
            text += f"  {ing['emoji']} {ing['name']} — {ing['quantity']} шт.\n"
    else:
        text += "<i>Нет ингредиентов</i>\n"
    text += "\n👥 <b>Владельцы рецепта:</b>\n"
    if recipe['owners']:
        text += "\n".join(f"  @{clean_username(owner)}" for owner in recipe['owners'])
    else:
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
        await callback.answer()
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
    await callback.answer()

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_add_"))
async def recipe_add_choose_item(callback: types.CallbackQuery, state: FSMContext):
    result_type = callback.data.split("_")[2]
    await state.update_data(new_recipe_type=result_type)
    if result_type == 'gear':
        all_gear = await db.get_all_gear_simple()
        existing = await db.execute_query("SELECT result_id FROM recipes WHERE result_type='gear'")
        existing_ids = {e['result_id'] for e in existing}
        available = [g for g in all_gear if g['id'] not in existing_ids]
        if not available:
            await callback.message.edit_text("Для всего снаряжения уже есть рецепты.")
            await callback.answer()
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{g['emoji']} {g['name']}", callback_data=f"recipe_new_target_{g['id']}")] for g in available
        ])
        await callback.message.edit_text("Выберите снаряжение, для которого создать рецепт:", reply_markup=keyboard)
    else:
        all_res = await db.get_all_resources_simple()
        existing = await db.execute_query("SELECT result_id FROM recipes WHERE result_type='resource'")
        existing_ids = {e['result_id'] for e in existing}
        available = [r for r in all_res if r['id'] not in existing_ids]
        if not available:
            await callback.message.edit_text("Для всех ресурсов уже есть рецепты.")
            await callback.answer()
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{r['emoji']} {r['name']}", callback_data=f"recipe_new_target_{r['id']}")] for r in available
        ])
        await callback.message.edit_text("Выберите ресурс, для которого создать рецепт:", reply_markup=keyboard)
    await state.set_state(RecipeStates.add_confirm)

@admin_router.callback_query(RecipeStates.add_confirm, F.data.startswith("recipe_new_target_"))
async def recipe_create(callback: types.CallbackQuery, state: FSMContext):
    result_id = int(callback.data.split("_")[3])
    data = await state.get_data()
    result_type = data['new_recipe_type']
    try:
        recipe_id = await db.create_recipe(result_type, result_id, 1)
        await callback.message.edit_text(f"✅ Рецепт создан (ID {recipe_id}). Теперь добавьте ингредиенты и владельцев через кнопки.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        await callback.answer()
        return
    recipe = await db.get_recipe_details(recipe_id)
    await state.update_data(recipe_id=recipe_id, recipe_result_type=result_type, recipe_page=1)
    await show_recipe(callback, recipe, state)

# Добавление ингредиента
@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_ingredient")
async def recipe_add_ingredient_select(callback: types.CallbackQuery, state: FSMContext):
    resources = await db.get_all_resources_simple()
    if not resources:
        await callback.answer("Нет ресурсов в БД", show_alert=True)
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{r['emoji']} {r['name']}", callback_data=f"recipe_ing_select_{r['id']}")] for r in resources[:20]
    ])
    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="recipe_back_to_view")])
    await callback.message.edit_text("Выберите ресурс для добавления в ингредиенты:", reply_markup=keyboard)
    await state.set_state(RecipeStates.add_ingredient)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_select_"))
async def recipe_ingredient_quantity(callback: types.CallbackQuery, state: FSMContext):
    resource_id = int(callback.data.split("_")[3])
    await state.update_data(temp_resource_id=resource_id)
    await callback.message.edit_text("Введите количество (целое число):")
    await state.set_state(RecipeStates.edit_ingredient_quantity)
    await state.update_data(edit_action='add')

@admin_router.message(RecipeStates.edit_ingredient_quantity, F.text)
async def recipe_ingredient_save(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Введите положительное целое число.")
        return
    data = await state.get_data()
    recipe_id = data['recipe_id']
    action = data.get('edit_action')
    if action == 'add':
        resource_id = data['temp_resource_id']
        await db.add_ingredient(recipe_id, resource_id, qty)
        await message.answer("✅ Ингредиент добавлен.")
    elif action == 'change':
        resource_id = data['edit_resource_id']
        await db.update_ingredient(recipe_id, resource_id, qty)
        await message.answer("✅ Количество обновлено.")
    else:
        await message.answer("Ошибка: неизвестное действие.")
        return
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(message, recipe, state)

# Добавление владельца
@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_owner")
async def recipe_add_owner_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите username владельца (без @):")
    await state.set_state(RecipeStates.add_owner)
    await callback.answer()

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

# Редактирование ингредиентов
@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_edit_ingredients")
async def recipe_edit_ingredients_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    if not recipe['ingredients']:
        await callback.answer("Нет ингредиентов для редактирования", show_alert=True)
        return
    keyboard = []
    for ing in recipe['ingredients']:
        keyboard.append([InlineKeyboardButton(
            text=f"{ing['emoji']} {ing['name']} — {ing['quantity']} шт.",
            callback_data=f"recipe_edit_ing_{ing['resource_id']}"
        )])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к рецепту", callback_data="recipe_back_to_view")])
    await callback.message.edit_text("Выберите ингредиент для изменения количества или удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.edit_ingredient)

@admin_router.callback_query(RecipeStates.edit_ingredient, F.data.startswith("recipe_edit_ing_"))
async def recipe_edit_ingredient_options(callback: types.CallbackQuery, state: FSMContext):
    resource_id = int(callback.data.split("_")[3])
    await state.update_data(edit_resource_id=resource_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить количество", callback_data="recipe_ing_change")],
        [InlineKeyboardButton(text="❌ Удалить ингредиент", callback_data="recipe_ing_delete")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="recipe_back_to_edit_list")]
    ])
    await callback.message.edit_text("Что сделать с этим ингредиентом?", reply_markup=keyboard)
    await state.set_state(RecipeStates.edit_ingredient_quantity)
    await state.update_data(edit_action='change')  # для изменения

@admin_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_change")
async def recipe_ing_change_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новое количество:")
    await state.update_data(edit_action='change')
    await state.set_state(RecipeStates.edit_ingredient_quantity)  # остаёмся в том же состоянии, но следующий message обработает

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

# Обработчик для ввода количества при изменении ингредиента (уже есть общий message handler выше)
# Но чтобы не конфликтовать, оставим как есть – он обработает и добавление, и изменение.

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_back_to_view")
async def recipe_back_to_view(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_delete")
async def recipe_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="recipe_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_back_to_view")]
    ])
    await callback.message.edit_text("⚠️ Удалить рецепт? Это действие необратимо.", reply_markup=keyboard)
    await state.set_state(RecipeStates.delete_confirm)

@admin_router.callback_query(RecipeStates.delete_confirm, F.data == "recipe_delete_yes")
async def recipe_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    result_type = data.get('recipe_result_type', 'gear')
    await db.delete_recipe(recipe_id)
    await callback.message.edit_text("✅ Рецепт удалён.")
    await state.update_data(recipe_result_type=result_type, recipe_page=1)
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await callback.message.answer(f"Рецепты для {result_type.upper()}:", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)
    await callback.answer()
