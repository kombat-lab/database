import logging
from collections.abc import Callable, Sequence

from admin_contracts import EntityConfig, EntityRow
from admin_commands import admin_transition
from telegram_helpers import get_bound_bot, get_callback_data, get_callback_message, get_message_text
import secrets

from aiogram import F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ui.rich import CardView, present_rich_card
from admin_sessions import present_admin_rich, present_admin_text, validate_admin_input
from recipe_domain import MAX_NAME_LENGTH, MAX_RESOURCE_NAME_LENGTH, MAX_NOTE_LENGTH, MAX_SQLITE_ID
from utils import RICH_TABLE_OPEN, escape_html, is_valid_emoji

logger = logging.getLogger(__name__)

ADMIN_ITEMS_PER_PAGE = 10
OPTIONAL_NOTE_SKIP_CALLBACK = "optional_note_skip"
OPTIONAL_NOTE_PROMPT = "Введите примечание или нажмите «Без примечания»:"


def build_optional_note_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⏭ Без примечания",
                    callback_data=OPTIONAL_NOTE_SKIP_CALLBACK,
                )
            ]
        ],
        force_reply=True,
    )


def normalize_optional_note(value: str) -> str:
    value = value.strip()
    return "" if value == "-" else value


async def edit_admin_rich(
    callback: types.CallbackQuery,
    html: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    fallback_html: str | None = None,
) -> types.Message:
    """Use the shared delivery policy for bounded rich and plain admin screens."""
    message = get_callback_message(callback)
    return await present_rich_card(
        bot=get_bound_bot(callback),
        chat_id=message.chat.id,
        current_message=message,
        card=CardView(rich_html=html, fallback_html=fallback_html or html),
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
        [InlineKeyboardButton(text="📝 Продолжить черновик", callback_data="gear_drafts")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")],
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
    callback: types.CallbackQuery,
    state: FSMContext,
    kind: str,
    entity_id: int,
    prefix: str,
    context: int | None = None,
) -> str:
    """Bind a one-use confirmation to the exact object and Telegram message."""
    token = secrets.token_urlsafe(9)
    await state.update_data(
        admin_delete_confirmation={
            "token": token,
            "kind": kind,
            "entity_id": entity_id,
            "context": context,
            "chat_id": get_callback_message(callback).chat.id,
            "message_id": get_callback_message(callback).message_id,
        }
    )
    return f"{prefix}{token}"


async def consume_delete_confirmation(
    callback: types.CallbackQuery,
    state: FSMContext,
    kind: str,
    entity_id: int | None,
    prefix: str,
    expected_state: State,
    context: int | None = None,
) -> bool:
    data = await state.get_data()
    raw_confirmation = data.get("admin_delete_confirmation")
    confirmation = raw_confirmation if isinstance(raw_confirmation, dict) else {}
    message = callback.message
    valid = (
        message is not None
        and await state.get_state() == expected_state.state
        and confirmation.get("kind") == kind
        and confirmation.get("entity_id") == entity_id
        and confirmation.get("context") == context
        and confirmation.get("chat_id") == message.chat.id
        and confirmation.get("message_id") == message.message_id
        and callback.data == f"{prefix}{confirmation.get('token', '')}"
        and bool(confirmation.get("token"))
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


def build_paginated_keyboard(
    items: Sequence[EntityRow],
    page: int,
    has_next: bool,
    item_callback_prefix: str,
    extra_buttons: list[list[InlineKeyboardButton]] | None = None,
) -> InlineKeyboardMarkup:
    keyboard = []
    for item in items:
        text = f"{item.get('emoji', '')} {item['name']}" + (f" (ID {item['id']})" if "id" in item else "")
        if "rarity" in item:
            text += f" [{item['rarity']}]"
        if "slot" in item:
            text += f" ({item['slot']})"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"{item_callback_prefix}_{item['id']}")])

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"page_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"page_{page + 1}"))
    if nav:
        keyboard.append(nav)

    if extra_buttons:
        for btn_row in extra_buttons:
            keyboard.append(btn_row)

    keyboard.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def render_entity_list(
    callback: types.CallbackQuery, state: FSMContext, entity_config: EntityConfig, page: int = 1
) -> None:
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    page_items = await entity_config["get_page_func"](offset, ADMIN_ITEMS_PER_PAGE + 1)
    has_next = len(page_items) > ADMIN_ITEMS_PER_PAGE
    items = [dict(item) for item in page_items[:ADMIN_ITEMS_PER_PAGE]]

    display_mapping = entity_config.get("display_mapping", {})
    for item in items:
        for field, mapping in display_mapping.items():
            if field in item:
                item[field] = mapping.get(str(item[field]), str(item[field]))

    extra = []
    if entity_config.get("add_button"):
        extra.append(
            [InlineKeyboardButton(text=entity_config["add_button_text"], callback_data=entity_config["add_callback"])]
        )

    keyboard = build_paginated_keyboard(
        items, page, has_next, entity_config["item_callback_prefix"], extra_buttons=extra
    )
    await get_callback_message(callback).edit_text(entity_config["list_title"], reply_markup=keyboard)
    await state.update_data(editing_entity=entity_config["name"], current_page=page)
    await state.set_state(entity_config["list_state"])
    await callback.answer()


