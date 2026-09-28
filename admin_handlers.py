import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from admin_commands import EntityCommands
from admin_forms import CatalogCreation
from storage.types import sql_int, sql_text
from admin_contracts import EntityConfig, StateData
from telegram_helpers import get_callback_data, get_callback_message, get_message_text

from aiogram import BaseMiddleware, F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_utils import (
    ADMIN_ITEMS_PER_PAGE,
    OPTIONAL_NOTE_PROMPT,
    OPTIONAL_NOTE_SKIP_CALLBACK,
    admin_cancel_edit,
    admin_close,
    build_optional_note_keyboard,
    get_admin_main_keyboard,
    normalize_optional_note,
    register_generic_handlers,
    render_entity_list,
    show_edit_menu,
)
from runtime_scope import RuntimeScope, RuntimeScopeMiddleware, database_for
from admin_mobs import create_mob_router
from admin_recipes import create_recipe_router
from admin_gear import create_gear_router, start_gear_editor
from admin_item_sources import register_item_sources_handlers, start_item_sources
from admin_sessions import (
    AdminScreenMiddleware,
    present_admin_text,
    validate_admin_input,
)
from recipe_domain import MAX_NAME_LENGTH, MAX_RESOURCE_NAME_LENGTH, MAX_NOTE_LENGTH
from game_constants import (
    GEAR_SLOT_LABELS,
    GEAR_SLOTS,
    RARITY_EMOJIS,
    RARITY_KEYS,
    RARITY_LABELS,
    RESOURCE_TYPE_KEYS,
    format_gear_classes,
)
from analytics import AnalyticsService
from stats_handlers import create_stats_router
from utils import is_valid_emoji, escape_html
from recipe_domain import DomainError, DuplicateIdentityError

logger = logging.getLogger(__name__)


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, admin_ids: frozenset[int]) -> None:
        self.admin_ids = admin_ids

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if isinstance(user, types.User) and user.id in self.admin_ids:
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery):
            await event.answer("⛔ Нет доступа.", show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer("⛔ Нет доступа.")
        return None


async def admin_panel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        "🔧 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML", reply_markup=get_admin_main_keyboard()
    )


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


ENTITY_CONFIGS: dict[str, EntityConfig] = {}
RESOURCE_COMMANDS = EntityCommands(lambda: database_for(), "resource")
GEAR_COMMANDS = EntityCommands(lambda: database_for(), "gear")
CARD_COMMANDS = EntityCommands(lambda: database_for(), "card")

ENTITY_CONFIGS["resource"] = {
    "name": "resource",
    "name_ru": "ресурс",
    "get_page_func": RESOURCE_COMMANDS.page,
    "get_by_id_func": RESOURCE_COMMANDS.get,
    "update_func": RESOURCE_COMMANDS.update,
    "delete_func": RESOURCE_COMMANDS.delete,
    "item_callback_prefix": "resource_edit",
    "list_state": ResourceListStates.list_page,
    "list_title": "📦 Ресурсы:\nВыберите ресурс для редактирования или добавьте новый:",
    "add_button": True,
    "add_button_text": "➕ Добавить ресурс",
    "add_callback": "resource_add_start",
    "edit_fields": [("name", "✏️ Название"), ("emoji", "😀 Эмодзи"), ("type", "🏷 Тип"), ("note", "📝 Примечание")],
    "integer_fields": [],
    "select_options": {"type": RESOURCE_TYPE_KEYS},
    "display_mapping": {
        "type": {
            "craft": "📦 Крафтовый",
            "consumable": "✨ Расходуемый",
            "scroll_recipe": "📜 Рецепт экипировки",
            "currency": "💰 Валюта",
            "alchemy": "🧪 Алхимия",
        }
    },
}

