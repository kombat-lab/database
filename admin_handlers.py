import os
import logging
from typing import List, Optional

from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database import db

logger = logging.getLogger(__name__)

# ---------- Настройка прав администратора ----------
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

# ---------- Роутер ----------
admin_router = Router()

# ==================== ГЛАВНОЕ МЕНЮ ====================
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

# Заглушки для нереализованных разделов
@admin_router.callback_query(F.data == "admin_resources_soon")
async def soon(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)
@admin_router.callback_query(F.data == "admin_gear_soon")
async def soon(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)
@admin_router.callback_query(F.data == "admin_recipes_soon")
async def soon(callback: types.CallbackQuery):
    await callback.answer("Раздел в разработке", show_alert=True)

# ==================== ДОБАВЛЕНИЕ МОБА (FSM) ====================
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
    # Запрашиваем локации
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
            "Теперь вы можете добавить дропы (ресурсы) позже через редактирование.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.exception("Ошибка добавления моба")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await callback.answer()

# ==================== РЕДАКТИРОВАНИЕ МОБА ====================
class EditMobStates(StatesGroup):
    select_mob = State()      # выбор моба по ID
    select_field = State()    # выбор поля для редактирования
    new_value = State()       # ввод нового значения

@admin_router.callback_query(F.data == "admin_edit_mob")
async def start_edit_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    mobs = await db.execute_query("SELECT id, name FROM mobs ORDER BY id")
    if not mobs:
        await callback.message.edit_text("❌ Нет мобов для редактирования.")
        await callback.answer()
        return
    # Строим клавиатуру из мобов (постранично? для простоты – до 20 кнопок)
    keyboard = []
    for mob in mobs[:20]:
        keyboard.append([InlineKeyboardButton(text=f"{mob['name']} (ID {mob['id']})", callback_data=f"edit_mob_{mob['id']}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_cancel_edit")])
    await callback.message.edit_text("Выберите моба для редактирования:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(EditMobStates.select_mob)
    await callback.answer()

@admin_router.callback_query(EditMobStates.select_mob, F.data.startswith("edit_mob_"))
async def select_mob_for_edit(callback: types.CallbackQuery, state: FSMContext):
    mob_id = int(callback.data.split("_")[2])
    await state.update_data(mob_id=mob_id)
    # Показываем поля для редактирования
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
    keyboard.append([InlineKeyboardButton(text="🔙 Отмена", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(f"Редактирование моба ID {mob_id}\nВыберите поле:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
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
    # Валидация в зависимости от типа поля
    if field in ('hp', 'dust_min', 'dust_max', 'exp', 'location_id'):
        try:
            new_value = int(new_value)
        except ValueError:
            await message.answer("Ошибка: поле должно быть целым числом. Попробуйте снова:")
            return
    # Обновление в БД
    query = f"UPDATE mobs SET {field} = ? WHERE id = ?"
    try:
        await db.execute_query(query, (new_value, mob_id))
        await message.answer(f"✅ Поле *{field}* успешно обновлено на `{new_value}`.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ Ошибка обновления: {e}")
    await state.clear()
    # Возвращаем в главное меню админки
    await message.answer("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())

@admin_router.callback_query(F.data == "admin_cancel_edit")
async def cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🔧 Админ-панель", reply_markup=await get_admin_main_keyboard())
    await callback.answer()

# ==================== УДАЛЕНИЕ МОБА ====================
class DeleteMobStates(StatesGroup):
    select_mob = State()
    confirm = State()

@admin_router.callback_query(F.data == "admin_delete_mob")
async def start_delete_mob(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа")
        return
    mobs = await db.execute_query("SELECT id, name FROM mobs ORDER BY id")
    if not mobs:
        await callback.message.edit_text("❌ Нет мобов для удаления.")
        await callback.answer()
        return
    keyboard = []
    for mob in mobs[:20]:
        keyboard.append([InlineKeyboardButton(text=f"{mob['name']} (ID {mob['id']})", callback_data=f"del_mob_{mob['id']}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_cancel_edit")])
    await callback.message.edit_text("Выберите моба для УДАЛЕНИЯ:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(DeleteMobStates.select_mob)
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
        # Сначала удаляем связанные записи (mob_drops, gear_drops), если есть внешние ключи
        await db.execute_query("DELETE FROM mob_drops WHERE mob_id = ?", (mob_id,))
        await db.execute_query("DELETE FROM gear_drops WHERE mob_id = ?", (mob_id,))
        await db.execute_query("DELETE FROM mobs WHERE id = ?", (mob_id,))
        await callback.message.edit_text("✅ Моб успешно удалён.")
    except Exception as e:
        logger.exception("Ошибка удаления моба")
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    await callback.answer()
