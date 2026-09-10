import logging
from collections.abc import Callable, Sequence

from admin_contracts import EntityConfig, EntityRow
from telegram_helpers import get_bound_bot, get_callback_data, get_callback_message, get_message_text
import secrets

from aiogram import F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, InputRichMessage

from utils import escape_html, is_valid_emoji

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
                          reply_markup: InlineKeyboardMarkup | None = None,
                          fallback_html: str | None = None) -> types.Message | bool:
    """Редактирует экран админки как Rich Message с HTML fallback."""
    try:
        return await get_bound_bot(callback).edit_message_text(
            chat_id=get_callback_message(callback).chat.id,
            message_id=get_callback_message(callback).message_id,
            rich_message=InputRichMessage(html=html),
            reply_markup=reply_markup,
        )
    except TelegramAPIError as error:
        if isinstance(error, TelegramBadRequest) and "message is not modified" in str(error).lower():
            return get_callback_message(callback)
        logger.info("Rich admin screen fallback: %s", error)
        return await get_callback_message(callback).edit_text(
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

async def admin_close(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        if isinstance(callback.message, types.Message):
            await callback.message.delete()
    except (AttributeError, TelegramAPIError):
        pass
    await callback.answer()

async def prepare_delete_confirmation(
    callback: types.CallbackQuery, state: FSMContext, kind: str, entity_id: int,
    prefix: str, context: int | None = None,
) -> str:
    """Bind a one-use confirmation to the exact object and Telegram message."""
    token = secrets.token_urlsafe(9)
    await state.update_data(admin_delete_confirmation={
        'token': token,
        'kind': kind,
        'entity_id': entity_id,
        'context': context,
        'chat_id': get_callback_message(callback).chat.id,
        'message_id': get_callback_message(callback).message_id,
    })
    return f"{prefix}{token}"


async def consume_delete_confirmation(
    callback: types.CallbackQuery, state: FSMContext, kind: str, entity_id: int | None,
    prefix: str, expected_state: State, context: int | None = None,
) -> bool:
    data = await state.get_data()
    confirmation = data.get('admin_delete_confirmation') or {}
    message = callback.message
    valid = (
        message is not None
        and await state.get_state() == expected_state.state
        and confirmation.get('kind') == kind
        and confirmation.get('entity_id') == entity_id
        and confirmation.get('context') == context
        and confirmation.get('chat_id') == message.chat.id
        and confirmation.get('message_id') == message.message_id
        and callback.data == f"{prefix}{confirmation.get('token', '')}"
        and bool(confirmation.get('token'))
    )
    if not valid:
        await callback.answer("Подтверждение устарело. Откройте удаление заново.", show_alert=True)
        return False
    await state.update_data(admin_delete_confirmation=None)
    return True


async def admin_cancel_edit(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await get_callback_message(callback).edit_text("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
    await callback.answer()

def build_paginated_keyboard(items: Sequence[EntityRow], page: int, has_next: bool, item_callback_prefix: str, extra_buttons: list[list[InlineKeyboardButton]] | None = None) -> InlineKeyboardMarkup:
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

async def render_entity_list(callback: types.CallbackQuery, state: FSMContext, entity_config: EntityConfig, page: int = 1) -> None:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    page_items = await entity_config['get_page_func'](offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(page_items) > ADMIN_ITEMS_PER_PAGE
    items = [dict(item) for item in page_items[:ADMIN_ITEMS_PER_PAGE]]
    
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
    await get_callback_message(callback).edit_text(entity_config['list_title'], reply_markup=keyboard)
    await state.update_data(editing_entity=entity_config['name'], current_page=page)
    await state.set_state(entity_config['list_state'])
    await callback.answer()

def build_edit_menu(
    entity_id: int,
    entity_config: EntityConfig,
    entity_data: EntityRow,
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
            keyboard.append(list(btn))
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_entity")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    
    fallback_text = (
        f"<b>✏️ Редактирование: {escape_html(entity_config['name_ru'])} · ID {entity_id}</b>\n"
        + "\n".join(fallback_lines)
    )
    rich_html = (
        f"<b>✏️ Редактирование: {escape_html(entity_config['name_ru'])} · ID {entity_id}</b>"
        "<table><tbody><tr><th>Поле</th><th>Значение</th></tr>"
        + "".join(rich_rows) + "</tbody></table>"
    )
    return fallback_text, rich_html, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_edit_menu(callback: types.CallbackQuery, state: FSMContext, entity_id: int, entity_config: EntityConfig, entity_data: EntityRow) -> None:
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

def register_generic_handlers(router: Router, get_entity_configs_func: Callable[[], dict[str, EntityConfig]]) -> None:
    """
    Регистрирует универсальные обработчики на роутере.
    get_entity_configs_func должна возвращать словарь ENTITY_CONFIGS.
    """

    async def update_entity_field(config: EntityConfig, entity_id: int, field: str, value: str | int) -> None:
        database_field = config.get('field_aliases', {}).get(field, field)
        await config['update_func'](entity_id, **{database_field: value})

    @router.callback_query(GenericEditStates.select_field, F.data.startswith("edit_field_"))
    async def generic_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
        field = get_callback_data(callback).split("_")[2]
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
            await get_callback_message(callback).edit_text(
                f"Выберите значение для поля <b>{field}</b>:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
            )
            await state.update_data(edit_field=field)
            await state.set_state(GenericEditStates.select_option)
        else:
            if field == 'note':
                await get_callback_message(callback).edit_text(
                    OPTIONAL_NOTE_PROMPT,
                    reply_markup=build_optional_note_keyboard(),
                )
            else:
                await get_callback_message(callback).edit_text(
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
    async def generic_skip_optional_note(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        if data.get('edit_field') != 'note':
            await callback.answer("Эта кнопка доступна только для примечания.", show_alert=True)
            return

        entity_type = data.get('editing_entity')
        entity_id = data.get('entity_id')
        configs = get_entity_configs_func()
        config = configs.get(entity_type) if isinstance(entity_type, str) else None
        if not config or not entity_id:
            await get_callback_message(callback).edit_text(
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
            await get_callback_message(callback).edit_text("❌ Сущность не найдена.")
            await state.clear()
            await callback.answer()
            return

        await show_edit_menu(callback, state, entity_id, config, entity_data)

    @router.callback_query(GenericEditStates.select_option, F.data == "back_to_edit_menu")
    async def back_to_edit_menu_from_options(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        entity_type = data.get('editing_entity')
        entity_id = data.get('entity_id')
        if not entity_type or not entity_id:
            await get_callback_message(callback).edit_text("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            await state.clear()
            await callback.answer()
            return
        configs = get_entity_configs_func()
        config = configs[entity_type]
        entity_data = await config['get_by_id_func'](entity_id)
        if not entity_data:
            await get_callback_message(callback).edit_text("❌ Сущность не найдена. Возврат в список.")
            await render_entity_list(callback, state, config, 1)
            return
        await show_edit_menu(callback, state, entity_id, config, entity_data)
    
    @router.callback_query(GenericEditStates.select_option, F.data.startswith("select_opt_"))
    async def generic_select_option(callback: types.CallbackQuery, state: FSMContext) -> None:
        parts = get_callback_data(callback).split("_")
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
            await get_callback_message(callback).edit_text(
                f"✅ Поле <b>{escape_html(field)}</b> обновлено на <code>{escape_html(value)}</code>.",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.exception("Не удалось обновить поле %s", field)
            await get_callback_message(callback).edit_text(f"❌ Ошибка: {e}")
            await callback.answer()
            return
        
        entity_data = await config['get_by_id_func'](entity_id)
        if not entity_data:
            await get_callback_message(callback).answer("❌ Сущность не найдена.")
            await render_entity_list(callback, state, config, 1)
            return
        
        await show_edit_menu(callback, state, entity_id, config, entity_data)

    @router.message(GenericEditStates.new_value, F.text, ~F.text.startswith('/'))
    async def generic_update_field(message: types.Message, state: FSMContext) -> None:
        data = await state.get_data()
        
        if 'edit_field' not in data:
            await message.answer("❌ Ошибка состояния. Возврат в админку.")
            await state.clear()
            await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            return
        
        entity_type = data['editing_entity']
        entity_id = data['entity_id']
        field = data['edit_field']
        new_value: str | int = get_message_text(message).strip()
    
        configs = get_entity_configs_func()
        config = configs[entity_type]

        if field == 'note':
            new_value = normalize_optional_note(str(new_value))
    
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
    
        if field == 'emoji' and (not isinstance(new_value, str) or not is_valid_emoji(new_value)):
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
        try:
            await message.delete()
        except TelegramAPIError:
            logger.debug("Admin input message could not be deleted", exc_info=True)

        await state.update_data(entity_id=entity_id, editing_entity=config['name'])
        await state.set_state(GenericEditStates.select_field)

    @router.callback_query(GenericEditStates.select_field, F.data == "delete_entity")
    async def generic_delete_confirm(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        entity_type = data['editing_entity']
        entity_id = data['entity_id']
        configs = get_entity_configs_func()
        config = configs[entity_type]
        entity = await config['get_by_id_func'](entity_id)
        if not entity:
            await get_callback_message(callback).edit_text("❌ Сущность не найдена.")
            await callback.answer()
            return
        confirmation_callback = await prepare_delete_confirmation(
            callback, state, entity_type, entity_id, 'confirm_delete_yes_',
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, удалить", callback_data=confirmation_callback)],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_list")]
        ])
        await get_callback_message(callback).edit_text(
            f"⚠️ Удалить {escape_html(config['name_ru'])} "
            f"<b>{escape_html(entity['name'])}</b> (ID {entity_id})?\n"
            "Это действие необратимо.",
            parse_mode="HTML", reply_markup=keyboard
        )
        await state.set_state(GenericEditStates.confirm_delete)
        await callback.answer()

    @router.callback_query(F.data.startswith("confirm_delete_yes"))
    async def generic_delete_execute(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        entity_type = str(data.get('editing_entity') or '')
        entity_id = data.get('entity_id')
        if not await consume_delete_confirmation(
            callback, state, str(entity_type), entity_id, 'confirm_delete_yes_',
            GenericEditStates.confirm_delete,
        ) or not isinstance(entity_id, int):
            return
        configs = get_entity_configs_func()
        config = configs[entity_type]
        try:
            await config['delete_func'](entity_id)
            await get_callback_message(callback).edit_text("✅ Успешно удалено.")
        except Exception as e:
            await get_callback_message(callback).edit_text(f"❌ Ошибка: {e}")
        back_to_list_func = config.get('back_to_list_func')
        if back_to_list_func:
            await back_to_list_func(callback, state, data)
        else:
            await render_entity_list(callback, state, config, 1)

    @router.callback_query(
        StateFilter(GenericEditStates.select_field, GenericEditStates.confirm_delete),
        F.data == "back_to_list"
    )
    async def generic_back_to_list(callback: types.CallbackQuery, state: FSMContext) -> None:
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
            await get_callback_message(callback).edit_text("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
        await callback.answer()
