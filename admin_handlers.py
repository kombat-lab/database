import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from admin_contracts import EntityConfig, StateData
from telegram_helpers import get_callback_data, get_callback_message, get_message_text
import os

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
from database import db
from admin_mobs import mob_router
from admin_recipes import recipe_router
from admin_gear import gear_router, start_gear_editor
from admin_sessions import (
    AdminScreenMiddleware, present_admin_text, register_protected_callbacks, validate_admin_input,
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
from stats_handlers import stats_router
from utils import is_valid_emoji

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()
register_protected_callbacks((
    'res_type_', 'card_slot_', OPTIONAL_NOTE_SKIP_CALLBACK, 'catalog_create_back',
))

class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(
        self, handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject, data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user and is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, types.CallbackQuery):
            await event.answer("⛔ Нет доступа.", show_alert=True)
        elif isinstance(event, types.Message):
            await event.answer("⛔ Нет доступа.")
        return None


admin_access = AdminOnlyMiddleware()
admin_router.message.outer_middleware(admin_access)
admin_router.callback_query.outer_middleware(admin_access)
admin_router.callback_query.outer_middleware(AdminScreenMiddleware())

# Подключаем роутер статистики
admin_router.include_router(stats_router)
admin_router.include_router(gear_router)
admin_router.include_router(mob_router)
admin_router.include_router(recipe_router)
stats_router.message.outer_middleware(admin_access)
stats_router.callback_query.outer_middleware(admin_access)

@admin_router.message(Command("kombat"))
async def admin_panel(message: types.Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🔧 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML",
                         reply_markup=get_admin_main_keyboard())


admin_router.callback_query(F.data == "admin_close")(admin_close)
admin_router.callback_query(F.data == "admin_cancel_edit")(admin_cancel_edit)

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

ENTITY_CONFIGS['resource'] = {
    'name': 'resource',
    'name_ru': 'ресурс',
    'get_page_func': db.get_resources_page,
    'get_by_id_func': db.get_resource_by_id,
    'update_func': db.update_resource,
    'field_aliases': {'type': 'resource_type'},
    'delete_func': db.delete_resource,
    'item_callback_prefix': 'resource_edit',
    'list_state': ResourceListStates.list_page,
    'list_title': "📦 Ресурсы:\nВыберите ресурс для редактирования или добавьте новый:",
    'add_button': True,
    'add_button_text': "➕ Добавить ресурс",
    'add_callback': "resource_add_start",
    'edit_fields': [
        ('name', '✏️ Название'),
        ('emoji', '😀 Эмодзи'),
        ('type', '🏷 Тип'),
        ('note', '📝 Примечание')
    ],
    'integer_fields': [],
    'select_options': {
        'type': RESOURCE_TYPE_KEYS
    },
    'display_mapping': {
        'type': {
            'craft': '📦 Крафтовый',
            'consumable': '✨ Расходуемый',
            'scroll_recipe': '📜 Рецепт экипировки',
            'currency': '💰 Валюта',
            'alchemy': '🧪 Алхимия'
        }
    }
}

ENTITY_CONFIGS['gear'] = {
    'name': 'gear',
    'name_ru': 'снаряжение',
    'get_page_func': db.get_all_gear,
    'get_by_id_func': db.get_gear_by_id,
    'update_func': db.update_gear,
    'delete_func': db.delete_gear,
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
        ('level', '📈 Уровень'),
        ('classes', '🧙 Классы'),
        ('note', '📝 Примечание'),
        ('emoji', '😀 Эмодзи')
    ],
    'integer_fields': ['level'],
    'integer_minimums': {'level': 1},
    'select_options': {
        'rarity': RARITY_KEYS,
        'slot': GEAR_SLOTS,
    },
    'display_mapping': {
        'rarity': RARITY_LABELS,
        'slot': GEAR_SLOT_LABELS
    },
    'field_formatters': {'classes': format_gear_classes}
}