ENTITY_CONFIGS["gear"] = {
    "name": "gear",
    "name_ru": "снаряжение",
    "get_page_func": GEAR_COMMANDS.page,
    "get_by_id_func": GEAR_COMMANDS.get,
    "update_func": GEAR_COMMANDS.update,
    "delete_func": GEAR_COMMANDS.delete,
    "item_callback_prefix": "gear_edit",
    "list_state": GearListStates.list_page,
    "list_title": "⚔️ Управление снаряжением:\nВыберите предмет для редактирования или добавьте новый:",
    "add_button": True,
    "add_button_text": "➕ Добавить снаряжение",
    "add_callback": "gear_add_start",
    "edit_fields": [
        ("name", "✏️ Название"),
        ("rarity", "⭐ Редкость"),
        ("slot", "🔧 Слот"),
        ("level", "📈 Уровень"),
        ("classes", "🧙 Классы"),
        ("note", "📝 Примечание"),
        ("emoji", "😀 Эмодзи"),
    ],
    "integer_fields": ["level"],
    "integer_minimums": {"level": 1},
    "select_options": {
        "rarity": RARITY_KEYS,
        "slot": GEAR_SLOTS,
    },
    "display_mapping": {"rarity": RARITY_LABELS, "slot": GEAR_SLOT_LABELS},
    "field_formatters": {"classes": format_gear_classes},
}

ENTITY_CONFIGS["card"] = {
    "name": "card",
    "name_ru": "карту",
    "get_page_func": CARD_COMMANDS.page,
    "get_by_id_func": CARD_COMMANDS.get,
    "update_func": CARD_COMMANDS.update,
    "delete_func": CARD_COMMANDS.delete,
    "item_callback_prefix": "card_edit",
    "list_state": CardListStates.list_page,
    "list_title": "🃏 Управление картами:\nВыберите карту для редактирования или добавьте новую:",
    "add_button": True,
    "add_button_text": "➕ Добавить карту",
    "add_callback": "card_add_start",
    "edit_fields": [
        ("name", "✏️ Название"),
        ("emoji", "😀 Эмодзи"),
        ("slot", "🔧 Слот"),
        ("bonus1", "✨ Бонус 1"),
        ("bonus2", "✨ Бонус 2"),
        ("bonus3", "✨ Бонус 3"),
        ("bonus4", "✨ Бонус 4"),
        ("note", "📝 Примечание"),
    ],
    "integer_fields": [],
    "select_options": {"slot": GEAR_SLOTS},
    "display_mapping": {"slot": GEAR_SLOT_LABELS},
}

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ РЕСУРСОВ

# ============================================================


async def manage_catalog_entity(callback: types.CallbackQuery, state: FSMContext) -> None:
    entity_type = get_callback_data(callback).removeprefix("admin_manage_")
    entity_type = "card" if entity_type == "cards" else "resource"
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], 1)


async def edit_catalog_entity(callback: types.CallbackQuery, state: FSMContext) -> None:
    entity_type, raw_id = get_callback_data(callback).split("_edit_", 1)
    entity_id = int(raw_id)
    if entity_type == "gear":
        await start_gear_editor(callback, state, gear_id=entity_id)
        return
    config = ENTITY_CONFIGS[entity_type]
    entity = await config["get_by_id_func"](entity_id)
    if not entity:
        await get_callback_message(callback).edit_text("Объект не найден.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, entity_id, config, entity)


async def catalog_page_nav(callback: types.CallbackQuery, state: FSMContext) -> None:
    entity_type = "resource" if await state.get_state() == ResourceListStates.list_page.state else "card"
    page = int(get_callback_data(callback).split("_")[1])
    await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], page)


async def resource_add_name(callback: types.CallbackQuery, state: FSMContext) -> None:
    await begin_catalog_creation(callback, state, "resource")


class ResourceAddStates(StatesGroup):
    name = State()
    emoji = State()
    type = State()
    note = State()


CATALOG_CREATION_STEPS = {
    "resource": ("name", "emoji", "type", "note"),
    "card": ("name", "emoji", "slot", "bonus1", "bonus2", "bonus3", "bonus4", "note"),
}
MAX_CARD_BONUS_LENGTH = 500


