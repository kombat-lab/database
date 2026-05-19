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

# ---------- Главное меню ----------
async def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить моба", callback_data="admin_add_mob")],
        [InlineKeyboardButton(text="✏️ Редактировать моба", callback_data="admin_edit_mob")],
        [InlineKeyboardButton(text="🗑 Удалить моба", callback_data="admin_delete_mob")],
        [InlineKeyboardButton(text="📦 Ресурсы (позже)", callback_data="admin_resources_soon")],
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
    await message.answer("🔧 **Админ-панель**\nВыберите действие:", parse_mode="Markdown",
                         reply_markup=await get_admin_main_keyboard())

@admin_router.callback_query(F.data == "admin_close")
async def admin_close(callback: types.CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@admin_router.callback_query(F.data == "admin_resources_soon")
async def soon(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)
@admin_router.callback_query(F.data == "admin_gear_soon")
async def soon(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)
@admin_router.callback_query(F.data == "admin_recipes_soon")
async def soon(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)

# ---------- Добавление моба ----------
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
    await callback.message.edit_text("Введите название моба (например *Лесной волк*):", parse_mode="Markdown")
    await state.set_state(AddMobStates.name)
    await callback.answer()

@admin_router.message(AddMobStates.name)
async def add_mob_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите эмодзи моба (один символ, например 🐺):")
    await state.set_state(AddMobStates.emoji)

@admin_router.message(AddMobStates.emoji)
async def add_mob_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not emoji:
        await message.answer("Эмодзи не может быть пустым.")
        return
    await state.update_data(emoji=emoji)
    await message.answer("Введите HP (целое число):")
    await state.set_state(AddMobStates.hp)

@admin_router.message(AddMobStates.hp)
async def add_mob_hp(message: types.Message, state: FSMContext):
    try:
        hp = int(message.text.strip())
    except ValueError:
        await message.answer("Ошибка: введите целое число.")
        return
    await state.update_data(hp=hp)
    await message.answer("Введите минимальное количество пыли (dust_min):")
    await state.set_state(AddMobStates.dust_min)

@admin_router.message(AddMobStates.dust_min)
async def add_mob_dust_min(message: types.Message, state: FSMContext):
    try:
        dust_min = int(message.text.strip())
    except ValueError:
        await message.answer("Ошибка: введите целое число.")
        return
    await state.update_data(dust_min=dust_min)
    await message.answer("Введите максимальное количество пыли (dust_max):")
    await state.set_state(AddMobStates.dust_max)

@admin_router.message(AddMobStates.dust_max)
async def add_mob_dust_max(message: types.Message, state: FSMContext):
    try:
        dust_max = int(message.text.strip())
    except ValueError:
        await message.answer("Ошибка: введите целое число.")
        return
    data = await state.get_data()
    if dust_max < data['dust_min']:
        await message.answer("dust_max не может быть меньше dust_min. Повторите:")
        return
    await state.update_data(dust_max=dust_max)
    await message.answer("Введите опыт (exp):")
    await state.set_state(AddMobStates.exp)

@admin_router.message(AddMobStates.exp)
async def add_mob_exp(message: types.Message, state: FSMContext):
    try:
        exp = int(message.text.strip())
    except ValueError:
        await message.answer("Ошибка: введите целое число.")
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
            f"✅ Моб *{data['name']}* добавлен (ID: {mob_id}).\n\n"
            "Теперь вы можете добавить дропы через редактирование.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Ошибка добавления моба")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await callback.answer()

# ---------- Редактирование моба ----------
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
    await state.update_data(mob_id=mob_id)
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
    await callback.message.edit_text(f"Введите новое значение для поля *{field}*:", parse_mode="Markdown")
    await state.set_state(EditMobStates.new_value)
    await callback.answer()

@admin_router.message(EditMobStates.new_value)
async def set_new_value(message: types.Message, state: FSMContext):
    new_value = message.text.strip()
    data = await state.get_data()
    mob_id = data['mob_id']
    field = data['edit_field']
    if field in ('hp', 'dust_min', 'dust_max', 'exp', 'location_id'):
        try:
            new_value = int(new_value)
        except ValueError:
            await message.answer("Ошибка: поле должно быть целым числом. Попробуйте снова:")
            return
    query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
    try:
        await db.execute_query(query, (new_value, mob_id))
        await message.answer(f"✅ Поле *{field}* успешно обновлено на `{new_value}`.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка обновления: {e}")
    await state.clear()
    await message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())

@admin_router.callback_query(EditMobStates.select_field, F.data == "edit_drop_menu")
async def drop_category_menu(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="📜 Рецепты (свитки)", callback_data="drop_category_recipe")],
        [InlineKeyboardButton(text="🗺️ Карты", callback_data="drop_category_map")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_edit_mob")]
    ])
    await callback.message.edit_text("Выберите категорию дропа:", reply_markup=keyboard)
    await state.set_state(EditMobStates.drop_category)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_category, F.data == "back_to_edit_mob")
async def back_to_edit_mob(callback: types.CallbackQuery, state: FSMContext):
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

async def get_drop_list_keyboard(mob_id: int, category: str, page: int) -> InlineKeyboardMarkup:
    if category == 'resource':
        items = await db.get_all_resources()
    elif category == 'gear':
        items = await db.get_all_common_gear()
    elif category == 'recipe':
        items = await db.get_all_recipes()   # только ресурсы с id >= 59
    elif category == 'map':
        items = await db.get_all_maps()
    else:
        return InlineKeyboardMarkup(inline_keyboard=[])

    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    total = len(items)
    has_next = offset + ADMIN_ITEMS_PER_PAGE < total
    items_page = items[offset:offset + ADMIN_ITEMS_PER_PAGE]

    keyboard = []
    for item in items_page:
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
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@admin_router.callback_query(EditMobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[2]
    data = await state.get_data()
    mob_id = data['mob_id']
    await state.update_data(drop_category=category, drop_page=1)
    keyboard = await get_drop_list_keyboard(mob_id, category, 1)
    await callback.message.edit_text(f"Управление дропом: {category.upper()}\nНажмите на предмет, чтобы добавить/удалить:", reply_markup=keyboard)
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
    await callback.message.edit_text(f"Управление дропом: {category.upper()}\nНажмите на предмет, чтобы добавить/удалить:", reply_markup=keyboard)
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
    await callback.message.edit_text(f"Управление дропом: {category.upper()}\nНажмите на предмет, чтобы добавить/удалить:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(EditMobStates.drop_list_page, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext):
    await drop_category_menu(callback, state)

# ---------- Удаление моба ----------
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
    await callback.message.edit_text(f"⚠️ Вы уверены, что хотите удалить моба *{mob_name}* (ID {mob_id})?\nЭто действие необратимо.", parse_mode="Markdown", reply_markup=keyboard)
    await state.set_state(DeleteMobStates.confirm)
    await callback.answer()

@admin_router.callback_query(DeleteMobStates.confirm, F.data == "confirm_delete_yes")
async def delete_mob(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    try:
        await db.execute_query("DELETE FROM mob_drops WHERE mob_id = ?", (mob_id,))
        await db.execute_query("DELETE FROM gear_drops WHERE mob_id = ?", (mob_id,))
        await db.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))
        await callback.message.edit_text("✅ Моб успешно удалён.")
    except Exception as e:
        logger.exception("Ошибка удаления моба")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await callback.answer()

@admin_router.callback_query(F.data == "admin_cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()