ENTITY_CONFIGS['card'] = {
    'name': 'card',
    'name_ru': 'карту',
    'get_page_func': db.get_cards_page,
    'get_by_id_func': db.get_card_by_id,
    'update_func': db.update_card,
    'delete_func': db.delete_card,
    'item_callback_prefix': 'card_edit',
    'list_state': CardListStates.list_page,
    'list_title': "🃏 Управление картами:\nВыберите карту для редактирования или добавьте новую:",
    'add_button': True,
    'add_button_text': "➕ Добавить карту",
    'add_callback': "card_add_start",
    'edit_fields': [
        ('name', '✏️ Название'),
        ('emoji', '😀 Эмодзи'),
        ('slot', '🔧 Слот'),
        ('bonus1', '✨ Бонус 1'),
        ('bonus2', '✨ Бонус 2'),
        ('bonus3', '✨ Бонус 3'),
        ('bonus4', '✨ Бонус 4'),
        ('note', '📝 Примечание')
    ],
    'integer_fields': [],
    'select_options': {
        'slot': GEAR_SLOTS
    },
    'display_mapping': {
        'slot': GEAR_SLOT_LABELS
    }
}

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ РЕСУРСОВ

# ============================================================

@admin_router.callback_query(F.data == "admin_manage_resources")
@admin_router.callback_query(F.data == "admin_manage_cards")
async def manage_catalog_entity(callback: types.CallbackQuery, state: FSMContext) -> None:
    entity_type = get_callback_data(callback).removeprefix("admin_manage_")
    entity_type = "card" if entity_type == "cards" else "resource"
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], 1)

@admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("resource_edit_"))
@admin_router.callback_query(GearListStates.list_page, F.data.startswith("gear_edit_"))
@admin_router.callback_query(CardListStates.list_page, F.data.startswith("card_edit_"))
async def edit_catalog_entity(callback: types.CallbackQuery, state: FSMContext) -> None:
    entity_type, raw_id = get_callback_data(callback).split("_edit_", 1)
    entity_id = int(raw_id)
    if entity_type == 'gear':
        await start_gear_editor(callback, state, gear_id=entity_id)
        return
    config = ENTITY_CONFIGS[entity_type]
    entity = await config['get_by_id_func'](entity_id)
    if not entity:
        await get_callback_message(callback).edit_text("Объект не найден.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, entity_id, config, entity)

@admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("page_"))
@admin_router.callback_query(CardListStates.list_page, F.data.startswith("page_"))
async def catalog_page_nav(callback: types.CallbackQuery, state: FSMContext) -> None:
    entity_type = (
        "resource"
        if await state.get_state() == ResourceListStates.list_page.state
        else "card"
    )
    page = int(get_callback_data(callback).split("_")[1])
    await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], page)

@admin_router.callback_query(ResourceListStates.list_page, F.data == "resource_add_start")
async def resource_add_name(callback: types.CallbackQuery, state: FSMContext) -> None:
    await begin_catalog_creation(callback, state, 'resource')

class ResourceAddStates(StatesGroup):
    name = State()
    emoji = State()
    type = State()
    note = State()

CATALOG_CREATION_STEPS = {
    'resource': ('name', 'emoji', 'type', 'note'),
    'card': ('name', 'emoji', 'slot', 'bonus1', 'bonus2', 'bonus3', 'bonus4', 'note'),
}
MAX_CARD_BONUS_LENGTH = 500