async def show_catalog_creation_step(
    target: types.Message | types.CallbackQuery,
    state: FSMContext,
    kind: str,
    step: str,
) -> None:
    data = await state.get_data()
    session = data.get("catalog_creation_session")
    if not isinstance(session, str) or kind not in CATALOG_CREATION_STEPS or step not in CATALOG_CREATION_STEPS[kind]:
        raise ValueError("Создание объекта устарело. Откройте админку заново.")
    rows: list[list[InlineKeyboardButton]] = []
    prompts = {
        "name": "Введите название нового ресурса:" if kind == "resource" else "Введите название карты:",
        "emoji": "Введите эмодзи:",
        "type": "Выберите тип ресурса:",
        "slot": "Выберите слот:",
        "bonus1": "Введите первый бонус (например: «Удача +2») или «-»:",
        "bonus2": "Введите второй бонус или «-»:",
        "bonus3": "Введите третий бонус или «-»:",
        "bonus4": "Введите четвёртый бонус или «-»:",
        "note": OPTIONAL_NOTE_PROMPT,
    }
    if step == "type":
        labels = ENTITY_CONFIGS["resource"]["display_mapping"]["type"]
        rows = [
            [InlineKeyboardButton(text=labels[value], callback_data=f"res_type_{value}")]
            for value in RESOURCE_TYPE_KEYS
            if value != "scroll_recipe"
        ]
    elif step == "slot":
        rows = [
            [InlineKeyboardButton(text=GEAR_SLOT_LABELS[value], callback_data=f"card_slot_{value}")]
            for value in GEAR_SLOTS
        ]
    elif step == "note":
        rows = list(build_optional_note_keyboard().inline_keyboard)
    if CATALOG_CREATION_STEPS[kind].index(step) > 0:
        rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="catalog_create_back")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin_cancel_edit")])
    steps = {
        "resource": {
            "name": ResourceAddStates.name,
            "emoji": ResourceAddStates.emoji,
            "type": ResourceAddStates.type,
            "note": ResourceAddStates.note,
        },
        "card": {
            "name": CardAddStates.name,
            "emoji": CardAddStates.emoji,
            "slot": CardAddStates.slot,
            "bonus1": CardAddStates.bonus1,
            "bonus2": CardAddStates.bonus2,
            "bonus3": CardAddStates.bonus3,
            "bonus4": CardAddStates.bonus4,
            "note": CardAddStates.note,
        },
    }

    async def commit_step() -> None:
        await state.update_data(catalog_creation_kind=kind, catalog_creation_step=step)
        await state.set_state(steps[kind][step])

    # Bind the new screen, payload and active input state only after delivery.
    await present_admin_text(
        target,
        state,
        prompts[step],
        InlineKeyboardMarkup(inline_keyboard=rows),
        context={"catalog_creation_session": session, "catalog_creation_kind": kind, "catalog_creation_step": step},
        commit=commit_step,
    )


async def begin_catalog_creation(callback: types.CallbackQuery, state: FSMContext, kind: str) -> None:
    await state.clear()
    await state.update_data(catalog_creation_session=secrets.token_hex(8), catalog_creation_kind=kind)
    await show_catalog_creation_step(callback, state, kind, "name")
    await callback.answer()


async def catalog_creation_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    kind, step = data.get("catalog_creation_kind"), data.get("catalog_creation_step")
    if not isinstance(kind, str) or kind not in CATALOG_CREATION_STEPS or step not in CATALOG_CREATION_STEPS[kind]:
        await callback.answer("Создание объекта устарело.", show_alert=True)
        return
    index = CATALOG_CREATION_STEPS[kind].index(step)
    if index > 0:
        await show_catalog_creation_step(callback, state, kind, CATALOG_CREATION_STEPS[kind][index - 1])
    await callback.answer()


