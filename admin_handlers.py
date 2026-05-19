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
    return len(s) == 1 and not s.isalnum()

# ==================== ГЛАВНОЕ МЕНЮ ====================
async def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить моба", callback_data="admin_add_mob")],
        [InlineKeyboardButton(text="✏️ Редактировать моба", callback_data="admin_edit_mob")],
        [InlineKeyboardButton(text="🗑 Удалить моба", callback_data="admin_delete_mob")],
        [InlineKeyboardButton(text="📦 Управление ресурсами", callback_data="admin_manage_resources")],
        [InlineKeyboardButton(text="⚔️ Снаряжение (позже)", callback_data="admin_gear_soon")],
        [InlineKeyboardButton(text="📜 Рецепты (позже)", callback_data="admin_recipes_soon")],
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

@admin_router.callback_query(F.data == "admin_gear_soon")
async def soon_gear(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)

@admin_router.callback_query(F.data == "admin_recipes_soon")
async def soon_recipes(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)

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
    edit_select = State()
    edit_name = State()
    edit_emoji = State()
    delete_confirm = State()

async def get_resources_list_keyboard(page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    resources = await db.get_resources_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(resources) > ADMIN_ITEMS_PER_PAGE
    resources = resources[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for res in resources:
        keyboard.append([InlineKeyboardButton(
            text=f"{res['emoji']} {res['name']} (ID {res['id']})",
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
    logger.info(f"resource_add_emoji получил текст: {message.text}")
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова:")
        return
    await state.update_data(res_name=name)
    await message.answer("Теперь введите эмодзи для ресурса (один символ, например 🍎):")
    await state.set_state(ResourceStates.add_emoji)

@admin_router.message(ResourceStates.add_emoji, F.text)
async def resource_save(message: types.Message, state: FSMContext):
    logger.info(f"resource_save получил эмодзи: {message.text}")
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра). Попробуйте снова:")
        return
    data = await state.get_data()
    name = data['res_name']
    try:
        await db.add_resource(name, emoji)
        await message.answer(f"✅ Ресурс <b>{name}</b> добавлен.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_resources_list_keyboard(1)
    await message.answer("📦 Управление ресурсами:", reply_markup=keyboard)
    await state.set_state(ResourceStates.list_page)

@admin_router.callback_query(ResourceStates.list_page, F.data.startswith("resource_edit_"))
async def resource_edit_menu(callback: types.CallbackQuery, state: FSMContext):
    resource_id = int(callback.data.split("_")[2])
    res = await db.get_resource_by_id(resource_id)
    if not res:
        await callback.message.edit_text("Ресурс не найден.")
        await callback.answer()
        return
    await state.update_data(res_id=resource_id, res_name=res['name'], res_emoji=res['emoji'])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await callback.message.edit_text(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']})\nЧто хотите сделать?", reply_markup=keyboard)
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
    logger.info(f"resource_update_name получил текст: {message.text}")
    new_name = message.text.strip()
    if not new_name:
        await message.answer("Название не может быть пустым. Попробуйте снова:")
        return
    data = await state.get_data()
    res_id = data['res_id']
    current_emoji = data['res_emoji']
    try:
        await db.update_resource(res_id, new_name, current_emoji)
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
    await state.update_data(res_name=res['name'], res_emoji=res['emoji'])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await message.answer(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']})\nЧто хотите сделать?", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_select)

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_edit_emoji")
async def resource_edit_emoji_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новый эмодзи для ресурса (один символ):")
    await state.set_state(ResourceStates.edit_emoji)
    await callback.answer()

@admin_router.message(ResourceStates.edit_emoji, F.text)
async def resource_update_emoji(message: types.Message, state: FSMContext):
    logger.info(f"resource_update_emoji получил эмодзи: {message.text}")
    new_emoji = message.text.strip()
    if not is_valid_emoji(new_emoji):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра). Попробуйте снова:")
        return
    data = await state.get_data()
    res_id = data['res_id']
    current_name = data['res_name']
    try:
        await db.update_resource(res_id, current_name, new_emoji)
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
    await state.update_data(res_name=res['name'], res_emoji=res['emoji'])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить название", callback_data="resource_edit_name")],
        [InlineKeyboardButton(text="😀 Изменить эмодзи", callback_data="resource_edit_emoji")],
        [InlineKeyboardButton(text="🗑 Удалить ресурс", callback_data="resource_delete")],
        [InlineKeyboardButton(text="🔙 Назад к списку", callback_data="resource_back_to_list")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")]
    ])
    await message.answer(f"Ресурс: {res['emoji']} {res['name']} (ID {res['id']})\nЧто хотите сделать?", reply_markup=keyboard)
    await state.set_state(ResourceStates.edit_select)

@admin_router.callback_query(ResourceStates.edit_select, F.data == "resource_delete")
async def resource_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    res_id = data['res_id']
    res_name = data['res_name']
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="resource_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="resource_back_to_list")]
    ])
    await callback.message.edit_text(f"⚠️ Вы уверены, что хотите удалить ресурс <b>{res_name}</b> (ID {res_id})?\nЭто также удалит его из дропов всех мобов.", parse_mode="HTML", reply_markup=keyboard)
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
    logger.info(f"add_mob_name получил текст: {message.text}")
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым. Попробуйте снова:")
        return
    await state.update_data(name=name)
    await message.answer("Введите эмодзи моба (один символ, например 🐺):")
    await state.set_state(AddMobStates.emoji)

@admin_router.message(AddMobStates.emoji, F.text)
async def add_mob_emoji(message: types.Message, state: FSMContext):
    logger.info(f"add_mob_emoji получил эмодзи: {message.text}")
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен быть ровно один символ (не буква и не цифра). Попробуйте снова:")
        return
    await state.update_data(emoji=emoji)
    await message.answer("Введите HP (целое положительное число):")
    await state.set_state(AddMobStates.hp)

@admin_router.message(AddMobStates.hp, F.text)
async def add_mob_hp(message: types.Message, state: FSMContext):
    logger.info(f"add_mob_hp получил текст: {message.text}")
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
    logger.info(f"add_mob_dust_min получил текст: {message.text}")
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
    logger.info(f"add_mob_dust_max получил текст: {message.text}")
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
    logger.info(f"add_mob_exp получил текст: {message.text}")
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
    logger.info(f"set_new_value получил текст: {message.text}")
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
    elif category == 'recipe':
        items = await db.get_recipes_page(offset, ADMIN_ITEMS_PER_PAGE + 1)
        has_next = len(items) > ADMIN_ITEMS_PER_PAGE
        items = items[:ADMIN_ITEMS_PER_PAGE]
    else:
        return InlineKeyboardMarkup(inline_keyboard=[])
    keyboard = []
    for item in items:
        has_drop = await db.get_mob_drop_status(mob_id, category, item['id'])
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
        [InlineKeyboardButton(text="📜 Рецепты (свитки)", callback_data="drop_category_recipe")],
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
    has_drop = await db.get_mob_drop_status(mob_id, category, item_id)
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
