import logging
from collections.abc import Sequence
from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from admin_contracts import EntityRow
from admin_utils import ADMIN_ITEMS_PER_PAGE, get_admin_main_keyboard, prepare_delete_confirmation, consume_delete_confirmation
from database import ItemRow, db
from telegram_helpers import get_callback_data, get_callback_message, get_message_text
from game_constants import GEAR_SLOT_LABELS, GEAR_SLOTS, RARITY_EMOJIS
from utils import escape_html, is_valid_emoji
from admin_sessions import present_admin_text, register_protected_callbacks, validate_admin_input
logger = logging.getLogger(__name__)
register_protected_callbacks(('mob_', 'edit_mob_', 'drop_', 'confirm_mob_delete', 'back_to_mob_'))

async def present_mob(target: types.Message | types.CallbackQuery, state: FSMContext,
                      text: str, reply_markup: InlineKeyboardMarkup | None = None,
                      parse_mode: str | None = None) -> types.Message:
    data = await state.get_data()
    context = {key: value for key in ('mob_id', 'mob_location_id', 'edit_field') if isinstance((value := data.get(key)), (str, int))}
    return await present_admin_text(target, state, text, reply_markup, context=context, parse_mode=parse_mode)

mob_router = Router()
RESOURCE_TYPES = [('craft', '📦 Крафтовые'), ('consumable', '✨ Расходуемые'), ('scroll_recipe', '📜 Рецепты экипировки'), ('currency', '💰 Валюта'), ('alchemy', '🧪 Алхимия')]

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
    delete_confirm = State()
    drop_category = State()
    drop_list_page = State()
    drop_search = State()


async def get_sorted_locations() -> list[ItemRow]:
    locations = await db.get_locations()
    return sorted(locations, key=lambda location: str(location.get('name') or '').casefold())


async def get_location_choice_keyboard(
    callback_prefix: str,
    cancel_callback: str | None = None,
    locations: Sequence[ItemRow] | None = None,
) -> InlineKeyboardMarkup:
    if locations is None:
        locations = await get_sorted_locations()
    rows = [[InlineKeyboardButton(
        text=f"{location.get('emoji') or '📍'} {location['name']}",
        callback_data=f"{callback_prefix}{location['id']}",
    )] for location in locations]
    if cancel_callback:
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data=cancel_callback)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_mob_edit_data(mob_id: int) -> EntityRow | None:
    rows = await db.execute_query(
        """
        SELECT m.*, l.name AS location_name, l.emoji AS location_emoji
        FROM mobs m
        LEFT JOIN locations l ON l.id = m.location_id
        WHERE m.id = ?
        """,
        (mob_id,),
    )
    return rows[0] if rows else None


def build_mob_edit_keyboard(mob: EntityRow) -> InlineKeyboardMarkup:
    location_name = mob.get('location_name') or 'Неизвестная локация'
    location_emoji = mob.get('location_emoji') or '📍'
    fields = [
        ('name', f"Имя: {mob['name']}"),
        ('emoji', f"Эмодзи: {mob['emoji']}"),
        ('hp', f"HP: {mob['hp']}"),
        ('dust_min', f"Пыль мин: {mob['dust_min']}"),
        ('dust_max', f"Пыль макс: {mob['dust_max']}"),
        ('exp', f"Опыт: {mob['exp']}"),
        ('location_id', f"Локация: {location_emoji} {location_name}"),
    ]
    rows = [[InlineKeyboardButton(
        text=label,
        callback_data=f"mob_edit_field_{field}",
    )] for field, label in fields]
    rows.append([InlineKeyboardButton(text="📦 Управление дропом", callback_data="mob_drop_menu")])
    rows.append([InlineKeyboardButton(text="🗑 Удалить моба", callback_data="mob_delete")])
    rows.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_mob_list")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def get_mob_locations_keyboard() -> InlineKeyboardMarkup:
    locations = await get_sorted_locations()
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