async def store_card_bonus(message: types.Message, state: FSMContext, field: str, next_step: str) -> None:
    if not await validate_admin_input(message, state):
        return
    value = get_message_text(message).strip()
    if len(value) > MAX_CARD_BONUS_LENGTH:
        await message.answer(f"Бонус слишком длинный. Максимум {MAX_CARD_BONUS_LENGTH} символов.")
        return
    await state.update_data({f"card_{field}": "" if value == "-" else value})
    await show_catalog_creation_step(message, state, "card", next_step)


async def resource_add_emoji(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    name = get_message_text(message).strip()
    if not name or len(name) > MAX_RESOURCE_NAME_LENGTH:
        await message.answer(f"Название должно содержать от 1 до {MAX_RESOURCE_NAME_LENGTH} символов.")
        return
    await state.update_data(res_name=name, catalog_duplicate_confirmed=False)
    await show_catalog_creation_step(message, state, "resource", "emoji")


async def resource_add_emoji_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    emoji = get_message_text(message).strip()
    if not is_valid_emoji(emoji):
        await message.answer("Введите Unicode эмодзи.")
        return
    await state.update_data(res_emoji=emoji)
    await show_catalog_creation_step(message, state, "resource", "type")


async def resource_add_note(callback: types.CallbackQuery, state: FSMContext) -> None:
    resource_type = get_callback_data(callback).removeprefix("res_type_")
    if resource_type not in RESOURCE_TYPE_KEYS or resource_type == "scroll_recipe":
        await callback.answer("Неизвестный тип ресурса.", show_alert=True)
        return
    await state.update_data(res_type=resource_type, catalog_duplicate_confirmed=False)
    await show_catalog_creation_step(callback, state, "resource", "note")
    await callback.answer()


async def save_new_resource(target: types.Message | types.CallbackQuery, state: FSMContext, note: str) -> None:
    await prepare_catalog_sources(target, state, "resource", note)


async def prepare_catalog_sources(
    target: types.Message | types.CallbackQuery, state: FSMContext, kind: str, note: str
) -> None:
    if len(note) > MAX_NOTE_LENGTH:
        text = f"Примечание слишком длинное. Максимум {MAX_NOTE_LENGTH} символов."
        if isinstance(target, types.CallbackQuery):
            await target.answer(text, show_alert=True)
        else:
            await target.answer(text)
        return
    if kind not in ("resource", "card"):
        raise DomainError("Неизвестный вид предмета.")
    await state.update_data(catalog_creation_note=note)
    await start_item_sources(target, state, "resource" if kind == "resource" else "card")


async def return_to_catalog_note(target: types.Message | types.CallbackQuery, state: FSMContext) -> None:
    kind = (await state.get_data()).get("catalog_creation_kind")
    if kind not in ("resource", "card"):
        raise DomainError("Создание предмета устарело.")
    await show_catalog_creation_step(target, state, str(kind), "note")
    if isinstance(target, types.CallbackQuery):
        await target.answer()


class CatalogDuplicateStates(StatesGroup):
    choose = State()


async def show_catalog_duplicates(
    target: types.Message | types.CallbackQuery, state: FSMContext, page: int = 0
) -> None:
    data = await state.get_data()
    creation = CatalogCreation.decode(data)
    matches = (
        await database_for().get_resource_name_matches(creation.name, creation.category)
        if creation.kind == "resource"
        else await database_for().get_card_name_matches(creation.name, creation.category)
    )
    page = min(max(page, 0), max(0, (len(matches) - 1) // 8))
    rows = [
        [
            InlineKeyboardButton(
                text=f"Открыть #{item['id']} · {item['name']}"[:100],
                callback_data=f"catalog_duplicate_open_{item['id']}",
            )
        ]
        for item in matches[page * 8 : (page + 1) * 8]
    ]
    if page:
        rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data=f"catalog_duplicate_page_{page - 1}")])
    if (page + 1) * 8 < len(matches):
        rows.append([InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"catalog_duplicate_page_{page + 1}")])
    rows += [
        [InlineKeyboardButton(text="Создать отдельный вариант", callback_data="catalog_duplicate_confirm")],
        [InlineKeyboardButton(text="🔙 К источникам", callback_data="catalog_duplicate_back")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin_cancel_edit")],
    ]

    async def commit_duplicates() -> None:
        await state.update_data(catalog_duplicate_ids=[item["id"] for item in matches])
        await state.set_state(CatalogDuplicateStates.choose)

    await present_admin_text(
        target,
        state,
        f"Уже есть предметы с названием <b>{escape_html(creation.name)}</b> в этой категории. "
        "Откройте существующий предмет или явно создайте отдельный вариант. "
        "Открытие существующего предмета отменит ввод нового.",
        InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
        context={"catalog_creation_session": creation.session, "catalog_creation_kind": creation.kind},
        commit=commit_duplicates,
    )


async def catalog_duplicate_action(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    creation = CatalogCreation.decode(data)
    action = get_callback_data(callback).removeprefix("catalog_duplicate_")
    if action == "confirm":
        from admin_item_sources import decode_ids

        await state.update_data(catalog_duplicate_confirmed=True)
        await complete_catalog_creation(callback, state, decode_ids(data.get("catalog_duplicate_mob_ids")))
    elif action == "back":
        await start_item_sources(callback, state, creation.kind)
    elif action.startswith("page_"):
        await show_catalog_duplicates(callback, state, int(action.removeprefix("page_")))
    elif action.startswith("open_"):
        entity_id = int(action.removeprefix("open_"))
        if entity_id not in data.get("catalog_duplicate_ids", []):
            raise DomainError("Предмет не относится к совпадениям этой формы.")
        entity = await ENTITY_CONFIGS[creation.kind]["get_by_id_func"](entity_id)
        if entity is None:
            await callback.answer("Предмет уже удалён.", show_alert=True)
            return
        await state.clear()
        await show_edit_menu(callback, state, entity_id, ENTITY_CONFIGS[creation.kind], entity)
    await callback.answer()


async def complete_catalog_creation(
    target: types.Message | types.CallbackQuery, state: FSMContext, mob_ids: list[int]
) -> None:
    data = await state.get_data()
    creation = CatalogCreation.decode(data)
    kind = creation.kind
    allow_duplicate = data.get("catalog_duplicate_confirmed") is True
    try:
        if kind == "resource":
            entity_id = await database_for().create_resource_with_sources(
                creation.name,
                creation.emoji,
                creation.category,
                creation.note,
                mob_ids=mob_ids,
                allow_duplicate=allow_duplicate,
                operation_id=creation.session,
            )
        else:
            entity_id = await database_for().create_card_with_sources(
                name=creation.name,
                emoji=creation.emoji,
                slot=creation.category,
                bonus1=creation.bonuses[0],
                bonus2=creation.bonuses[1],
                bonus3=creation.bonuses[2],
                bonus4=creation.bonuses[3],
                note=creation.note,
                mob_ids=mob_ids,
                allow_duplicate=allow_duplicate,
                operation_id=creation.session,
            )
    except DuplicateIdentityError:
        await state.update_data(catalog_duplicate_mob_ids=mob_ids)
        await show_catalog_duplicates(target, state)
        return
    except DomainError:
        raise
    except Exception:
        logger.exception("Не удалось добавить предмет с источниками")
        if isinstance(target, types.CallbackQuery):
            await target.answer("Не удалось сохранить. Выбор сохранён, повторите попытку.", show_alert=True)
        else:
            await target.answer("Не удалось сохранить. Выбор сохранён, повторите попытку.")
        return
    # Commit succeeds before any Telegram delivery. Retrying an old button must
    # never insert a second item, even when showing the resulting card fails.
    await state.clear()
    entity = (
        await database_for().get_resource_by_id(entity_id)
        if kind == "resource"
        else await database_for().get_card_by_id(entity_id)
    )
    if entity is None:
        raise DomainError("Предмет сохранён, но уже удалён другим администратором.")
    await show_edit_menu(target, state, entity_id, ENTITY_CONFIGS[kind], entity)


async def resource_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    await save_new_resource(message, state, normalize_optional_note(get_message_text(message)))


# ============================================================
# ОБРАБОТЧИКИ ДЛЯ СНАРЯЖЕНИЯ
# ============================================================


def build_admin_gear_slots_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"admin_gear_slot_{i}")]
        for i, slot in enumerate(GEAR_SLOTS)
    ]
    rows.append([InlineKeyboardButton(text="➕ Добавить снаряжение", callback_data="gear_add_start")])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def render_admin_gear_slot(
    callback: types.CallbackQuery, state: FSMContext, slot_index: int, page: int = 1
) -> None:
    if not 0 <= slot_index < len(GEAR_SLOTS) or page < 1:
        await callback.answer("Некорректная страница снаряжения.", show_alert=True)
        return
    slot = GEAR_SLOTS[slot_index]
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items = await database_for().get_gear_by_slot(
        slot,
        offset,
        ADMIN_ITEMS_PER_PAGE + 1,
    )
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    rows = [
        [
            InlineKeyboardButton(
                text=f"{RARITY_EMOJIS.get(sql_text(item['rarity']), '⚪')} {sql_text(item['emoji'])} "
                f"{sql_text(item['name'])} · ур. {sql_int(item['level'])}",
                callback_data=f"gear_edit_{sql_int(item['id'])}",
            )
        ]
        for item in items
    ]
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_gear_page_{slot_index}_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"admin_gear_page_{slot_index}_{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад к слотам", callback_data="admin_manage_gear")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    await get_callback_message(callback).edit_text(
        f"⚔️ Управление снаряжением · {GEAR_SLOT_LABELS[slot]}\nВыберите предмет:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await state.update_data(gear_slot_index=slot_index, current_page=page, editing_entity="gear")
    await state.set_state(GearListStates.list_page)
    await callback.answer()


async def back_to_admin_gear_slot(callback: types.CallbackQuery, state: FSMContext, data: StateData) -> None:
    """Возвращает из карточки снаряжения в ранее открытую категорию/слот."""
    slot_index = data.get("gear_slot_index")
    page = data.get("current_page", 1)

    if not isinstance(slot_index, int):
        await get_callback_message(callback).edit_text(
            "⚔️ Управление снаряжением\nВыберите слот:",
            reply_markup=build_admin_gear_slots_keyboard(),
        )
        await state.set_state(GearListStates.list_page)
        return

    await render_admin_gear_slot(callback, state, slot_index, page if isinstance(page, int) and page > 0 else 1)


ENTITY_CONFIGS["gear"]["back_to_list_func"] = back_to_admin_gear_slot


async def manage_gear(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await get_callback_message(callback).edit_text(
        "⚔️ Управление снаряжением\nВыберите слот:", reply_markup=build_admin_gear_slots_keyboard()
    )
    await state.set_state(GearListStates.list_page)
    await callback.answer()


async def admin_gear_slot(callback: types.CallbackQuery, state: FSMContext) -> None:
    await render_admin_gear_slot(callback, state, int(get_callback_data(callback).rsplit("_", 1)[1]), 1)


async def admin_gear_page(callback: types.CallbackQuery, state: FSMContext) -> None:
    parts = get_callback_data(callback).split("_")
    await render_admin_gear_slot(callback, state, int(parts[3]), int(parts[4]))


async def gear_add_name(callback: types.CallbackQuery, state: FSMContext) -> None:
    from recipe_domain import GearDraftPayload

    data = await state.get_data()
    payload: GearDraftPayload = {"emoji": "⚔️", "level": 1, "classes": "", "craftable": False}
    slot_index = data.get("gear_slot_index")
    if isinstance(slot_index, int) and 0 <= slot_index < len(GEAR_SLOTS):
        payload["slot"] = GEAR_SLOTS[slot_index]
    await start_gear_editor(callback, state, payload=payload)


# ============================================================
# ОБРАБОТЧИКИ ДЛЯ КАРТ
# ============================================================


async def card_add_name(callback: types.CallbackQuery, state: FSMContext) -> None:
    await begin_catalog_creation(callback, state, "card")


async def card_add_emoji(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    name = get_message_text(message).strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        await message.answer(f"Название должно содержать от 1 до {MAX_NAME_LENGTH} символов.")
        return
    await state.update_data(card_name=name, catalog_duplicate_confirmed=False)
    await show_catalog_creation_step(message, state, "card", "emoji")


async def card_add_emoji_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    emoji = get_message_text(message).strip()
    if not is_valid_emoji(emoji):
        await message.answer("Введите Unicode эмодзи.")
        return
    await state.update_data(card_emoji=emoji)
    await show_catalog_creation_step(message, state, "card", "slot")


async def card_add_bonus1(callback: types.CallbackQuery, state: FSMContext) -> None:
    slot = get_callback_data(callback).removeprefix("card_slot_")
    if slot not in GEAR_SLOTS:
        await callback.answer("Неизвестный слот.", show_alert=True)
        return
    await state.update_data(card_slot=slot, catalog_duplicate_confirmed=False)
    await show_catalog_creation_step(callback, state, "card", "bonus1")
    await callback.answer()


async def card_add_bonus2(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, "bonus1", "bonus2")


async def card_add_bonus3(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, "bonus2", "bonus3")


async def card_add_bonus4(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, "bonus3", "bonus4")


async def card_add_note(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, "bonus4", "note")


async def save_new_card(target: types.Message | types.CallbackQuery, state: FSMContext, note: str) -> None:
    await prepare_catalog_sources(target, state, "card", note)


async def card_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    await save_new_card(message, state, normalize_optional_note(get_message_text(message)))


async def skip_new_entity_note(callback: types.CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    await callback.answer()
    if current_state == ResourceAddStates.note.state:
        await save_new_resource(callback, state, "")
    elif current_state == CardAddStates.note.state:
        await save_new_card(callback, state, "")


# ============================================================

# МНОЖЕСТВЕННЫЙ ВЫБОР КЛАССОВ ДЛЯ СУЩЕСТВУЮЩЕГО СНАРЯЖЕНИЯ
# ============================================================


# ============================================================
# Регистрация универсальных обработчиков (CRUD)
# ============================================================


def item_source_buttons(entity_id: int) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(text="👾 С кого падает", callback_data="item_sources_open")]]


ENTITY_CONFIGS["resource"]["extra_edit_buttons"] = item_source_buttons
ENTITY_CONFIGS["card"]["extra_edit_buttons"] = item_source_buttons


async def resource_delete_impact(resource_id: int) -> str:
    dependencies = await database_for().get_resource_dependencies(resource_id)
    labels = [
        (dependencies["ingredient_recipe_ids"], "Материал в рецептах"),
        (dependencies["learning_recipe_ids"], "Свиток изучения рецептов"),
        (dependencies["result_recipe_ids"], "Результат рецептов"),
    ]
    lines = []
    for ids, label in labels:
        if ids:
            lines.append(f"{label}: {', '.join(map(str, ids))}.")
    if dependencies["drop_mob_ids"]:
        lines.append(f"Источники дропа: {len(dependencies['drop_mob_ids'])} мобов.")
    return "\n".join(lines)


ENTITY_CONFIGS["resource"]["delete_impact_func"] = resource_delete_impact


def create_admin_router(scope: RuntimeScope) -> Router:
    admin_router = Router()
    admin_router.message.outer_middleware(RuntimeScopeMiddleware(scope))
    admin_router.callback_query.outer_middleware(RuntimeScopeMiddleware(scope))
    admin_access = AdminOnlyMiddleware(scope.admin_ids)
    admin_router.message.outer_middleware(admin_access)
    admin_router.callback_query.outer_middleware(admin_access)
    admin_router.callback_query.outer_middleware(AdminScreenMiddleware())
    admin_router.include_router(create_stats_router(AnalyticsService(scope.database)))
    admin_router.include_router(create_gear_router())
    admin_router.include_router(create_mob_router())
    admin_router.include_router(create_recipe_router())
    admin_router.message(Command("kombat"))(admin_panel)
    admin_router.callback_query(F.data == "admin_close")(admin_close)
    admin_router.callback_query(F.data == "admin_cancel_edit")(admin_cancel_edit)
    admin_router.callback_query(F.data == "admin_manage_cards")(manage_catalog_entity)
    admin_router.callback_query(F.data == "admin_manage_resources")(manage_catalog_entity)
    admin_router.callback_query(CardListStates.list_page, F.data.startswith("card_edit_"))(edit_catalog_entity)
    admin_router.callback_query(GearListStates.list_page, F.data.startswith("gear_edit_"))(edit_catalog_entity)
    admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("resource_edit_"))(edit_catalog_entity)
    admin_router.callback_query(CardListStates.list_page, F.data.startswith("page_"))(catalog_page_nav)
    admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("page_"))(catalog_page_nav)
    admin_router.callback_query(ResourceListStates.list_page, F.data == "resource_add_start")(resource_add_name)
    admin_router.callback_query(F.data == "catalog_create_back")(catalog_creation_back)
    admin_router.message(ResourceAddStates.name, F.text, ~F.text.startswith("/"))(resource_add_emoji)
    admin_router.message(ResourceAddStates.emoji, F.text, ~F.text.startswith("/"))(resource_add_emoji_input)
    admin_router.callback_query(ResourceAddStates.type, F.data.startswith("res_type_"))(resource_add_note)
    admin_router.callback_query(CatalogDuplicateStates.choose, F.data.startswith("catalog_duplicate_"))(
        catalog_duplicate_action
    )
    admin_router.message(ResourceAddStates.note, F.text, ~F.text.startswith("/"))(resource_save)
    admin_router.callback_query(F.data == "admin_manage_gear")(manage_gear)
    admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_slot_"))(admin_gear_slot)
    admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_page_"))(admin_gear_page)
    admin_router.callback_query(GearListStates.list_page, F.data == "gear_add_start")(gear_add_name)
    admin_router.callback_query(CardListStates.list_page, F.data == "card_add_start")(card_add_name)
    admin_router.message(CardAddStates.name, F.text, ~F.text.startswith("/"))(card_add_emoji)
    admin_router.message(CardAddStates.emoji, F.text, ~F.text.startswith("/"))(card_add_emoji_input)
    admin_router.callback_query(CardAddStates.slot, F.data.startswith("card_slot_"))(card_add_bonus1)
    admin_router.message(CardAddStates.bonus1, F.text, ~F.text.startswith("/"))(card_add_bonus2)
    admin_router.message(CardAddStates.bonus2, F.text, ~F.text.startswith("/"))(card_add_bonus3)
    admin_router.message(CardAddStates.bonus3, F.text, ~F.text.startswith("/"))(card_add_bonus4)
    admin_router.message(CardAddStates.bonus4, F.text, ~F.text.startswith("/"))(card_add_note)
    admin_router.message(CardAddStates.note, F.text, ~F.text.startswith("/"))(card_save)
    admin_router.callback_query(
        StateFilter(ResourceAddStates.note, CardAddStates.note),
        F.data == OPTIONAL_NOTE_SKIP_CALLBACK,
    )(skip_new_entity_note)
    register_generic_handlers(admin_router, lambda: ENTITY_CONFIGS)
    register_item_sources_handlers(
        admin_router, lambda: ENTITY_CONFIGS, complete_catalog_creation, return_to_catalog_note
    )
    return admin_router
