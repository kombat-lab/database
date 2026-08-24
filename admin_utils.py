import logging

from aiogram import F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

from utils import RICH_TABLE_OPEN, escape_html, is_valid_emoji

logger = logging.getLogger(__name__)

ADMIN_ITEMS_PER_PAGE = 10
OPTIONAL_NOTE_SKIP_CALLBACK = "optional_note_skip"
OPTIONAL_NOTE_PROMPT = "Введите примечание или нажмите «Без примечания»:"


def build_optional_note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="⏭ Без примечания",
            callback_data=OPTIONAL_NOTE_SKIP_CALLBACK,
        )
    ]])


def normalize_optional_note(value: str) -> str:
    value = value.strip()
    return "" if value == "-" else value


async def edit_admin_rich(callback: types.CallbackQuery, html: str,
                          reply_markup: InlineKeyboardMarkup = None,
                          fallback_html: str = None):
    """Редактирует экран админки как Rich Message с HTML fallback."""
    try:
        return await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            rich_message=InputRichMessage(html=html),
            reply_markup=reply_markup,
        )
    except TelegramAPIError as error:
        if isinstance(error, TelegramBadRequest) and "message is not modified" in str(error).lower():
            return callback.message
        logger.info("Rich admin screen fallback: %s", error)
        return await callback.message.edit_text(
            fallback_html or html,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

class GenericEditStates(StatesGroup):
    select_field = State()
    new_value = State()
    confirm_delete = State()
    select_option = State()

def get_admin_main_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🐾 Управление мобами", callback_data="admin_edit_mob")],
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="admin_manage_resources")],
        [InlineKeyboardButton(text="⚔️ Управление снаряжением", callback_data="admin_manage_gear")],
        [InlineKeyboardButton(text="🃏 Управление картами", callback_data="admin_manage_cards")],
        [InlineKeyboardButton(text="📜 Управление рецептами", callback_data="admin_manage_recipes")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def admin_close(callback: types.CallbackQuery):
    try:
        await callback.message.delete()
    except (AttributeError, TelegramAPIError):
        pass
    await callback.answer()

async def admin_cancel_edit(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
    await callback.answer()

def build_paginated_keyboard(items, page, has_next, item_callback_prefix, extra_buttons=None):
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
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_{page+1}"))
    if nav:
        keyboard.append(nav)
    
    if extra_buttons:
        for btn_row in extra_buttons:
            keyboard.append(btn_row)
    
    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def render_entity_list(callback: types.CallbackQuery, state: FSMContext, entity_config: dict, page: int = 1):
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items = await entity_config['get_page_func'](offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    
    display_mapping = entity_config.get('display_mapping', {})
    for item in items:
        for field, mapping in display_mapping.items():
            if field in item:
                item[field] = mapping.get(item[field], item[field])
    
    extra = []
    if entity_config.get('add_button'):
        extra.append([InlineKeyboardButton(text=entity_config['add_button_text'], callback_data=entity_config['add_callback'])])
    
    keyboard = build_paginated_keyboard(
        items, page, has_next,
        entity_config['item_callback_prefix'],
        extra_buttons=extra
    )
    await callback.message.edit_text(entity_config['list_title'], reply_markup=keyboard)
    await state.update_data(editing_entity=entity_config['name'], current_page=page)
    await state.set_state(entity_config['list_state'])
    await callback.answer()

def build_edit_menu(
    entity_id: int,
    entity_config: dict,
    entity_data: dict,
) -> tuple[str, str, InlineKeyboardMarkup]:
    fields = entity_config['edit_fields']
    display_mapping = entity_config.get('display_mapping', {})
    keyboard = []
    rich_rows = []
    fallback_lines = []
    for field_name, field_label in fields:
        current_value = entity_data.get(field_name, '?')
        formatter = entity_config.get('field_formatters', {}).get(field_name)
        if formatter:
            current_value = formatter(current_value)
        elif field_name in display_mapping:
            current_value = display_mapping[field_name].get(current_value, current_value)
        rich_rows.append(
            f"<tr><td>{escape_html(field_label)}</td><td>{escape_html(current_value)}</td></tr>"
        )
        fallback_lines.append(f"{escape_html(field_label)}: {escape_html(current_value)}")
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
    
    fallback_text = (
        f"<b>✏️ Редактирование: {escape_html(entity_config['name_ru'])} · ID {entity_id}</b>\n"
        + "\n".join(fallback_lines)
    )
    rich_html = (
        f"<b>✏️ Редактирование: {escape_html(entity_config['name_ru'])} · ID {entity_id}</b>"
        f"{RICH_TABLE_OPEN}<tbody><tr><th>Поле</th><th>Значение</th></tr>"
        + "".join(rich_rows) + "</tbody></table>"
    )
    return fallback_text, rich_html, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_edit_menu(callback: types.CallbackQuery, state: FSMContext, entity_id: int, entity_config: dict, entity_data: dict):
    fallback_text, rich_html, reply_markup = build_edit_menu(
        entity_id,
        entity_config,
        entity_data,
    )
    await edit_admin_rich(
        callback,
        rich_html,
        reply_markup,
        fallback_html=fallback_text,
    )
    await state.update_data(entity_id=entity_id, editing_entity=entity_config['name'])
    await state.set_state(GenericEditStates.select_field)
    await callback.answer()

def register_generic_handlers(router: Router, get_entity_configs_func):
    """
    Регистрирует универсальные обработчики на роутере.
    get_entity_configs_func должна возвращать словарь ENTITY_CONFIGS.
    """

    async def update_entity_field(config, entity_id, field, value):
        database_field = config.get('field_aliases', {}).get(field, field)
        await config['update_func'](entity_id, **{database_field: value})

    @router.callback_query(GenericEditStates.select_field, F.data.startswith("edit_field_"))
    async def generic_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext):
        field = callback.data.split("_")[2]
        data = await state.get_data()
        entity_type = data['editing_entity']
        configs = get_entity_configs_func()
        config = configs[entity_type]
        editable_fields = {name for name, _ in config['edit_fields']}
        if field not in editable_fields:
            await callback.answer("Неизвестное поле.", show_alert=True)
            return
        
        select_options = config.get('select_options', {}).get(field)
        if select_options:
            display_mapping = config.get('display_mapping', {}).get(field, {})
            keyboard = []
            for option in select_options:
                display_text = display_mapping.get(option, option)
                keyboard.append([InlineKeyboardButton(text=display_text, callback_data=f"select_opt_{field}_{option}")])
            keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_edit_menu")])
            await callback.message.edit_text(
                f"Выберите значение для поля <b>{field}</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
            await state.update_data(edit_field=field)
            await state.set_state(GenericEditStates.select_option)
        else:
            if field == 'note':
                await callback.message.edit_text(
                    OPTIONAL_NOTE_PROMPT,
                    reply_markup=build_optional_note_keyboard(),
                )
            else:
                await callback.message.edit_text(
                    f"Введите новое значение для поля <b>{field}</b>:",
                    parse_mode="HTML",
                )
            await state.update_data(edit_field=field)
            await state.set_state(GenericEditStates.new_value)
        await callback.answer()

    @router.callback_query(
        GenericEditStates.new_value,
        F.data == OPTIONAL_NOTE_SKIP_CALLBACK,
    )
    async def generic_skip_optional_note(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        if data.get('edit_field') != 'note':
            await callback.answer("Эта кнопка доступна только для примечания.", show_alert=True)
            return

        entity_type = data.get('editing_entity')
        entity_id = data.get('entity_id')
        configs = get_entity_configs_func()
        config = configs.get(entity_type)
        if not config or not entity_id:
            await callback.message.edit_text(
                "🔧 Админ-панель",
                reply_markup=get_admin_main_keyboard(),
            )
            await state.clear()
            await callback.answer()
            return

        try:
            await update_entity_field(config, entity_id, 'note', '')
        except Exception as error:
            logger.exception("Не удалось очистить примечание")
            await callback.answer(f"Ошибка: {error}", show_alert=True)
            return

        entity_data = await config['get_by_id_func'](entity_id)
        if not entity_data:
            await callback.message.edit_text("❌ Сущность не найдена.")
            await state.clear()
            await callback.answer()
            return

        await show_edit_menu(callback, state, entity_id, config, entity_data)

    @router.callback_query(GenericEditStates.select_option, F.data == "back_to_edit_menu")
    async def back_to_edit_menu_from_options(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        entity_type = data.get('editing_entity')
        entity_id = data.get('entity_id')
        if not entity_type or not entity_id:
            await callback.message.edit_text("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            await state.clear()
            await callback.answer()
            return
        configs = get_entity_configs_func()
        config = configs[entity_type]
        entity_data = await config['get_by_id_func'](entity_id)
        if not entity_data:
            await callback.message.edit_text("❌ Сущность не найдена. Возврат в список.")
            await render_entity_list(callback, state, config, 1)
            return
        await show_edit_menu(callback, state, entity_id, config, entity_data)
    
    @router.callback_query(GenericEditStates.select_option, F.data.startswith("select_opt_"))
    async def generic_select_option(callback: types.CallbackQuery, state: FSMContext):
        parts = callback.data.split("_")
        field = parts[2]
        value = "_".join(parts[3:])
        
        data = await state.get_data()
        entity_type = data['editing_entity']
        entity_id = data['entity_id']
        
        configs = get_entity_configs_func()
        config = configs[entity_type]
        allowed_values = config.get('select_options', {}).get(field)
        if not allowed_values or value not in allowed_values:
            await callback.answer("Недопустимое значение.", show_alert=True)
            return
        
        try:
            await update_entity_field(config, entity_id, field, value)
            await callback.message.edit_text(
                f"✅ Поле <b>{escape_html(field)}</b> обновлено на <code>{escape_html(value)}</code>.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Не удалось обновить поле %s", field)
            await callback.message.edit_text(f"❌ Ошибка: {e}")
            await callback.answer()
            return
        
        entity_data = await config['get_by_id_func'](entity_id)
        if not entity_data:
            await callback.message.answer("❌ Сущность не найдена.")
            await render_entity_list(callback, state, config, 1)
            return
        
        await show_edit_menu(callback, state, entity_id, config, entity_data)

    @router.message(GenericEditStates.new_value, F.text)
    async def generic_update_field(message: types.Message, state: FSMContext):
        data = await state.get_data()
        
        if 'edit_field' not in data:
            await message.answer("❌ Ошибка состояния. Возврат в админку.")
            await state.clear()
            await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            return
        
        entity_type = data['editing_entity']
        entity_id = data['entity_id']
        field = data['edit_field']
        new_value = message.text.strip()
    
        configs = get_entity_configs_func()
        config = configs[entity_type]

        if field == 'note':
            new_value = normalize_optional_note(new_value)
    
        if field in config.get('integer_fields', []):
            minimum = config.get('integer_minimums', {}).get(field, 0)
            try:
                new_value = int(new_value)
                if new_value < minimum:
                    raise ValueError
            except (TypeError, ValueError):
                await message.answer(
                    f"❌ Ошибка: введите целое число не меньше {minimum}."
                )
                return
    
        if field == 'emoji' and not is_valid_emoji(new_value):
            await message.answer("❌ Эмодзи не может быть пустым.")
            return
    
        if field == 'name' and not new_value:
            await message.answer("❌ Название не может быть пустым.")
            return
    
        try:
            await update_entity_field(config, entity_id, field, new_value)
            await message.answer(
                f"✅ Поле <b>{escape_html(field)}</b> обновлено на <code>{escape_html(new_value)}</code>.",
                parse_mode="HTML",
            )
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}")
            return
    
        entity_data = await config['get_by_id_func'](entity_id)
        if not entity_data:
            await message.answer("❌ Сущность не найдена. Возврат в админку.")
            await state.clear()
            await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            return
    
        fallback_text, _, reply_markup = build_edit_menu(
            entity_id,
            config,
            entity_data,
        )
        await message.answer(
            fallback_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        await message.delete()
    
        await state.update_data(entity_id=entity_id, editing_entity=config['name'])
        await state.set_state(GenericEditStates.select_field)

    @router.callback_query(GenericEditStates.select_field, F.data == "delete_entity")
    async def generic_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        entity_type = data['editing_entity']
        entity_id = data['entity_id']
        configs = get_entity_configs_func()
        config = configs[entity_type]
        entity = await config['get_by_id_func'](entity_id)
        if not entity:
            await callback.message.edit_text("❌ Сущность не найдена.")
            await callback.answer()
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete_yes")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_list")]
        ])
        await callback.message.edit_text(
            f"⚠️ Удалить {escape_html(config['name_ru'])} "
            f"<b>{escape_html(entity['name'])}</b> (ID {entity_id})?\n"
            "Это действие необратимо.",
            parse_mode="HTML", reply_markup=keyboard
        )
        await state.set_state(GenericEditStates.confirm_delete)
        await callback.answer()

    @router.callback_query(GenericEditStates.confirm_delete, F.data == "confirm_delete_yes")
    async def generic_delete_execute(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        entity_type = data['editing_entity']
        entity_id = data['entity_id']
        configs = get_entity_configs_func()
        config = configs[entity_type]
        try:
            await config['delete_func'](entity_id)
            await callback.message.edit_text("✅ Успешно удалено.")
        except Exception as e:
            await callback.message.edit_text(f"❌ Ошибка: {e}")
        back_to_list_func = config.get('back_to_list_func')
        if back_to_list_func:
            await back_to_list_func(callback, state, data)
        else:
            await render_entity_list(callback, state, config, 1)

    @router.callback_query(
        StateFilter(GenericEditStates.select_field, GenericEditStates.confirm_delete),
        F.data == "back_to_list"
    )
    async def generic_back_to_list(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        entity_type = data.get('editing_entity')
        configs = get_entity_configs_func()
        if entity_type in configs:
            config = configs[entity_type]
            back_to_list_func = config.get('back_to_list_func')
            if back_to_list_func:
                await back_to_list_func(callback, state, data)
            else:
                await render_entity_list(
                    callback,
                    state,
                    config,
                    data.get('current_page', 1),
                )
        else:
            await callback.message.edit_text("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
        await callback.answer()