def build_edit_menu(
    entity_id: int,
    entity_config: EntityConfig,
    entity_data: EntityRow,
) -> tuple[str, str, InlineKeyboardMarkup]:
    fields = entity_config["edit_fields"]
    display_mapping = entity_config.get("display_mapping", {})
    keyboard = []
    rich_rows = []
    fallback_lines = []
    for field_name, field_label in fields:
        current_value = entity_data.get(field_name, "?")
        formatter = entity_config.get("field_formatters", {}).get(field_name)
        if formatter:
            current_value = formatter(current_value)
        elif field_name in display_mapping:
            current_value = display_mapping[field_name].get(str(current_value), str(current_value))
        rich_rows.append(f"<tr><td>{escape_html(field_label)}</td><td>{escape_html(current_value)}</td></tr>")
        fallback_lines.append(f"{escape_html(field_label)}: {escape_html(current_value)}")
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"{field_label}: {current_value}"[:128], callback_data=f"edit_field_{field_name}"
                )
            ]
        )
    if entity_config.get("extra_edit_buttons"):
        for btn in entity_config["extra_edit_buttons"](entity_id):
            keyboard.append(list(btn))
    keyboard.append([InlineKeyboardButton(text="🗑 Удалить", callback_data="delete_entity")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="back_to_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])

    fallback_text = f"<b>✏️ Редактирование: {escape_html(entity_config['name_ru'])} · ID {entity_id}</b>\n" + "\n".join(
        fallback_lines
    )
    rich_html = (
        f"<b>✏️ Редактирование: {escape_html(entity_config['name_ru'])} · ID {entity_id}</b>"
        f"{RICH_TABLE_OPEN}<tbody><tr><th>Поле</th><th>Значение</th></tr>" + "".join(rich_rows) + "</tbody></table>"
    )
    return fallback_text, rich_html, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_edit_menu(
    callback: types.CallbackQuery | types.Message,
    state: FSMContext,
    entity_id: int,
    entity_config: EntityConfig,
    entity_data: EntityRow,
) -> None:
    fallback_text, rich_html, reply_markup = build_edit_menu(entity_id, entity_config, entity_data)
    await state.update_data(entity_id=entity_id, editing_entity=entity_config["name"])
    await state.set_state(GenericEditStates.select_field)
    await present_admin_rich(
        callback,
        state,
        rich_html,
        fallback_text,
        reply_markup,
        context={"entity_id": entity_id, "editing_entity": entity_config["name"]},
    )
    if isinstance(callback, types.CallbackQuery):
        await callback.answer()


def register_generic_handlers(router: Router, get_entity_configs_func: Callable[[], dict[str, EntityConfig]]) -> None:
    """Register field editors; the outer admin middleware validates screen identity."""

    async def update_entity_field(
        state: FSMContext, config: EntityConfig, entity_id: int, field: str, value: str | int
    ) -> None:
        async with admin_transition(state):
            await config["update_func"](entity_id, field, value)
            await state.set_state(GenericEditStates.select_field)

    async def return_to_item(
        event: types.CallbackQuery | types.Message, state: FSMContext, config: EntityConfig, entity_id: int
    ) -> None:
        # Persist the completed transition independently of Telegram delivery.
        await state.set_state(GenericEditStates.select_field)
        entity = await config["get_by_id_func"](entity_id)
        if entity is None:
            await state.clear()
            if isinstance(event, types.CallbackQuery):
                await event.answer("Предмет уже удалён.", show_alert=True)
            else:
                await event.answer("Предмет уже удалён. Откройте админку заново.")
            return
        await show_edit_menu(event, state, entity_id, config, entity)

    @router.callback_query(GenericEditStates.select_field, F.data.startswith("edit_field_"))
    async def generic_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
        field = get_callback_data(callback).removeprefix("edit_field_")
        data = await state.get_data()
        config = get_entity_configs_func()[data["editing_entity"]]
        fields = dict(config["edit_fields"])
        if field not in fields:
            await callback.answer("Неизвестное поле.", show_alert=True)
            return
        entity = await config["get_by_id_func"](data["entity_id"])
        if entity is None:
            await state.clear()
            await callback.answer("Предмет уже удалён.", show_alert=True)
            return
        title = f"<b>{escape_html(entity['name'])}</b> · {escape_html(fields[field])}"
        context = {"entity_id": data["entity_id"], "editing_entity": config["name"], "edit_field": field}
        select_options = config.get("select_options", {}).get(field)
        keyboard: list[list[InlineKeyboardButton]] = []
        if select_options:
            display = config.get("display_mapping", {}).get(field, {})
            for option in select_options:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=display.get(option, option), callback_data=f"select_opt_{field}_{option}"
                        )
                    ]
                )
            prompt = "Выберите новое значение:"
            next_state = GenericEditStates.select_option
        else:
            prompt = OPTIONAL_NOTE_PROMPT if field == "note" else "Введите новое значение:"
            if field == "note":
                keyboard = build_optional_note_keyboard().inline_keyboard
            next_state = GenericEditStates.new_value
        keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_edit_menu")])

        async def commit_prompt() -> None:
            await state.update_data(edit_field=field)
            await state.set_state(next_state)

        await present_admin_text(
            callback,
            state,
            f"{title}\n{prompt}",
            InlineKeyboardMarkup(inline_keyboard=keyboard, force_reply=not bool(select_options)),
            parse_mode="HTML",
            context=context,
            commit=commit_prompt,
        )
        await callback.answer()

    @router.callback_query(GenericEditStates.new_value, F.data == OPTIONAL_NOTE_SKIP_CALLBACK)
    async def generic_skip_optional_note(
        callback: types.CallbackQuery, state: FSMContext, admin_screen_validated: bool = False
    ) -> None:
        # The same callback also exists in creation forms. Untagged legacy
        # creation buttons must never mutate an unrelated generic edit.
        if not admin_screen_validated:
            await callback.answer("Экран устарел. Откройте предмет заново.", show_alert=True)
            return
        data = await state.get_data()
        if data.get("edit_field") != "note":
            await callback.answer("Эта кнопка доступна только для примечания.", show_alert=True)
            return
        config = get_entity_configs_func()[data["editing_entity"]]
        try:
            await update_entity_field(state, config, data["entity_id"], "note", "")
        except Exception:
            logger.exception("Не удалось очистить примечание")
            await callback.answer("Не удалось сохранить примечание. Повторите попытку.", show_alert=True)
            return
        await return_to_item(callback, state, config, data["entity_id"])

    @router.callback_query(
        StateFilter(GenericEditStates.select_option, GenericEditStates.new_value), F.data == "back_to_edit_menu"
    )
    async def back_to_edit_menu_from_options(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        config = get_entity_configs_func().get(data.get("editing_entity", ""))
        entity_id = data.get("entity_id")
        if config is None or not isinstance(entity_id, int):
            await admin_cancel_edit(callback, state)
            return
        await return_to_item(callback, state, config, entity_id)

    @router.callback_query(GenericEditStates.select_option, F.data.startswith("select_opt_"))
    async def generic_select_option(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        config = get_entity_configs_func()[data["editing_entity"]]
        field = data["edit_field"]
        prefix = f"select_opt_{field}_"
        payload = get_callback_data(callback)
        value = payload.removeprefix(prefix)
        allowed = config.get("select_options", {}).get(field, ())
        if not payload.startswith(prefix) or value not in allowed:
            await callback.answer("Недопустимое значение.", show_alert=True)
            return
        try:
            await update_entity_field(state, config, data["entity_id"], field, value)
        except ValueError as error:
            await callback.answer(str(error)[:180], show_alert=True)
            return
        except Exception:
            logger.exception("Не удалось обновить поле %s", field)
            await callback.answer("Не удалось сохранить. Повторите попытку.", show_alert=True)
            return
        await return_to_item(callback, state, config, data["entity_id"])

    @router.message(GenericEditStates.new_value, F.text, ~F.text.startswith("/"))
    async def generic_update_field(message: types.Message, state: FSMContext) -> None:
        if not await validate_admin_input(message, state):
            return
        data = await state.get_data()
        config = get_entity_configs_func().get(data.get("editing_entity", ""))
        field = data.get("edit_field")
        entity_id = data.get("entity_id")
        if (
            config is None
            or not isinstance(field, str)
            or field not in dict(config["edit_fields"])
            or not isinstance(entity_id, int)
        ):
            await state.clear()
            await message.answer("Экран устарел. Откройте предмет заново.")
            return
        value: str | int = get_message_text(message).strip()
        if field == "note":
            value = normalize_optional_note(str(value))
        if field in config.get("integer_fields", ()):
            minimum = config.get("integer_minimums", {}).get(field, 0)
            try:
                value = int(value)
                if not minimum <= value <= MAX_SQLITE_ID:
                    raise ValueError
            except (ValueError, TypeError):
                await message.answer(f"Введите целое число от {minimum} до {MAX_SQLITE_ID}.")
                return
        if field == "emoji" and (not isinstance(value, str) or not is_valid_emoji(value)):
            await message.answer("Введите один корректный эмодзи.")
            return
        if field == "name" and not value:
            await message.answer("Название не может быть пустым.")
            return
        name_maximum = MAX_RESOURCE_NAME_LENGTH if config["name"] == "resource" else MAX_NAME_LENGTH
        maximum = {"name": name_maximum, "note": MAX_NOTE_LENGTH}.get(field)
        if field.startswith("bonus"):
            maximum = 500
        if maximum is not None and isinstance(value, str) and len(value) > maximum:
            await message.answer(f"Допустимо не больше {maximum} символов.")
            return
        try:
            await update_entity_field(state, config, entity_id, field, value)
        except ValueError as error:
            await message.answer(str(error))
            return
        except Exception:
            logger.exception("Не удалось обновить поле %s", field)
            await message.answer("Не удалось сохранить. Повторите попытку.")
            return
        await return_to_item(message, state, config, entity_id)
        try:
            await message.delete()
        except TelegramAPIError:
            logger.debug("Admin input message could not be deleted", exc_info=True)

    @router.callback_query(GenericEditStates.select_field, F.data == "delete_entity")
    async def generic_delete_confirm(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        entity_type = data["editing_entity"]
        entity_id = data["entity_id"]
        config = get_entity_configs_func()[entity_type]
        entity = await config["get_by_id_func"](entity_id)
        if entity is None:
            await state.clear()
            await callback.answer("Предмет уже удалён.", show_alert=True)
            return
        impact_func = config.get("delete_impact_func")
        impact = await impact_func(entity_id) if impact_func is not None else ""
        if impact:
            await present_admin_text(
                callback,
                state,
                f"<b>{escape_html(entity['name'])}</b>\n\nУдаление недоступно: {escape_html(impact)}\nСначала измените связанные записи.",
                InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="🔙 К списку", callback_data="back_to_list")]]
                ),
                parse_mode="HTML",
                context={"entity_id": entity_id, "editing_entity": entity_type},
            )
            await callback.answer()
            return
        confirmation_callback = await prepare_delete_confirmation(
            callback, state, entity_type, entity_id, "confirm_delete_yes_"
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, удалить", callback_data=confirmation_callback)],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_list")],
            ]
        )
        await state.set_state(GenericEditStates.confirm_delete)
        sent = await present_admin_text(
            callback,
            state,
            f"⚠️ Удалить {escape_html(config['name_ru'])} <b>{escape_html(entity['name'])}</b> (ID {entity_id})?\nЭто действие необратимо.",
            keyboard,
            parse_mode="HTML",
            context={"entity_id": entity_id, "editing_entity": entity_type},
        )
        confirmation = (await state.get_data())["admin_delete_confirmation"]
        confirmation["message_id"] = sent.message_id
        await state.update_data(admin_delete_confirmation=confirmation)
        await callback.answer()

    @router.callback_query(F.data.startswith("confirm_delete_yes"))
    async def generic_delete_execute(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        entity_type = str(data.get("editing_entity") or "")
        entity_id = data.get("entity_id")
        if not await consume_delete_confirmation(
            callback, state, entity_type, entity_id, "confirm_delete_yes_", GenericEditStates.confirm_delete
        ) or not isinstance(entity_id, int):
            return
        config = get_entity_configs_func()[entity_type]
        try:
            async with admin_transition(state):
                await config["delete_func"](entity_id)
                await state.set_state(config["list_state"])
        except ValueError as error:
            await callback.answer(str(error)[:180], show_alert=True)
            await return_to_item(callback, state, config, entity_id)
            return
        except Exception:
            logger.exception("Не удалось удалить %s %s", entity_type, entity_id)
            await callback.answer("Не удалось удалить. Повторите попытку.", show_alert=True)
            await return_to_item(callback, state, config, entity_id)
            return
        back_to_list = config.get("back_to_list_func")
        if back_to_list:
            await back_to_list(callback, state, data)
        else:
            await render_entity_list(callback, state, config, 1)

    @router.callback_query(
        StateFilter(GenericEditStates.select_field, GenericEditStates.confirm_delete), F.data == "back_to_list"
    )
    async def generic_back_to_list(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        config = get_entity_configs_func().get(data.get("editing_entity", ""))
        if config is None:
            await admin_cancel_edit(callback, state)
            return
        back_to_list = config.get("back_to_list_func")
        if back_to_list:
            await back_to_list(callback, state, data)
        else:
            await render_entity_list(callback, state, config, data.get("current_page", 1))