@mob_router.callback_query(F.data == "admin_edit_mob")
async def start_edit_mob(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await present_mob(callback, state, "🐾 Управление мобами:\nВыберите локацию:", reply_markup=await get_mob_locations_keyboard())
    await state.set_state(MobStates.edit_select)
    await callback.answer()


@mob_router.callback_query(MobStates.edit_select, F.data.startswith("mob_location_"))
async def mob_location_select(callback: types.CallbackQuery, state: FSMContext) -> None:
    location_id = int(get_callback_data(callback).rsplit("_", 1)[1])
    location = await db.get_location_by_id(location_id)
    if not location:
        await callback.answer("Локация не найдена", show_alert=True)
        return
    await state.update_data(mob_location_id=location_id)
    await present_mob(callback, state, f"🐾 Мобы: {location['emoji']} {location['name']}\nВыберите моба или добавьте нового:", reply_markup=await get_mob_list_keyboard(location_id, 1))
    await callback.answer()


@mob_router.callback_query(MobStates.edit_select, F.data.startswith("mob_page_"))
async def mob_list_page(callback: types.CallbackQuery, state: FSMContext) -> None:
    _, _, raw_location_id, raw_page = get_callback_data(callback).split("_")
    location_id, page = int(raw_location_id), int(raw_page)
    await state.update_data(mob_location_id=location_id)
    location = await db.get_location_by_id(location_id)
    loc = location or {'name': 'Локация', 'emoji': '📍'}
    await present_mob(callback, state, f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:", reply_markup=await get_mob_list_keyboard(location_id, page))
    await callback.answer()


@mob_router.callback_query(MobStates.edit_select, F.data == "back_to_mob_locations")
async def back_to_mob_locations(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.update_data(mob_location_id=None)
    await present_mob(callback, state, "🐾 Управление мобами:\nВыберите локацию:", reply_markup=await get_mob_locations_keyboard())
    await callback.answer()

@mob_router.callback_query(MobStates.edit_select, F.data.startswith("edit_mob_"))
async def mob_edit_menu(callback: types.CallbackQuery, state: FSMContext) -> None:
    mob_id = int(get_callback_data(callback).split("_")[2])
    mob = await get_mob_edit_data(mob_id)
    if not mob:
        await present_mob(callback, state, "Моб не найден.")
        await callback.answer()
        return
    await state.update_data(mob_id=mob_id)
    await present_mob(callback, state, f"Редактирование моба ID {mob_id}", reply_markup=build_mob_edit_keyboard(mob))
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@mob_router.callback_query(MobStates.edit_field, F.data.startswith("mob_edit_field_"))
async def mob_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    field = get_callback_data(callback).split("_", 3)[3]
    await state.update_data(edit_field=field)
    if field == 'location_id':
        locations = await get_sorted_locations()
        if not locations:
            await callback.answer("Нет доступных локаций.", show_alert=True)
            return
        await present_mob(callback, state, "Выберите новую локацию:", reply_markup=await get_location_choice_keyboard(
                "mob_edit_location_",
                cancel_callback="mob_location_change_cancel",
                locations=locations,
            ))
        await state.set_state(MobStates.edit_new_value)
        await callback.answer()
        return
    await present_mob(callback, state, f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(MobStates.edit_new_value)
    await callback.answer()


@mob_router.callback_query(MobStates.edit_new_value, F.data.startswith("mob_edit_location_"))
async def mob_update_location(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mob_id = data.get('mob_id')
    location_id = int(get_callback_data(callback).removeprefix("mob_edit_location_"))
    location = await db.get_location_by_id(location_id)
    if not mob_id or not location:
        await callback.answer("Моб или локация не найдены.", show_alert=True)
        return

    await db.update_mob_field(mob_id, 'location_id', location_id)
    mob = await get_mob_edit_data(mob_id) if isinstance(mob_id, int) else None
    if not mob:
        await present_mob(callback, state, "❌ Моб не найден.")
        await state.clear()
        await callback.answer()
        return

    await state.update_data(mob_location_id=location_id, edit_field=None)
    await state.set_state(MobStates.edit_field)
    await present_mob(callback, state, f"Редактирование моба ID {mob_id}", reply_markup=build_mob_edit_keyboard(mob))
    await callback.answer("✅ Локация обновлена")


@mob_router.callback_query(MobStates.edit_new_value, F.data == "mob_location_change_cancel")
async def mob_edit_location_cancel(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mob_id = data.get('mob_id')
    mob = await get_mob_edit_data(mob_id) if isinstance(mob_id, int) else None
    if not mob:
        await present_mob(callback, state, "❌ Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(edit_field=None)
    await state.set_state(MobStates.edit_field)
    await present_mob(callback, state, f"Редактирование моба ID {mob['id']}", reply_markup=build_mob_edit_keyboard(mob))
    await callback.answer()

@mob_router.message(MobStates.edit_new_value, F.text, ~F.text.startswith('/'))
async def mob_update_field(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    data = await state.get_data()
    mob_id = data.get('mob_id')
    field = data.get('edit_field')

    if not mob_id or not field:
        logger.warning(f"Ошибка состояния: mob_id={mob_id}, field={field}. Данные состояния: {data}")
        await present_mob(message, state, "❌ Ошибка состояния. Пожалуйста, начните редактирование моба заново.\n"
            "Выберите моба из списка:")
        await state.clear()
        keyboard = await get_mob_locations_keyboard()
        await present_mob(message, state, "Выберите моба для редактирования:", reply_markup=keyboard)
        await state.set_state(MobStates.edit_select)
        return

    new_value: str | int = get_message_text(message).strip()

    if field == 'location_id':
        await present_mob(message, state, "Выберите локацию кнопкой:", reply_markup=await get_location_choice_keyboard(
                "mob_edit_location_",
                cancel_callback="mob_location_change_cancel",
            ))
        return

    if field in ('hp', 'dust_min', 'dust_max', 'exp'):
        try:
            new_value = int(new_value)
            if new_value < 0:
                raise ValueError
        except ValueError:
            await present_mob(message, state, "❌ Введите положительное целое число.")
            return

    if field == 'emoji' and (not isinstance(new_value, str) or not is_valid_emoji(new_value)):
        await present_mob(message, state, "❌ Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return

    if field == 'name' and not new_value:
        await present_mob(message, state, "❌ Имя не может быть пустым.")
        return

    if field in ('dust_min', 'dust_max'):
        current_mob = await get_mob_edit_data(mob_id)
        if current_mob:
            dust_min = int(new_value) if field == 'dust_min' else current_mob['dust_min']
            dust_max = int(new_value) if field == 'dust_max' else current_mob['dust_max']
            if dust_min > dust_max:
                await present_mob(message, state, "❌ dust_min не может быть больше dust_max.")
                return

    try:
        await db.update_mob_field(mob_id, field, new_value)

        mob = await get_mob_edit_data(mob_id)
        if not mob:
            await present_mob(message, state, "❌ Моб не найден. Возврат в админку.")
            await state.clear()
            await present_mob(message, state, "🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            return

        await present_mob(message, state, f"✅ Поле {field} обновлено.")
        await present_mob(message, state, f"Редактирование моба ID {mob_id}", reply_markup=build_mob_edit_keyboard(mob))

        await state.update_data(edit_field=None)
        await state.set_state(MobStates.edit_field)

        try:
            await message.delete()
        except TelegramAPIError:
            pass

    except Exception as e:
        await present_mob(message, state, f"❌ Ошибка: {e}")
        logger.exception("Ошибка при обновлении поля моба")

@mob_router.callback_query(F.data == "back_to_mob_list")
async def back_to_mob_list_from_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    location_id = data.get('mob_location_id')
    if not location_id and data.get('mob_id'):
        row = await db.execute_query("SELECT location_id FROM mobs WHERE id = ?", (data['mob_id'],))
        location_id = row[0]['location_id'] if row else None
    await state.clear()
    await state.set_state(MobStates.edit_select)
    if location_id:
        await state.update_data(mob_location_id=location_id)
        loc = await db.get_location_by_id(location_id) or {'name': 'Локация', 'emoji': '📍'}
        await present_mob(callback, state, f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:", reply_markup=await get_mob_list_keyboard(location_id, 1))
    else:
        await present_mob(callback, state, "🐾 Управление мобами:\nВыберите локацию:", reply_markup=await get_mob_locations_keyboard())
    await callback.answer()

@mob_router.callback_query(MobStates.edit_field, F.data == "mob_delete")
async def mob_delete_confirm(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mob_id = data['mob_id']
    mob = await db.execute_query("SELECT name FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await present_mob(callback, state, "Моб не найден.")
        await callback.answer()
        return
    confirmation_callback = await prepare_delete_confirmation(
        callback, state, 'mob', mob_id, 'confirm_mob_delete_',
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=confirmation_callback)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_mob_list")]
    ])
    await present_mob(callback, state, f"Удалить моба <b>{escape_html(mob[0]['name'])}</b>?", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(MobStates.delete_confirm)
    await callback.answer()

@mob_router.callback_query(F.data.startswith("confirm_mob_delete"))
async def mob_delete_execute(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not await consume_delete_confirmation(
        callback, state, 'mob', mob_id, 'confirm_mob_delete_', MobStates.delete_confirm,
    ) or not isinstance(mob_id, int):
        return
    await db.delete_mob(mob_id)
    await present_mob(callback, state, "✅ Моб удалён.")
    keyboard = await get_mob_locations_keyboard()
    await present_mob(callback, state, "🐾 Управление мобами:\nВыберите моба или добавьте нового:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)
    await callback.answer()

# ------------------- Управление дропом -------------------
def build_drop_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Поиск лута по названию", callback_data="drop_search_start")],
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🃏 Карты", callback_data="drop_category_card")],
        [InlineKeyboardButton(text="🔙 Назад к мобу", callback_data="back_to_mob_edit")],
    ])

def build_drop_filters_keyboard(category: str) -> InlineKeyboardMarkup:
    options = get_drop_filter_options(category)
    rows = [[InlineKeyboardButton(
        text=label,
        callback_data=f"drop_filter_{category}_{index}",
    )] for index, (_, label) in enumerate(options)]
    rows.append([InlineKeyboardButton(text="🔙 Назад к типам дропа", callback_data="back_to_drop_categories")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_drop_filter_options(category: str) -> list[tuple[str, str]]:
    if category == 'resource':
        return RESOURCE_TYPES
    if category in {'gear', 'card'}:
        return [(slot, GEAR_SLOT_LABELS[slot]) for slot in GEAR_SLOTS]
    raise ValueError(f"Unknown drop category: {category}")


def resolve_drop_filter(category: str, filter_index: int) -> str:
    return get_drop_filter_options(category)[filter_index][0]

async def get_drop_list_keyboard(mob_id: int, category: str, filter_value: str, page: int) -> InlineKeyboardMarkup:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items: Sequence[EntityRow]
    if category == 'resource':
        items = await db.get_resources_by_type(
            filter_value,
            offset,
            ADMIN_ITEMS_PER_PAGE + 1,
        )
    elif category == 'gear':
        items = await db.get_gear_by_slot(
            filter_value,
            offset,
            ADMIN_ITEMS_PER_PAGE + 1,
        )
    else:
        sql = "SELECT id, name, emoji, slot FROM cards WHERE slot = ? ORDER BY LOWER_UNICODE(name), id LIMIT ? OFFSET ?"
        params = (filter_value, ADMIN_ITEMS_PER_PAGE + 1, offset)
        items = await db.execute_query(sql, params)
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    enabled_ids = await db.get_enabled_drop_ids(
        mob_id,
        category,
        [item['id'] for item in items],
    )
    rows = []
    for item in items:
        status = '✅' if item['id'] in enabled_ids else '❌'
        rarity = RARITY_EMOJIS.get(item.get('rarity') or '', '') if category == 'gear' else ''
        label = f"{status} {rarity} {item.get('emoji') or ''} {item['name']}".replace('  ', ' ').strip()
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data=f"drop_toggle_{category}_{item['id']}_{page}",
        )])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(
            text="◀️ Назад",
            callback_data=f"drop_page_{category}_{page-1}",
        ))
    if has_next:
        nav.append(InlineKeyboardButton(
            text="Вперед ▶️",
            callback_data=f"drop_page_{category}_{page+1}",
        ))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад к категориям", callback_data=f"back_to_drop_filters_{category}")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_drop_search_keyboard(items: Sequence[EntityRow]) -> InlineKeyboardMarkup:
    category_icons = {'resource': '📦', 'gear': '⚔️', 'card': '🃏'}
    rows = []
    for item in items:
        status = '✅' if item['enabled'] else '❌'
        category_icon = category_icons[item['item_type']]
        rarity_icon = RARITY_EMOJIS.get(item.get('rarity') or '', '')
        label = (
            f"{status} {category_icon} {rarity_icon} "
            f"{item.get('emoji') or ''} {item['name']}"
        ).replace('  ', ' ').strip()
        if len(label) > 60:
            label = f"{label[:57]}…"
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"drop_search_toggle_{item['item_type']}_{item['id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="🔎 Другой запрос", callback_data="drop_search_again")
    ])
    rows.append([
        InlineKeyboardButton(text="🔙 Назад к типам дропа", callback_data="drop_search_back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@mob_router.callback_query(MobStates.edit_field, F.data == "mob_drop_menu")
async def mob_drop_category(callback: types.CallbackQuery, state: FSMContext) -> None:
    await present_mob(callback, state, "Выберите тип дропа:", reply_markup=build_drop_categories_keyboard())
    await state.set_state(MobStates.drop_category)
    await callback.answer()


@mob_router.callback_query(MobStates.drop_category, F.data == "drop_search_start")
async def start_drop_search(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.update_data(drop_search_query=None)
    await present_mob(callback, state, "🔎 Введите часть названия ресурса, экипировки или карты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="drop_search_back")]
        ]))
    await state.set_state(MobStates.drop_search)
    await callback.answer()


@mob_router.message(MobStates.drop_search, F.text, ~F.text.startswith('/'))
async def show_drop_search_results(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    query = get_message_text(message).strip()
    if not query:
        await present_mob(message, state, "Введите хотя бы один символ для поиска.")
        return
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await state.clear()
        await present_mob(message, state, "Моб не выбран. Откройте управление дропом заново.")
        return
    items = await db.search_drop_items(mob_id, query, limit=20)
    await state.update_data(drop_search_query=query)
    text = (
        f"🔎 Результаты по запросу «{query}»:\n"
        "✅ — уже падает, ❌ — не падает. Нажмите на предмет для переключения."
        if items
        else f"По запросу «{query}» ничего не найдено."
    )
    await present_mob(message, state, text, reply_markup=build_drop_search_keyboard(items))


@mob_router.callback_query(MobStates.drop_search, F.data == "drop_search_again")
async def repeat_drop_search(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.update_data(drop_search_query=None)
    await present_mob(callback, state, "🔎 Введите новый поисковый запрос:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="drop_search_back")]
        ]))
    await callback.answer()


@mob_router.callback_query(MobStates.drop_search, F.data.startswith("drop_search_toggle_"))
async def toggle_drop_from_search(callback: types.CallbackQuery, state: FSMContext) -> None:
    _, _, _, category, raw_item_id = get_callback_data(callback).split("_")
    item_id = int(raw_item_id)
    data = await state.get_data()
    mob_id = data.get('mob_id')
    query = data.get('drop_search_query')
    if not mob_id or not query:
        await callback.answer("Поиск устарел. Введите запрос заново.", show_alert=True)
        return

    if await db.get_drop_status(mob_id, category, item_id):
        await db.remove_drop(mob_id, category, item_id)
        await callback.answer("❌ Дроп убран")
    else:
        await db.add_drop(mob_id, category, item_id)
        await callback.answer("✅ Дроп добавлен")

    items = await db.search_drop_items(mob_id, query, limit=20)
    await present_mob(callback, state, get_callback_message(callback).text or 'Выберите действие:', reply_markup=build_drop_search_keyboard(items))


@mob_router.callback_query(MobStates.drop_search, F.data == "drop_search_back")
async def back_from_drop_search(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.update_data(drop_search_query=None)
    await present_mob(callback, state, "Выберите тип дропа:", reply_markup=build_drop_categories_keyboard())
    await state.set_state(MobStates.drop_category)
    await callback.answer()

@mob_router.callback_query(MobStates.drop_category, F.data == "back_to_mob_edit")
async def back_to_mob_edit_from_drop_category(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    mob_id = data.get('mob_id')
    mob = await get_mob_edit_data(mob_id) if isinstance(mob_id, int) else None
    if not mob:
        await present_mob(callback, state, "❌ Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    await present_mob(callback, state, f"Редактирование моба ID {mob_id}", reply_markup=build_mob_edit_keyboard(mob))
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@mob_router.callback_query(MobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_filters(callback: types.CallbackQuery, state: FSMContext) -> None:
    category = get_callback_data(callback).split('_')[2]
    await present_mob(callback, state, "Выберите категорию:", reply_markup=build_drop_filters_keyboard(category))
    await state.update_data(drop_category=category)
    await callback.answer()

@mob_router.callback_query(MobStates.drop_category, F.data.startswith("drop_filter_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    _, _, category, raw_filter_index = get_callback_data(callback).split('_')
    filter_index = int(raw_filter_index)
    filter_value = resolve_drop_filter(category, filter_index)
    data = await state.get_data()
    keyboard = await get_drop_list_keyboard(data['mob_id'], category, filter_value, 1)
    await present_mob(callback, state, "✅ — падает, ❌ — не падает", reply_markup=keyboard)
    await state.update_data(
        drop_category=category,
        drop_filter_index=filter_index,
        drop_filter_value=filter_value,
        drop_page=1,
    )
    await state.set_state(MobStates.drop_list_page)
    await callback.answer()

@mob_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_page_"))
async def drop_page(callback: types.CallbackQuery, state: FSMContext) -> None:
    _, _, category, raw_page = get_callback_data(callback).split('_')
    page = int(raw_page)
    data = await state.get_data()
    keyboard = await get_drop_list_keyboard(
        data['mob_id'],
        category,
        data['drop_filter_value'],
        page,
    )
    await present_mob(callback, state, get_callback_message(callback).text or 'Выберите действие:', reply_markup=keyboard)
    await state.update_data(drop_page=page)
    await callback.answer()

@mob_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_toggle_"))
async def toggle_drop(callback: types.CallbackQuery, state: FSMContext) -> None:
    _, _, category, raw_item_id, raw_page = get_callback_data(callback).split('_')
    item_id = int(raw_item_id)
    page = int(raw_page)
    data = await state.get_data()
    mob_id = data['mob_id']
    if await db.get_drop_status(mob_id, category, item_id):
        await db.remove_drop(mob_id, category, item_id)
        await callback.answer("❌ Дроп убран")
    else:
        await db.add_drop(mob_id, category, item_id)
        await callback.answer("✅ Дроп добавлен")
    keyboard = await get_drop_list_keyboard(
        mob_id,
        category,
        data['drop_filter_value'],
        page,
    )
    await present_mob(callback, state, get_callback_message(callback).text or 'Выберите действие:', reply_markup=keyboard)

@mob_router.callback_query(MobStates.drop_list_page, F.data.startswith("back_to_drop_filters_"))
async def back_to_drop_filters(callback: types.CallbackQuery, state: FSMContext) -> None:
    category = get_callback_data(callback).rsplit('_', 1)[1]
    await present_mob(callback, state, "Выберите категорию:", reply_markup=build_drop_filters_keyboard(category))
    await state.set_state(MobStates.drop_category)
    await callback.answer()

@mob_router.callback_query(MobStates.drop_category, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext) -> None:
    await present_mob(callback, state, "Выберите тип дропа:", reply_markup=build_drop_categories_keyboard())
    await callback.answer()

# ---------- Добавление моба ----------
@mob_router.callback_query(MobStates.edit_select, F.data == "mob_add_start")
async def start_add_mob(callback: types.CallbackQuery, state: FSMContext) -> None:
    await present_mob(callback, state, "Введите название моба:")
    await state.set_state(MobStates.add_name)
    await callback.answer()

@mob_router.message(MobStates.add_name, F.text, ~F.text.startswith('/'))
async def add_mob_name(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    await state.update_data(name=get_message_text(message).strip())
    await present_mob(message, state, "Введите эмодзи моба:")
    await state.set_state(MobStates.add_emoji)

@mob_router.message(MobStates.add_emoji, F.text, ~F.text.startswith('/'))
async def add_mob_emoji(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    emoji = get_message_text(message).strip()
    if not is_valid_emoji(emoji):
        await present_mob(message, state, "Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(emoji=emoji)
    await present_mob(message, state, "Введите HP:")
    await state.set_state(MobStates.add_hp)

@mob_router.message(MobStates.add_hp, F.text, ~F.text.startswith('/'))
async def add_mob_hp(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    try:
        hp = int(get_message_text(message).strip())
        if hp < 0:
            raise ValueError
    except (TypeError, ValueError):
        await present_mob(message, state, "Введите целое положительное число.")
        return
    await state.update_data(hp=hp)
    await present_mob(message, state, "Введите dust_min:")
    await state.set_state(MobStates.add_dust_min)

@mob_router.message(MobStates.add_dust_min, F.text, ~F.text.startswith('/'))
async def add_mob_dust_min(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    try:
        dust_min = int(get_message_text(message).strip())
        if dust_min < 0:
            raise ValueError
    except (TypeError, ValueError):
        await present_mob(message, state, "Введите целое положительное число.")
        return
    await state.update_data(dust_min=dust_min)
    await present_mob(message, state, "Введите dust_max:")
    await state.set_state(MobStates.add_dust_max)

@mob_router.message(MobStates.add_dust_max, F.text, ~F.text.startswith('/'))
async def add_mob_dust_max(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    try:
        dust_max = int(get_message_text(message).strip())
        if dust_max < 0:
            raise ValueError
    except (TypeError, ValueError):
        await present_mob(message, state, "Введите целое положительное число.")
        return
    data = await state.get_data()
    if dust_max < data['dust_min']:
        await present_mob(message, state, "dust_max не может быть меньше dust_min")
        return
    await state.update_data(dust_max=dust_max)
    await present_mob(message, state, "Введите опыт (exp):")
    await state.set_state(MobStates.add_exp)

@mob_router.message(MobStates.add_exp, F.text, ~F.text.startswith('/'))
async def add_mob_exp(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    try:
        exp = int(get_message_text(message).strip())
        if exp < 0:
            raise ValueError
    except (TypeError, ValueError):
        await present_mob(message, state, "Введите целое положительное число.")
        return
    await state.update_data(exp=exp)
    locations = await get_sorted_locations()
    if not locations:
        await present_mob(message, state, "Нет локаций.")
        await state.clear()
        return
    keyboard = await get_location_choice_keyboard("mob_add_location_", locations=locations)
    await present_mob(message, state, "Выберите локацию:", reply_markup=keyboard)
    await state.set_state(MobStates.add_location)

@mob_router.callback_query(MobStates.add_location, F.data.startswith("mob_add_location_"))
async def add_mob_location(callback: types.CallbackQuery, state: FSMContext) -> None:
    location_id = int(get_callback_data(callback).removeprefix("mob_add_location_"))
    if not await db.get_location_by_id(location_id):
        await callback.answer("Локация не найдена.", show_alert=True)
        return
    data = await state.get_data()
    try:
        await db.execute_query(
            "INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id) VALUES (?,?,?,?,?,?,?)",
            (data['name'], data['emoji'], data['hp'], data['dust_min'], data['dust_max'], data['exp'], location_id)
        )
    except Exception as e:
        await present_mob(callback, state, f"❌ Ошибка: {e}")
        return
    await state.clear()
    keyboard = await get_mob_locations_keyboard()
    await present_mob(callback, state, "🐾 Управление мобами:\nВыберите моба или добавьте нового:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)
    await callback.answer()


# ============================================================