async def show_catalog_creation_step(
    target: types.Message | types.CallbackQuery, state: FSMContext, kind: str, step: str,
) -> None:
    data = await state.get_data()
    session = data.get('catalog_creation_session')
    if not isinstance(session, str) or kind not in CATALOG_CREATION_STEPS or step not in CATALOG_CREATION_STEPS[kind]:
        raise ValueError('Создание объекта устарело. Откройте админку заново.')
    rows: list[list[InlineKeyboardButton]] = []
    prompts = {
        'name': 'Введите название нового ресурса:' if kind == 'resource' else 'Введите название карты:',
        'emoji': 'Введите эмодзи:', 'type': 'Выберите тип ресурса:', 'slot': 'Выберите слот:',
        'bonus1': 'Введите первый бонус (например: «Удача +2») или «-»:',
        'bonus2': 'Введите второй бонус или «-»:',
        'bonus3': 'Введите третий бонус или «-»:',
        'bonus4': 'Введите четвёртый бонус или «-»:',
        'note': OPTIONAL_NOTE_PROMPT,
    }
    if step == 'type':
        labels = ENTITY_CONFIGS['resource']['display_mapping']['type']
        rows = [[InlineKeyboardButton(text=labels[value], callback_data=f'res_type_{value}')]
                for value in RESOURCE_TYPE_KEYS if value != 'scroll_recipe']
    elif step == 'slot':
        rows = [[InlineKeyboardButton(text=GEAR_SLOT_LABELS[value], callback_data=f'card_slot_{value}')]
                for value in GEAR_SLOTS]
    elif step == 'note':
        rows = list(build_optional_note_keyboard().inline_keyboard)
    if CATALOG_CREATION_STEPS[kind].index(step) > 0:
        rows.append([InlineKeyboardButton(text='🔙 Назад', callback_data='catalog_create_back')])
    rows.append([InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_edit')])
    # Keep the previous step usable when delivery fails. Bind and advance only
    # after Telegram has accepted the next prompt.
    await present_admin_text(
        target, state, prompts[step], InlineKeyboardMarkup(inline_keyboard=rows),
        context={'catalog_creation_session': session, 'catalog_creation_kind': kind,
                 'catalog_creation_step': step},
    )
    await state.update_data(catalog_creation_kind=kind, catalog_creation_step=step)
    steps = {
        'resource': {'name': ResourceAddStates.name, 'emoji': ResourceAddStates.emoji,
                     'type': ResourceAddStates.type, 'note': ResourceAddStates.note},
        'card': {'name': CardAddStates.name, 'emoji': CardAddStates.emoji, 'slot': CardAddStates.slot,
                 'bonus1': CardAddStates.bonus1, 'bonus2': CardAddStates.bonus2,
                 'bonus3': CardAddStates.bonus3, 'bonus4': CardAddStates.bonus4, 'note': CardAddStates.note},
    }
    await state.set_state(steps[kind][step])


async def begin_catalog_creation(callback: types.CallbackQuery, state: FSMContext, kind: str) -> None:
    await state.clear()
    await state.update_data(catalog_creation_session=secrets.token_hex(8), catalog_creation_kind=kind)
    await show_catalog_creation_step(callback, state, kind, 'name')
    await callback.answer()


@admin_router.callback_query(F.data == 'catalog_create_back')
async def catalog_creation_back(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    kind, step = data.get('catalog_creation_kind'), data.get('catalog_creation_step')
    if not isinstance(kind, str) or kind not in CATALOG_CREATION_STEPS or step not in CATALOG_CREATION_STEPS[kind]:
        await callback.answer('Создание объекта устарело.', show_alert=True)
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
        await message.answer(f'Бонус слишком длинный. Максимум {MAX_CARD_BONUS_LENGTH} символов.')
        return
    await state.update_data({f'card_{field}': '' if value == '-' else value})
    await show_catalog_creation_step(message, state, 'card', next_step)

@admin_router.message(ResourceAddStates.name, F.text, ~F.text.startswith('/'))
async def resource_add_emoji(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    name = get_message_text(message).strip()
    if not name or len(name) > MAX_RESOURCE_NAME_LENGTH:
        await message.answer(f'Название должно содержать от 1 до {MAX_RESOURCE_NAME_LENGTH} символов.')
        return
    await state.update_data(res_name=name)
    await show_catalog_creation_step(message, state, 'resource', 'emoji')

@admin_router.message(ResourceAddStates.emoji, F.text, ~F.text.startswith('/'))
async def resource_add_emoji_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    emoji = get_message_text(message).strip()
    if not is_valid_emoji(emoji):
        await message.answer('Введите Unicode эмодзи.')
        return
    await state.update_data(res_emoji=emoji)
    await show_catalog_creation_step(message, state, 'resource', 'type')

@admin_router.callback_query(ResourceAddStates.type, F.data.startswith("res_type_"))
async def resource_add_note(callback: types.CallbackQuery, state: FSMContext) -> None:
    resource_type = get_callback_data(callback).removeprefix('res_type_')
    if resource_type not in RESOURCE_TYPE_KEYS or resource_type == 'scroll_recipe':
        await callback.answer('Неизвестный тип ресурса.', show_alert=True)
        return
    await state.update_data(res_type=resource_type)
    await show_catalog_creation_step(callback, state, 'resource', 'note')
    await callback.answer()


async def save_new_resource(target: types.Message, state: FSMContext, note: str) -> None:
    if len(note) > MAX_NOTE_LENGTH:
        await target.answer(f'Примечание слишком длинное. Максимум {MAX_NOTE_LENGTH} символов.')
        return
    data = await state.get_data()
    try:
        await db.add_resource(data['res_name'], data['res_emoji'], data['res_type'], note)
    except Exception as error:
        logger.exception("Не удалось добавить ресурс")
        await target.answer(f"❌ Ошибка: {error}")
        return
    await state.clear()
    await target.answer("✅ Ресурс добавлен.\n🔧 Админ-панель", reply_markup=get_admin_main_keyboard())


@admin_router.message(ResourceAddStates.note, F.text, ~F.text.startswith('/'))
async def resource_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    await save_new_resource(message, state, normalize_optional_note(get_message_text(message)))

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ СНАРЯЖЕНИЯ
# ============================================================

def build_admin_gear_slots_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"admin_gear_slot_{i}")] for i, slot in enumerate(GEAR_SLOTS)]
    rows.append([InlineKeyboardButton(text="➕ Добавить снаряжение", callback_data="gear_add_start")])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def render_admin_gear_slot(callback: types.CallbackQuery, state: FSMContext, slot_index: int, page: int = 1) -> None:
    if not 0 <= slot_index < len(GEAR_SLOTS) or page < 1:
        await callback.answer("Некорректная страница снаряжения.", show_alert=True)
        return
    slot = GEAR_SLOTS[slot_index]
    offset = (page - 1) * ADMIN_ITEMS_PER_PAGE
    items = await db.get_gear_by_slot(
        slot,
        offset,
        ADMIN_ITEMS_PER_PAGE + 1,
    )
    has_next = len(items) > ADMIN_ITEMS_PER_PAGE
    items = items[:ADMIN_ITEMS_PER_PAGE]
    rows = [[InlineKeyboardButton(text=f"{RARITY_EMOJIS.get(x.get('rarity') or 'common','⚪')} {x.get('emoji','')} {x['name']} · ур. {x.get('level',1)}", callback_data=f"gear_edit_{x['id']}")] for x in items]
    nav=[]
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_gear_page_{slot_index}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"admin_gear_page_{slot_index}_{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад к слотам", callback_data="admin_manage_gear")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    await get_callback_message(callback).edit_text(f"⚔️ Управление снаряжением · {GEAR_SLOT_LABELS[slot]}\nВыберите предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(gear_slot_index=slot_index, current_page=page, editing_entity='gear')
    await state.set_state(GearListStates.list_page)
    await callback.answer()

async def back_to_admin_gear_slot(callback: types.CallbackQuery, state: FSMContext, data: StateData) -> None:
    """Возвращает из карточки снаряжения в ранее открытую категорию/слот."""
    slot_index = data.get("gear_slot_index")
    page = data.get("current_page", 1)

    if slot_index is None:
        await get_callback_message(callback).edit_text(
            "⚔️ Управление снаряжением\nВыберите слот:",
            reply_markup=build_admin_gear_slots_keyboard(),
        )
        await state.set_state(GearListStates.list_page)
        return

    await render_admin_gear_slot(callback, state, int(slot_index), int(page or 1))


ENTITY_CONFIGS['gear']['back_to_list_func'] = back_to_admin_gear_slot


@admin_router.callback_query(F.data == "admin_manage_gear")
async def manage_gear(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await get_callback_message(callback).edit_text("⚔️ Управление снаряжением\nВыберите слот:", reply_markup=build_admin_gear_slots_keyboard())
    await state.set_state(GearListStates.list_page)
    await callback.answer()

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_slot_"))
async def admin_gear_slot(callback: types.CallbackQuery, state: FSMContext) -> None:
    await render_admin_gear_slot(callback, state, int(get_callback_data(callback).rsplit('_',1)[1]), 1)

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_page_"))
async def admin_gear_page(callback: types.CallbackQuery, state: FSMContext) -> None:
    parts=get_callback_data(callback).split('_')
    await render_admin_gear_slot(callback, state, int(parts[3]), int(parts[4]))

@admin_router.callback_query(GearListStates.list_page, F.data == "gear_add_start")
async def gear_add_name(callback: types.CallbackQuery, state: FSMContext) -> None:
    from recipe_domain import GearDraftPayload
    data = await state.get_data()
    payload: GearDraftPayload = {'emoji': '⚔️', 'level': 1, 'classes': '', 'craftable': False}
    slot_index = data.get('gear_slot_index')
    if isinstance(slot_index, int) and 0 <= slot_index < len(GEAR_SLOTS):
        payload['slot'] = GEAR_SLOTS[slot_index]
    await start_gear_editor(callback, state, payload=payload)













# ============================================================
# ОБРАБОТЧИКИ ДЛЯ КАРТ
# ============================================================

@admin_router.callback_query(CardListStates.list_page, F.data == "card_add_start")
async def card_add_name(callback: types.CallbackQuery, state: FSMContext) -> None:
    await begin_catalog_creation(callback, state, 'card')

@admin_router.message(CardAddStates.name, F.text, ~F.text.startswith('/'))
async def card_add_emoji(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    name = get_message_text(message).strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        await message.answer(f'Название должно содержать от 1 до {MAX_NAME_LENGTH} символов.')
        return
    await state.update_data(card_name=name)
    await show_catalog_creation_step(message, state, 'card', 'emoji')

@admin_router.message(CardAddStates.emoji, F.text, ~F.text.startswith('/'))
async def card_add_emoji_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    emoji = get_message_text(message).strip()
    if not is_valid_emoji(emoji):
        await message.answer('Введите Unicode эмодзи.')
        return
    await state.update_data(card_emoji=emoji)
    await show_catalog_creation_step(message, state, 'card', 'slot')

@admin_router.callback_query(CardAddStates.slot, F.data.startswith("card_slot_"))
async def card_add_bonus1(callback: types.CallbackQuery, state: FSMContext) -> None:
    slot = get_callback_data(callback).removeprefix('card_slot_')
    if slot not in GEAR_SLOTS:
        await callback.answer('Неизвестный слот.', show_alert=True)
        return
    await state.update_data(card_slot=slot)
    await show_catalog_creation_step(callback, state, 'card', 'bonus1')
    await callback.answer()

@admin_router.message(CardAddStates.bonus1, F.text, ~F.text.startswith('/'))
async def card_add_bonus2(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, 'bonus1', 'bonus2')

@admin_router.message(CardAddStates.bonus2, F.text, ~F.text.startswith('/'))
async def card_add_bonus3(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, 'bonus2', 'bonus3')

@admin_router.message(CardAddStates.bonus3, F.text, ~F.text.startswith('/'))
async def card_add_bonus4(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, 'bonus3', 'bonus4')

@admin_router.message(CardAddStates.bonus4, F.text, ~F.text.startswith('/'))
async def card_add_note(message: types.Message, state: FSMContext) -> None:
    await store_card_bonus(message, state, 'bonus4', 'note')


async def save_new_card(target: types.Message, state: FSMContext, note: str) -> None:
    if len(note) > MAX_NOTE_LENGTH:
        await target.answer(f'Примечание слишком длинное. Максимум {MAX_NOTE_LENGTH} символов.')
        return
    data = await state.get_data()
    try:
        await db.add_card(
            name=data['card_name'],
            emoji=data['card_emoji'],
            slot=data['card_slot'],
            bonus1=data.get('card_bonus1', ''),
            bonus2=data.get('card_bonus2', ''),
            bonus3=data.get('card_bonus3', ''),
            bonus4=data.get('card_bonus4', ''),
            note=note,
        )
    except Exception as error:
        logger.exception("Не удалось добавить карту")
        await target.answer(f"❌ Ошибка: {error}")
        return
    await state.clear()
    await target.answer("✅ Карта добавлена.\n🔧 Админ-панель", reply_markup=get_admin_main_keyboard())


@admin_router.message(CardAddStates.note, F.text, ~F.text.startswith('/'))
async def card_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    await save_new_card(message, state, normalize_optional_note(get_message_text(message)))


@admin_router.callback_query(
    StateFilter(ResourceAddStates.note, CardAddStates.note),
    F.data == OPTIONAL_NOTE_SKIP_CALLBACK,
)
async def skip_new_entity_note(callback: types.CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    await callback.answer()
    if current_state == ResourceAddStates.note.state:
        await save_new_resource(get_callback_message(callback), state, "")
    elif current_state == CardAddStates.note.state:
        await save_new_card(get_callback_message(callback), state, "")

# ============================================================

# МНОЖЕСТВЕННЫЙ ВЫБОР КЛАССОВ ДЛЯ СУЩЕСТВУЮЩЕГО СНАРЯЖЕНИЯ
# ============================================================



# ============================================================
# Регистрация универсальных обработчиков (CRUD)
# ============================================================

register_generic_handlers(admin_router, lambda: ENTITY_CONFIGS)


async def resource_delete_impact(resource_id: int) -> str:
    dependencies = await db.get_resource_dependencies(resource_id)
    labels = [(dependencies['ingredient_recipe_ids'], 'Материал в рецептах'), (dependencies['learning_recipe_ids'], 'Свиток изучения рецептов'),
              (dependencies['result_recipe_ids'], 'Результат рецептов')]
    lines = []
    for ids, label in labels:
        if ids:
            lines.append(f"{label}: {', '.join(map(str, ids))}.")
    if lines and dependencies['drop_mob_ids']:
        lines.append(f"Источники дропа: {len(dependencies['drop_mob_ids'])} мобов.")
    return '\n'.join(lines)


ENTITY_CONFIGS['resource']['delete_impact_func'] = resource_delete_impact
