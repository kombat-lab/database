import logging
import os

from aiogram import BaseMiddleware, F, Router, types
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_utils import (
    ADMIN_ITEMS_PER_PAGE,
    OPTIONAL_NOTE_PROMPT,
    OPTIONAL_NOTE_SKIP_CALLBACK,
    GenericEditStates,
    admin_cancel_edit,
    admin_close,
    build_optional_note_keyboard,
    edit_admin_rich,
    get_admin_main_keyboard,
    normalize_optional_note,
    register_generic_handlers,
    render_entity_list,
    show_edit_menu,
)
from database import db
from game_constants import (
    GEAR_CLASS_ORDER as GEAR_CLASSES,
    GEAR_SLOT_LABELS,
    GEAR_SLOTS,
    RARITY_EMOJIS,
    RARITY_KEYS,
    RARITY_LABELS,
    RESOURCE_TYPE_KEYS,
    format_gear_classes,
    parse_gear_classes,
)
from stats_handlers import stats_router
from utils import RICH_TABLE_OPEN, clean_username, escape_html, is_valid_emoji

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

RESOURCE_TYPES = [
    ('craft', '📦 Крафтовые'), ('consumable', '✨ Расходуемые'),
    ('scroll_recipe', '📜 Рецепты экипировки'), ('currency', '💰 Валюта'),
    ('alchemy', '🧪 Алхимия')
]


class AdminOnlyMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
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

# Подключаем роутер статистики
admin_router.include_router(stats_router)
stats_router.message.outer_middleware(admin_access)
stats_router.callback_query.outer_middleware(admin_access)

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

ENTITY_CONFIGS = {}

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
async def manage_catalog_entity(callback: types.CallbackQuery, state: FSMContext):
    entity_type = callback.data.removeprefix("admin_manage_")
    entity_type = "card" if entity_type == "cards" else "resource"
    await state.clear()
    await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], 1)

@admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("resource_edit_"))
@admin_router.callback_query(GearListStates.list_page, F.data.startswith("gear_edit_"))
@admin_router.callback_query(CardListStates.list_page, F.data.startswith("card_edit_"))
async def edit_catalog_entity(callback: types.CallbackQuery, state: FSMContext):
    entity_type, raw_id = callback.data.split("_edit_", 1)
    entity_id = int(raw_id)
    config = ENTITY_CONFIGS[entity_type]
    entity = await config['get_by_id_func'](entity_id)
    if not entity:
        await callback.message.edit_text("Объект не найден.")
        await callback.answer()
        return
    await show_edit_menu(callback, state, entity_id, config, entity)

@admin_router.callback_query(ResourceListStates.list_page, F.data.startswith("page_"))
@admin_router.callback_query(CardListStates.list_page, F.data.startswith("page_"))
async def catalog_page_nav(callback: types.CallbackQuery, state: FSMContext):
    entity_type = (
        "resource"
        if await state.get_state() == ResourceListStates.list_page.state
        else "card"
    )
    page = int(callback.data.split("_")[1])
    await render_entity_list(callback, state, ENTITY_CONFIGS[entity_type], page)

@admin_router.callback_query(ResourceListStates.list_page, F.data == "resource_add_start")
async def resource_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название нового ресурса:")
    await state.set_state(ResourceAddStates.name)

class ResourceAddStates(StatesGroup):
    name = State()
    emoji = State()
    type = State()
    note = State()

@admin_router.message(ResourceAddStates.name, F.text)
async def resource_add_emoji(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(res_name=name)
    await message.answer("Введите эмодзи:")
    await state.set_state(ResourceAddStates.emoji)

@admin_router.message(ResourceAddStates.emoji, F.text)
async def resource_add_emoji_input(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(res_emoji=emoji)
    type_labels = ENTITY_CONFIGS['resource']['display_mapping']['type']
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=type_labels[resource_type], callback_data=f"res_type_{resource_type}")]
        for resource_type in RESOURCE_TYPE_KEYS
    ])
    await message.answer("Выберите тип ресурса:", reply_markup=keyboard)
    await state.set_state(ResourceAddStates.type)

@admin_router.callback_query(ResourceAddStates.type, F.data.startswith("res_type_"))
async def resource_add_note(callback: types.CallbackQuery, state: FSMContext):
    resource_type = callback.data.removeprefix("res_type_")
    if resource_type not in RESOURCE_TYPE_KEYS:
        await callback.answer("Неизвестный тип ресурса", show_alert=True)
        return
    await state.update_data(res_type=resource_type)
    await callback.message.edit_text(
        OPTIONAL_NOTE_PROMPT,
        reply_markup=build_optional_note_keyboard(),
    )
    await state.set_state(ResourceAddStates.note)


async def save_new_resource(target: types.Message, state: FSMContext, note: str):
    data = await state.get_data()
    try:
        await db.add_resource(data['res_name'], data['res_emoji'], data['res_type'], note)
        await target.answer("✅ Ресурс добавлен.")
    except Exception as error:
        logger.exception("Не удалось добавить ресурс")
        await target.answer(f"❌ Ошибка: {error}")
    await state.clear()
    await target.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())


@admin_router.message(ResourceAddStates.note, F.text)
async def resource_save(message: types.Message, state: FSMContext):
    await save_new_resource(message, state, normalize_optional_note(message.text))

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ СНАРЯЖЕНИЯ
# ============================================================

def build_admin_gear_slots_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"admin_gear_slot_{i}")] for i, slot in enumerate(GEAR_SLOTS)]
    rows.append([InlineKeyboardButton(text="➕ Добавить снаряжение", callback_data="gear_add_start")])
    rows.append([InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

async def render_admin_gear_slot(callback, state, slot_index: int, page: int = 1):
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
    rows = [[InlineKeyboardButton(text=f"{RARITY_EMOJIS.get(x.get('rarity'),'⚪')} {x.get('emoji','')} {x['name']} · ур. {x.get('level',1)}", callback_data=f"gear_edit_{x['id']}")] for x in items]
    nav=[]
    if page>1: nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"admin_gear_page_{slot_index}_{page-1}"))
    if has_next: nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"admin_gear_page_{slot_index}_{page+1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="🔙 Назад к слотам", callback_data="admin_manage_gear")])
    rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])
    await callback.message.edit_text(f"⚔️ Управление снаряжением · {GEAR_SLOT_LABELS[slot]}\nВыберите предмет:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.update_data(gear_slot_index=slot_index, current_page=page, editing_entity='gear')
    await state.set_state(GearListStates.list_page)
    await callback.answer()

async def back_to_admin_gear_slot(callback, state, data):
    """Возвращает из карточки снаряжения в ранее открытую категорию/слот."""
    slot_index = data.get("gear_slot_index")
    page = data.get("current_page", 1)

    if slot_index is None:
        await callback.message.edit_text(
            "⚔️ Управление снаряжением\nВыберите слот:",
            reply_markup=build_admin_gear_slots_keyboard(),
        )
        await state.set_state(GearListStates.list_page)
        return

    await render_admin_gear_slot(callback, state, int(slot_index), int(page or 1))


ENTITY_CONFIGS['gear']['back_to_list_func'] = back_to_admin_gear_slot


@admin_router.callback_query(F.data == "admin_manage_gear")
async def manage_gear(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚔️ Управление снаряжением\nВыберите слот:", reply_markup=build_admin_gear_slots_keyboard())
    await state.set_state(GearListStates.list_page)
    await callback.answer()

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_slot_"))
async def admin_gear_slot(callback: types.CallbackQuery, state: FSMContext):
    await render_admin_gear_slot(callback, state, int(callback.data.rsplit('_',1)[1]), 1)

@admin_router.callback_query(GearListStates.list_page, F.data.startswith("admin_gear_page_"))
async def admin_gear_page(callback: types.CallbackQuery, state: FSMContext):
    parts=callback.data.split('_')
    await render_admin_gear_slot(callback, state, int(parts[3]), int(parts[4]))

@admin_router.callback_query(GearListStates.list_page, F.data == "gear_add_start")
async def gear_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название снаряжения:")
    await state.set_state(GearAddStates.name)

class GearAddStates(StatesGroup):
    name = State()
    rarity = State()
    slot = State()
    emoji = State()
    level = State()
    classes = State()
    note = State()

class GearClassEditStates(StatesGroup):
    selecting = State()

def build_gear_classes_keyboard(selected):
    selected = set(selected)
    rows = []
    for class_name in GEAR_CLASSES:
        mark = "☑️" if class_name in selected else "⬜"
        rows.append([InlineKeyboardButton(
            text=f"{mark} {class_name}",
            callback_data=f"gear_class_toggle_{GEAR_CLASSES.index(class_name)}"
        )])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="gear_classes_done")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

@admin_router.message(GearAddStates.name, F.text)
async def gear_add_rarity(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(gear_name=name)
    rarity_labels = ENTITY_CONFIGS['gear']['display_mapping']['rarity']
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{rarity_labels[rarity]} ({rarity})", callback_data=f"rarity_{rarity}")]
        for rarity in RARITY_KEYS
    ])
    await message.answer("Выберите редкость:", reply_markup=keyboard)
    await state.set_state(GearAddStates.rarity)

@admin_router.callback_query(GearAddStates.rarity, F.data.startswith("rarity_"))
async def gear_add_slot(callback: types.CallbackQuery, state: FSMContext):
    rarity = callback.data.split("_")[1]
    if rarity not in RARITY_KEYS:
        await callback.answer("Неизвестная редкость", show_alert=True)
        return
    await state.update_data(gear_rarity=rarity)
    keyboard = [
        [InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"slot_{slot}")]
        for slot in GEAR_SLOTS
    ]
    await callback.message.edit_text("Выберите слот:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(GearAddStates.slot)
    await callback.answer()

@admin_router.callback_query(GearAddStates.slot, F.data.startswith("slot_"))
async def gear_add_emoji(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split("_")[1]
    if slot not in GEAR_SLOTS:
        await callback.answer("Неизвестный слот", show_alert=True)
        return
    await state.update_data(gear_slot=slot)
    await callback.message.edit_text("Введите эмодзи:")
    await state.set_state(GearAddStates.emoji)
    await callback.answer()

@admin_router.message(GearAddStates.emoji, F.text)
async def gear_add_level_prompt(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(gear_emoji=emoji)
    await message.answer("Введите минимальный уровень для экипировки (целое число от 1):")
    await state.set_state(GearAddStates.level)

@admin_router.message(GearAddStates.level, F.text)
async def gear_add_classes_prompt(message: types.Message, state: FSMContext):
    try:
        level = int(message.text.strip())
        if level < 1:
            raise ValueError
    except ValueError:
        await message.answer("Введите целое число не меньше 1.")
        return
    await state.update_data(gear_level=level, gear_classes=[])
    await message.answer(
        "Выберите один или несколько классов, затем нажмите «Готово»:",
        reply_markup=build_gear_classes_keyboard([])
    )
    await state.set_state(GearAddStates.classes)

@admin_router.callback_query(GearAddStates.classes, F.data.startswith("gear_class_toggle_"))
@admin_router.callback_query(GearClassEditStates.selecting, F.data.startswith("gear_class_toggle_"))
async def gear_toggle_class(callback: types.CallbackQuery, state: FSMContext):
    index = int(callback.data.rsplit("_", 1)[1])
    if index < 0 or index >= len(GEAR_CLASSES):
        await callback.answer("Неизвестный класс", show_alert=True)
        return
    data = await state.get_data()
    selected = list(data.get('gear_classes', []))
    class_name = GEAR_CLASSES[index]
    if class_name in selected:
        selected.remove(class_name)
    else:
        selected.append(class_name)
    selected.sort(key=GEAR_CLASSES.index)
    await state.update_data(gear_classes=selected)
    await callback.message.edit_reply_markup(reply_markup=build_gear_classes_keyboard(selected))
    await callback.answer()

@admin_router.callback_query(GearAddStates.classes, F.data == "gear_classes_done")
async def gear_add_note_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('gear_classes'):
        await callback.answer("Выберите хотя бы один класс", show_alert=True)
        return
    await callback.message.edit_text(
        OPTIONAL_NOTE_PROMPT,
        reply_markup=build_optional_note_keyboard(),
    )
    await state.set_state(GearAddStates.note)
    await callback.answer()

async def save_new_gear(target, state: FSMContext, note: str):
    data = await state.get_data()
    try:
        await db.add_gear(
            data['gear_name'], data['gear_rarity'], data['gear_slot'], data['gear_emoji'],
            data['gear_level'], ", ".join(data['gear_classes']), note
        )
        await target.answer("✅ Снаряжение добавлено.")
    except Exception as e:
        await target.answer(f"❌ Ошибка: {e}")
    await state.clear()
    await target.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())

@admin_router.message(GearAddStates.note, F.text)
async def gear_save_with_note(message: types.Message, state: FSMContext):
    await save_new_gear(message, state, normalize_optional_note(message.text))

# ============================================================
# ОБРАБОТЧИКИ ДЛЯ КАРТ
# ============================================================

@admin_router.callback_query(CardListStates.list_page, F.data == "card_add_start")
async def card_add_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название карты:")
    await state.set_state(CardAddStates.name)

@admin_router.message(CardAddStates.name, F.text)
async def card_add_emoji(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if not name:
        await message.answer("Название не может быть пустым.")
        return
    await state.update_data(card_name=name)
    await message.answer("Введите эмодзи:")
    await state.set_state(CardAddStates.emoji)

@admin_router.message(CardAddStates.emoji, F.text)
async def card_add_emoji_input(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(card_emoji=emoji)
    keyboard = [
        [InlineKeyboardButton(text=GEAR_SLOT_LABELS[slot], callback_data=f"card_slot_{slot}")]
        for slot in GEAR_SLOTS
    ]
    await message.answer("Выберите слот:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(CardAddStates.slot)

@admin_router.callback_query(CardAddStates.slot, F.data.startswith("card_slot_"))
async def card_add_bonus1(callback: types.CallbackQuery, state: FSMContext):
    slot = callback.data.split("_")[2]
    await state.update_data(card_slot=slot)
    await callback.message.edit_text("Введите первый бонус (например: «Удача +2»):\nЕсли не нужно, отправьте «-».")
    await state.set_state(CardAddStates.bonus1)

@admin_router.message(CardAddStates.bonus1, F.text)
async def card_add_bonus2(message: types.Message, state: FSMContext):
    bonus1 = message.text.strip()
    if bonus1 == "-":
        bonus1 = ""
    await state.update_data(card_bonus1=bonus1)
    await message.answer("Введите второй бонус (или «-»):")
    await state.set_state(CardAddStates.bonus2)

@admin_router.message(CardAddStates.bonus2, F.text)
async def card_add_bonus3(message: types.Message, state: FSMContext):
    bonus2 = message.text.strip()
    if bonus2 == "-":
        bonus2 = ""
    await state.update_data(card_bonus2=bonus2)
    await message.answer("Введите третий бонус (или «-»):")
    await state.set_state(CardAddStates.bonus3)

@admin_router.message(CardAddStates.bonus3, F.text)
async def card_add_bonus4(message: types.Message, state: FSMContext):
    bonus3 = message.text.strip()
    if bonus3 == "-":
        bonus3 = ""
    await state.update_data(card_bonus3=bonus3)
    await message.answer("Введите четвёртый бонус (или «-»):")
    await state.set_state(CardAddStates.bonus4)

@admin_router.message(CardAddStates.bonus4, F.text)
async def card_add_note(message: types.Message, state: FSMContext):
    bonus4 = message.text.strip()
    if bonus4 == "-":
        bonus4 = ""
    await state.update_data(card_bonus4=bonus4)
    await message.answer(
        OPTIONAL_NOTE_PROMPT,
        reply_markup=build_optional_note_keyboard(),
    )
    await state.set_state(CardAddStates.note)


async def save_new_card(target: types.Message, state: FSMContext, note: str):
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
        await target.answer("✅ Карта добавлена.")
    except Exception as error:
        logger.exception("Не удалось добавить карту")
        await target.answer(f"❌ Ошибка: {error}")
    await state.clear()
    await target.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())


@admin_router.message(CardAddStates.note, F.text)
async def card_save(message: types.Message, state: FSMContext):
    await save_new_card(message, state, normalize_optional_note(message.text))


@admin_router.callback_query(
    StateFilter(ResourceAddStates.note, GearAddStates.note, CardAddStates.note),
    F.data == OPTIONAL_NOTE_SKIP_CALLBACK,
)
async def skip_new_entity_note(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    await callback.answer()
    if current_state == ResourceAddStates.note.state:
        await save_new_resource(callback.message, state, "")
    elif current_state == GearAddStates.note.state:
        await save_new_gear(callback.message, state, "")
    elif current_state == CardAddStates.note.state:
        await save_new_card(callback.message, state, "")

# ============================================================
# УПРАВЛЕНИЕ МОБАМИ
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
    drop_category = State()
    drop_list_page = State()
    drop_search = State()


async def get_sorted_locations() -> list[dict]:
    locations = await db.get_locations()
    return sorted(locations, key=lambda location: str(location.get('name') or '').casefold())


async def get_location_choice_keyboard(
    callback_prefix: str,
    cancel_callback: str | None = None,
    locations: list[dict] | None = None,
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


async def get_mob_edit_data(mob_id: int) -> dict | None:
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


def build_mob_edit_keyboard(mob: dict) -> InlineKeyboardMarkup:
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


@admin_router.callback_query(F.data == "admin_edit_mob")
async def start_edit_mob(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🐾 Управление мобами:\nВыберите локацию:",
        reply_markup=await get_mob_locations_keyboard(),
    )
    await state.set_state(MobStates.edit_select)
    await callback.answer()


@admin_router.callback_query(MobStates.edit_select, F.data.startswith("mob_location_"))
async def mob_location_select(callback: types.CallbackQuery, state: FSMContext):
    location_id = int(callback.data.rsplit("_", 1)[1])
    location = await db.get_location_by_id(location_id)
    if not location:
        await callback.answer("Локация не найдена", show_alert=True)
        return
    await state.update_data(mob_location_id=location_id)
    await callback.message.edit_text(
        f"🐾 Мобы: {location['emoji']} {location['name']}\nВыберите моба или добавьте нового:",
        reply_markup=await get_mob_list_keyboard(location_id, 1),
    )
    await callback.answer()


@admin_router.callback_query(MobStates.edit_select, F.data.startswith("mob_page_"))
async def mob_list_page(callback: types.CallbackQuery, state: FSMContext):
    _, _, location_id, page = callback.data.split("_")
    location_id, page = int(location_id), int(page)
    await state.update_data(mob_location_id=location_id)
    location = await db.get_location_by_id(location_id)
    loc = location or {'name': 'Локация', 'emoji': '📍'}
    await callback.message.edit_text(
        f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:",
        reply_markup=await get_mob_list_keyboard(location_id, page),
    )
    await callback.answer()


@admin_router.callback_query(MobStates.edit_select, F.data == "back_to_mob_locations")
async def back_to_mob_locations(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(mob_location_id=None)
    await callback.message.edit_text(
        "🐾 Управление мобами:\nВыберите локацию:",
        reply_markup=await get_mob_locations_keyboard(),
    )
    await callback.answer()

@admin_router.callback_query(MobStates.edit_select, F.data.startswith("edit_mob_"))
async def mob_edit_menu(callback: types.CallbackQuery, state: FSMContext):
    mob_id = int(callback.data.split("_")[2])
    mob = await get_mob_edit_data(mob_id)
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await callback.answer()
        return
    await state.update_data(mob_id=mob_id)
    await callback.message.edit_text(
        f"Редактирование моба ID {mob_id}",
        reply_markup=build_mob_edit_keyboard(mob),
    )
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@admin_router.callback_query(MobStates.edit_field, F.data.startswith("mob_edit_field_"))
async def mob_edit_field_prompt(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_", 3)[3]
    await state.update_data(edit_field=field)
    if field == 'location_id':
        locations = await get_sorted_locations()
        if not locations:
            await callback.answer("Нет доступных локаций.", show_alert=True)
            return
        await callback.message.edit_text(
            "Выберите новую локацию:",
            reply_markup=await get_location_choice_keyboard(
                "mob_edit_location_",
                cancel_callback="mob_location_change_cancel",
                locations=locations,
            ),
        )
        await state.set_state(MobStates.edit_new_value)
        await callback.answer()
        return
    await callback.message.edit_text(f"Введите новое значение для поля <b>{field}</b>:", parse_mode="HTML")
    await state.set_state(MobStates.edit_new_value)
    await callback.answer()


@admin_router.callback_query(MobStates.edit_new_value, F.data.startswith("mob_edit_location_"))
async def mob_update_location(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    location_id = int(callback.data.removeprefix("mob_edit_location_"))
    location = await db.get_location_by_id(location_id)
    if not mob_id or not location:
        await callback.answer("Моб или локация не найдены.", show_alert=True)
        return

    await db.update_mob_field(mob_id, 'location_id', location_id)
    mob = await get_mob_edit_data(mob_id)
    if not mob:
        await callback.message.edit_text("❌ Моб не найден.")
        await state.clear()
        await callback.answer()
        return

    await state.update_data(mob_location_id=location_id, edit_field=None)
    await state.set_state(MobStates.edit_field)
    await callback.message.edit_text(
        f"Редактирование моба ID {mob_id}",
        reply_markup=build_mob_edit_keyboard(mob),
    )
    await callback.answer("✅ Локация обновлена")


@admin_router.callback_query(MobStates.edit_new_value, F.data == "mob_location_change_cancel")
async def mob_edit_location_cancel(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob = await get_mob_edit_data(data.get('mob_id')) if data.get('mob_id') else None
    if not mob:
        await callback.message.edit_text("❌ Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    await state.update_data(edit_field=None)
    await state.set_state(MobStates.edit_field)
    await callback.message.edit_text(
        f"Редактирование моба ID {mob['id']}",
        reply_markup=build_mob_edit_keyboard(mob),
    )
    await callback.answer()

@admin_router.message(MobStates.edit_new_value, F.text)
async def mob_update_field(message: types.Message, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    field = data.get('edit_field')

    if not mob_id or not field:
        logger.warning(f"Ошибка состояния: mob_id={mob_id}, field={field}. Данные состояния: {data}")
        await message.answer(
            "❌ Ошибка состояния. Пожалуйста, начните редактирование моба заново.\n"
            "Выберите моба из списка:"
        )
        await state.clear()
        keyboard = await get_mob_locations_keyboard()
        await message.answer("Выберите моба для редактирования:", reply_markup=keyboard)
        await state.set_state(MobStates.edit_select)
        return

    new_value = message.text.strip()

    if field == 'location_id':
        await message.answer(
            "Выберите локацию кнопкой:",
            reply_markup=await get_location_choice_keyboard(
                "mob_edit_location_",
                cancel_callback="mob_location_change_cancel",
            ),
        )
        return

    if field in ('hp', 'dust_min', 'dust_max', 'exp'):
        try:
            new_value = int(new_value)
            if new_value < 0:
                raise ValueError
        except ValueError:
            await message.answer("❌ Введите положительное целое число.")
            return

    if field == 'emoji' and not is_valid_emoji(new_value):
        await message.answer("❌ Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return

    if field == 'name' and not new_value:
        await message.answer("❌ Имя не может быть пустым.")
        return

    try:
        await db.update_mob_field(mob_id, field, new_value)

        mob = await get_mob_edit_data(mob_id)
        if not mob:
            await message.answer("❌ Моб не найден. Возврат в админку.")
            await state.clear()
            await message.answer("🔧 Админ-панель", reply_markup=get_admin_main_keyboard())
            return

        await message.answer(f"✅ Поле {field} обновлено.")
        await message.answer(
            f"Редактирование моба ID {mob_id}",
            reply_markup=build_mob_edit_keyboard(mob),
        )

        await state.update_data(edit_field=None)
        await state.set_state(MobStates.edit_field)

        try:
            await message.delete()
        except TelegramAPIError:
            pass

    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        logger.exception("Ошибка при обновлении поля моба")

@admin_router.callback_query(F.data == "back_to_mob_list")
async def back_to_mob_list_from_edit(callback: types.CallbackQuery, state: FSMContext):
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
        await callback.message.edit_text(
            f"🐾 Мобы: {loc['emoji']} {loc['name']}\nВыберите моба или добавьте нового:",
            reply_markup=await get_mob_list_keyboard(location_id, 1),
        )
    else:
        await callback.message.edit_text(
            "🐾 Управление мобами:\nВыберите локацию:",
            reply_markup=await get_mob_locations_keyboard(),
        )
    await callback.answer()

@admin_router.callback_query(MobStates.edit_field, F.data == "mob_delete")
async def mob_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    mob = await db.execute_query("SELECT name FROM mobs WHERE id = ?", (mob_id,))
    if not mob:
        await callback.message.edit_text("Моб не найден.")
        await callback.answer()
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_mob_delete")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_mob_list")]
    ])
    await callback.message.edit_text(
        f"Удалить моба <b>{escape_html(mob[0]['name'])}</b>?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@admin_router.callback_query(F.data == "confirm_mob_delete")
async def mob_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data['mob_id']
    await db.delete_mob(mob_id)
    await callback.message.edit_text("✅ Моб удалён.")
    keyboard = await get_mob_locations_keyboard()
    await callback.message.answer("🐾 Управление мобами:\nВыберите моба или добавьте нового:", reply_markup=keyboard)
    await state.set_state(MobStates.edit_select)
    await callback.answer()

# ------------------- Управление дропом -------------------
def build_drop_categories_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Поиск лута по названию", callback_data="drop_search_start")],
        [InlineKeyboardButton(text="📦 Ресурсы", callback_data="drop_category_resource")],
        [InlineKeyboardButton(text="⚔️ Экипировка", callback_data="drop_category_gear")],
        [InlineKeyboardButton(text="🃏 Карты", callback_data="drop_category_card")],
        [InlineKeyboardButton(text="🔙 Назад к мобу", callback_data="back_to_mob_list")],
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
        rarity = RARITY_EMOJIS.get(item.get('rarity'), '') if category == 'gear' else ''
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


def build_drop_search_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    category_icons = {'resource': '📦', 'gear': '⚔️', 'card': '🃏'}
    rows = []
    for item in items:
        status = '✅' if item['enabled'] else '❌'
        category_icon = category_icons[item['item_type']]
        rarity_icon = RARITY_EMOJIS.get(item.get('rarity'), '')
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

@admin_router.callback_query(MobStates.edit_field, F.data == "mob_drop_menu")
async def mob_drop_category(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите тип дропа:", reply_markup=build_drop_categories_keyboard())
    await state.set_state(MobStates.drop_category)
    await callback.answer()


@admin_router.callback_query(MobStates.drop_category, F.data == "drop_search_start")
async def start_drop_search(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(drop_search_query=None)
    await callback.message.edit_text(
        "🔎 Введите часть названия ресурса, экипировки или карты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="drop_search_back")]
        ]),
    )
    await state.set_state(MobStates.drop_search)
    await callback.answer()


@admin_router.message(MobStates.drop_search, F.text)
async def show_drop_search_results(message: types.Message, state: FSMContext):
    query = message.text.strip()
    if not query:
        await message.answer("Введите хотя бы один символ для поиска.")
        return
    data = await state.get_data()
    mob_id = data.get('mob_id')
    if not mob_id:
        await state.clear()
        await message.answer("Моб не выбран. Откройте управление дропом заново.")
        return
    items = await db.search_drop_items(mob_id, query, limit=20)
    await state.update_data(drop_search_query=query)
    text = (
        f"🔎 Результаты по запросу «{query}»:\n"
        "✅ — уже падает, ❌ — не падает. Нажмите на предмет для переключения."
        if items
        else f"По запросу «{query}» ничего не найдено."
    )
    await message.answer(text, reply_markup=build_drop_search_keyboard(items))


@admin_router.callback_query(MobStates.drop_search, F.data == "drop_search_again")
async def repeat_drop_search(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(drop_search_query=None)
    await callback.message.edit_text(
        "🔎 Введите новый поисковый запрос:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="drop_search_back")]
        ]),
    )
    await callback.answer()


@admin_router.callback_query(MobStates.drop_search, F.data.startswith("drop_search_toggle_"))
async def toggle_drop_from_search(callback: types.CallbackQuery, state: FSMContext):
    _, _, _, category, item_id = callback.data.split("_")
    item_id = int(item_id)
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
    await callback.message.edit_reply_markup(
        reply_markup=build_drop_search_keyboard(items)
    )


@admin_router.callback_query(MobStates.drop_search, F.data == "drop_search_back")
async def back_from_drop_search(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(drop_search_query=None)
    await callback.message.edit_text(
        "Выберите тип дропа:", reply_markup=build_drop_categories_keyboard()
    )
    await state.set_state(MobStates.drop_category)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data == "back_to_mob_list")
async def back_to_mob_edit_from_drop_category(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mob_id = data.get('mob_id')
    mob = await get_mob_edit_data(mob_id)
    if not mob:
        await callback.message.edit_text("❌ Моб не найден.")
        await state.clear()
        await callback.answer()
        return
    await callback.message.edit_text(
        f"Редактирование моба ID {mob_id}",
        reply_markup=build_mob_edit_keyboard(mob),
    )
    await state.set_state(MobStates.edit_field)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data.startswith("drop_category_"))
async def show_drop_filters(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.split('_')[2]
    await callback.message.edit_text(
        "Выберите категорию:",
        reply_markup=build_drop_filters_keyboard(category),
    )
    await state.update_data(drop_category=category)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data.startswith("drop_filter_"))
async def show_drop_list(callback: types.CallbackQuery, state: FSMContext):
    _, _, category, raw_filter_index = callback.data.split('_')
    filter_index = int(raw_filter_index)
    filter_value = resolve_drop_filter(category, filter_index)
    data = await state.get_data()
    keyboard = await get_drop_list_keyboard(data['mob_id'], category, filter_value, 1)
    await callback.message.edit_text("✅ — падает, ❌ — не падает", reply_markup=keyboard)
    await state.update_data(
        drop_category=category,
        drop_filter_index=filter_index,
        drop_filter_value=filter_value,
        drop_page=1,
    )
    await state.set_state(MobStates.drop_list_page)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_page_"))
async def drop_page(callback: types.CallbackQuery, state: FSMContext):
    _, _, category, raw_page = callback.data.split('_')
    page = int(raw_page)
    data = await state.get_data()
    keyboard = await get_drop_list_keyboard(
        data['mob_id'],
        category,
        data['drop_filter_value'],
        page,
    )
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await state.update_data(drop_page=page)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("drop_toggle_"))
async def toggle_drop(callback: types.CallbackQuery, state: FSMContext):
    _, _, category, raw_item_id, raw_page = callback.data.split('_')
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
    await callback.message.edit_reply_markup(reply_markup=keyboard)

@admin_router.callback_query(MobStates.drop_list_page, F.data.startswith("back_to_drop_filters_"))
async def back_to_drop_filters(callback: types.CallbackQuery, state: FSMContext):
    category = callback.data.rsplit('_', 1)[1]
    await callback.message.edit_text(
        "Выберите категорию:",
        reply_markup=build_drop_filters_keyboard(category),
    )
    await state.set_state(MobStates.drop_category)
    await callback.answer()

@admin_router.callback_query(MobStates.drop_category, F.data == "back_to_drop_categories")
async def back_to_drop_categories(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Выберите тип дропа:",
        reply_markup=build_drop_categories_keyboard(),
    )
    await callback.answer()

# ---------- Добавление моба ----------
@admin_router.callback_query(MobStates.edit_select, F.data == "mob_add_start")
async def start_add_mob(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите название моба:")
    await state.set_state(MobStates.add_name)
    await callback.answer()

@admin_router.message(MobStates.add_name, F.text)
async def add_mob_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Введите эмодзи моба:")
    await state.set_state(MobStates.add_emoji)

@admin_router.message(MobStates.add_emoji, F.text)
async def add_mob_emoji(message: types.Message, state: FSMContext):
    emoji = message.text.strip()
    if not is_valid_emoji(emoji):
        await message.answer("Эмодзи должен состоять из 1 или 2 символов (не буквы и не цифры).")
        return
    await state.update_data(emoji=emoji)
    await message.answer("Введите HP:")
    await state.set_state(MobStates.add_hp)

@admin_router.message(MobStates.add_hp, F.text)
async def add_mob_hp(message: types.Message, state: FSMContext):
    try:
        hp = int(message.text.strip())
        if hp < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(hp=hp)
    await message.answer("Введите dust_min:")
    await state.set_state(MobStates.add_dust_min)

@admin_router.message(MobStates.add_dust_min, F.text)
async def add_mob_dust_min(message: types.Message, state: FSMContext):
    try:
        dust_min = int(message.text.strip())
        if dust_min < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(dust_min=dust_min)
    await message.answer("Введите dust_max:")
    await state.set_state(MobStates.add_dust_max)

@admin_router.message(MobStates.add_dust_max, F.text)
async def add_mob_dust_max(message: types.Message, state: FSMContext):
    try:
        dust_max = int(message.text.strip())
        if dust_max < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    data = await state.get_data()
    if dust_max < data['dust_min']:
        await message.answer("dust_max не может быть меньше dust_min")
        return
    await state.update_data(dust_max=dust_max)
    await message.answer("Введите опыт (exp):")
    await state.set_state(MobStates.add_exp)

@admin_router.message(MobStates.add_exp, F.text)
async def add_mob_exp(message: types.Message, state: FSMContext):
    try:
        exp = int(message.text.strip())
        if exp < 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите целое положительное число.")
        return
    await state.update_data(exp=exp)
    locations = await get_sorted_locations()
    if not locations:
        await message.answer("Нет локаций.")
        await state.clear()
        return
    keyboard = await get_location_choice_keyboard("mob_add_location_", locations=locations)
    await message.answer("Выберите локацию:", reply_markup=keyboard)
    await state.set_state(MobStates.add_location)

@admin_router.callback_query(MobStates.add_location, F.data.startswith("mob_add_location_"))
async def add_mob_location(callback: types.CallbackQuery, state: FSMContext):
    location_id = int(callback.data.removeprefix("mob_add_location_"))
    if not await db.get_location_by_id(location_id):
        await callback.answer("Локация не найдена.", show_alert=True)
        return
    data = await state.get_data()
    try:
        await db.execute_query(
            "INSERT INTO mobs (name, emoji, hp, dust_min, dust_max, exp, location_id) VALUES (?,?,?,?,?,?,?)",
            (data['name'], data['emoji'], data['hp'], data['dust_min'], data['dust_max'], data['exp'], location_id)
        )
        await callback.message.edit_text("✅ Моб добавлен.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
    keyboard = await get_mob_locations_keyboard()
    await callback.message.answer(
        "🐾 Управление мобами:\nВыберите моба или добавьте нового:",
        reply_markup=keyboard,
    )
    await state.set_state(MobStates.edit_select)
    await callback.answer()


# ============================================================
# УПРАВЛЕНИЕ РЕЦЕПТАМИ
# ============================================================

class RecipeStates(StatesGroup):
    list_type = State()
    list_page = State()
    view_recipe = State()
    add_confirm = State()
    add_ingredient = State()
    add_owner = State()
    manage_owners = State()
    delete_owner_confirm = State()
    edit_ingredient = State()
    edit_ingredient_quantity = State()
    delete_confirm = State()

async def get_recipe_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Снаряжение", callback_data="recipe_type_gear")],
        [InlineKeyboardButton(text="⚗️ Алхимия", callback_data="recipe_type_resource")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")]
    ])


def get_recipe_type_title(result_type: str) -> str:
    return "Снаряжение" if result_type == "gear" else "Алхимия"

@admin_router.callback_query(F.data == "admin_manage_recipes")
async def manage_recipes_type(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)

async def get_recipe_list_keyboard(result_type: str, page: int):
    offset = (page-1)*ADMIN_ITEMS_PER_PAGE
    recipes = await db.get_all_recipes(result_type, offset, ADMIN_ITEMS_PER_PAGE+1)
    has_next = len(recipes) > ADMIN_ITEMS_PER_PAGE
    recipes = recipes[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for r in recipes:
        if result_type == 'gear':
            text = f"{r['result_emoji']} {r['result_name']} (ID рец.{r['id']}) | ингр:{r['ingredient_count']} влад:{r['owner_count']}"
        else:
            text = f"{r['result_emoji']} {r['result_name']} (ID рец.{r['id']}) | ингр:{r['ingredient_count']}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"recipe_view_{r['id']}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"recipe_page_{result_type}_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"recipe_page_{result_type}_{page+1}"))
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
    await callback.message.edit_text(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_page_"))
async def recipe_list_page(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    result_type = parts[2]
    page = int(parts[3])
    await state.update_data(recipe_result_type=result_type, recipe_page=page)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)

@admin_router.callback_query(RecipeStates.list_page, F.data == "recipe_back_to_type")
async def recipe_back_to_type(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)

async def show_recipe(target, recipe: dict, state: FSMContext):
    if recipe['result_type'] == 'gear':
        gear = await db.get_gear_by_id(recipe['result_id'])
        result_info = f"{escape_html(gear['emoji'])} {escape_html(gear['name'])}" if gear else f"ID {recipe['result_id']}"
    else:
        res = await db.get_resource_by_id(recipe['result_id'])
        result_info = f"{escape_html(res['emoji'])} {escape_html(res['name'])}" if res else f"ID {recipe['result_id']}"
    text = f"📜 Рецепт ID {recipe['id']}\n🎁 Результат: {result_info} (количество: {recipe['quantity']})\n\n"
    text += "<b>Ингредиенты:</b>\n"
    for ing in recipe['ingredients']:
        text += f"  {escape_html(ing['emoji'])} {escape_html(ing['name'])} — {ing['quantity']} шт.\n"
    if not recipe['ingredients']:
        text += "<i>Нет ингредиентов</i>\n"

    if recipe['result_type'] == 'gear':
        text += "\n👥 <b>Владельцы:</b>\n"
        for owner in recipe['owners']:
            text += f"  @{escape_html(clean_username(owner))}\n"
        if not recipe['owners']:
            text += "<i>Нет владельцев</i>\n"

    keyboard = []
    if recipe['result_type'] == 'gear':
        keyboard.append([InlineKeyboardButton(text="👤 Добавить владельца", callback_data="recipe_add_owner")])
        keyboard.append([InlineKeyboardButton(text="👥 Управлять владельцами", callback_data="recipe_manage_owners")])
    keyboard.append([InlineKeyboardButton(text="➕ Добавить ингредиент", callback_data="recipe_add_ingredient")])
    keyboard.append([InlineKeyboardButton(text="✏️ Редактировать ингредиенты", callback_data="recipe_edit_ingredients")])
    keyboard.append([InlineKeyboardButton(text="❌ Удалить рецепт", callback_data="recipe_delete")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к списку", callback_data="recipe_back_to_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="admin_cancel_edit")])

    if isinstance(target, types.CallbackQuery):
        ingredient_rows = "".join(
            f"<tr><td>{escape_html(ing['emoji'])} {escape_html(ing['name'])}</td>"
            f"<td>{ing['quantity']} шт.</td></tr>"
            for ing in recipe['ingredients']
        ) or "<tr><td>Нет ингредиентов</td><td>—</td></tr>"
        rich_html = (
            f"<b>📜 Рецепт ID {recipe['id']}</b><br>"
            f"🎁 Результат: {result_info} · {recipe['quantity']} шт.<br>"
            f"{RICH_TABLE_OPEN}<tbody><tr><th>Ингредиент</th><th>Количество</th></tr>"
            f"{ingredient_rows}</tbody></table>"
        )
        if recipe['result_type'] == 'gear':
            owners = "<br>".join(
                f"@{escape_html(clean_username(owner))}" for owner in recipe['owners']
            ) or "Нет владельцев"
            rich_html += f"<details><summary>👥 Владельцы</summary>{owners}</details>"
        await edit_admin_rich(
            target,
            rich_html,
            InlineKeyboardMarkup(inline_keyboard=keyboard),
            fallback_html=text,
        )
    else:
        await target.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.view_recipe)

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_view_"))
async def recipe_view(callback: types.CallbackQuery, state: FSMContext):
    recipe_id = int(callback.data.split("_")[2])
    recipe = await db.get_recipe_details(recipe_id)
    if not recipe:
        await callback.message.edit_text("Рецепт не найден.")
        return
    await state.update_data(recipe_id=recipe_id, recipe_result_type=recipe['result_type'])
    await show_recipe(callback, recipe, state)
    await callback.answer()

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_back_to_list")
async def recipe_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    result_type = data.get('recipe_result_type', 'gear')
    page = data.get('recipe_page', 1)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

@admin_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_add_"))
async def recipe_add_choose_item(callback: types.CallbackQuery, state: FSMContext):
    result_type = callback.data.split("_")[2]
    await state.update_data(new_recipe_type=result_type)
    if result_type == 'gear':
        all_items = await db.get_all_gear_simple()
        existing = await db.execute_query("SELECT result_id FROM recipes WHERE result_type='gear'")
    else:
        all_items = await db.get_all_resources_simple()
        existing = await db.execute_query("SELECT result_id FROM recipes WHERE result_type='resource'")
    existing_ids = {e['result_id'] for e in existing}
    available = [it for it in all_items if it['id'] not in existing_ids]
    if not available:
        await callback.message.edit_text("Для всех элементов уже есть рецепты.")
        return
    keyboard = [[InlineKeyboardButton(text=f"{it['emoji']} {it['name']}", callback_data=f"recipe_new_target_{it['id']}")] for it in available]
    await callback.message.edit_text("Выберите элемент для создания рецепта:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.add_confirm)

@admin_router.callback_query(RecipeStates.add_confirm, F.data.startswith("recipe_new_target_"))
async def recipe_create(callback: types.CallbackQuery, state: FSMContext):
    result_id = int(callback.data.split("_")[3])
    data = await state.get_data()
    result_type = data['new_recipe_type']
    try:
        recipe_id = await db.create_recipe(result_type, result_id, 1)
        await callback.message.edit_text(f"✅ Рецепт создан (ID {recipe_id}). Теперь добавьте ингредиенты и владельцев.")
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка: {e}")
        return
    recipe = await db.get_recipe_details(recipe_id)
    await state.update_data(recipe_id=recipe_id, recipe_result_type=result_type, recipe_page=1)
    await show_recipe(callback, recipe, state)
    await callback.answer()

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_ingredient")
async def recipe_add_ingredient_select(callback: types.CallbackQuery, state: FSMContext):
    resources = await db.get_all_resources_simple()
    if not resources:
        await callback.answer("Нет ресурсов", show_alert=True)
        return
    await state.update_data(ingredient_resources=resources, ingredient_page=1)
    await show_ingredient_page(callback, resources, 1, state)

async def show_ingredient_page(target, resources, page, state):
    from admin_utils import ADMIN_ITEMS_PER_PAGE
    per_page = ADMIN_ITEMS_PER_PAGE
    start = (page-1)*per_page
    end = start+per_page
    page_items = resources[start:end]
    has_next = end < len(resources)
    keyboard = []
    for r in page_items:
        keyboard.append([InlineKeyboardButton(text=f"{r['emoji']} {r['name']}", callback_data=f"recipe_ing_select_{r['id']}_{page}")])
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"recipe_ing_page_{page-1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Вперед ▶️", callback_data=f"recipe_ing_page_{page+1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Готово", callback_data="recipe_finish_adding")])
    if isinstance(target, types.CallbackQuery):
        await target.message.edit_text("Выберите ресурс для добавления в ингредиенты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await target.answer("Выберите ресурс:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.add_ingredient)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_page_"))
async def recipe_ing_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[3])
    data = await state.get_data()
    resources = data.get('ingredient_resources')
    if not resources:
        await callback.answer("Ошибка", show_alert=True)
        return
    await show_ingredient_page(callback, resources, page, state)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_select_"))
async def recipe_ing_quantity(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    resource_id = int(parts[3])
    page = int(parts[4]) if len(parts)>4 else 1
    await state.update_data(temp_resource_id=resource_id, ingredient_return_page=page, edit_action='add')
    await callback.message.edit_text("Введите количество (целое число):")
    await state.set_state(RecipeStates.edit_ingredient_quantity)

@admin_router.message(RecipeStates.edit_ingredient_quantity, F.text)
async def recipe_ing_save_quantity(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty <= 0: raise ValueError
    except (TypeError, ValueError):
        await message.answer("Введите положительное целое число.")
        return
    data = await state.get_data()
    recipe_id = data['recipe_id']
    action = data.get('edit_action')
    if action == 'add':
        resource_id = data['temp_resource_id']
        await db.add_ingredient(recipe_id, resource_id, qty)
        await message.answer("✅ Ингредиент добавлен. Выберите следующий или нажмите 'Готово'.")
        resources = data.get('ingredient_resources')
        page = data.get('ingredient_return_page', 1)
        if resources:
            await show_ingredient_page(message, resources, page, state)
            return
    elif action == 'change':
        resource_id = data['edit_resource_id']
        await db.update_ingredient(recipe_id, resource_id, qty)
        await message.answer("✅ Количество обновлено.")
    else:
        await message.answer("Ошибка.")
        return
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(message, recipe, state)

@admin_router.callback_query(RecipeStates.add_ingredient, F.data == "recipe_finish_adding")
@admin_router.callback_query(RecipeStates.manage_owners, F.data == "recipe_owners_back")
@admin_router.callback_query(
    StateFilter(RecipeStates.edit_ingredient, RecipeStates.delete_confirm),
    F.data == "recipe_back_to_view",
)
async def recipe_show_current(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)
    await callback.answer()

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_owner")
async def recipe_add_owner_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data.get('recipe_id')
    recipe = await db.get_recipe_details(recipe_id)
    if recipe and recipe['result_type'] != 'gear':
        await callback.answer("Владельцы добавляются только для рецептов снаряжения.", show_alert=True)
        return
    await callback.message.edit_text("Введите username владельца (без @):")
    await state.set_state(RecipeStates.add_owner)

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


async def show_recipe_owners(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    owners = await db.get_recipe_owners(recipe_id)
    keyboard = [
        [InlineKeyboardButton(
            text=f"❌ @{clean_username(owner)}",
            callback_data=f"recipe_owner_delete_{index}",
        )]
        for index, owner in enumerate(owners)
    ]
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к рецепту", callback_data="recipe_owners_back")])
    text = "👥 Выберите владельца для удаления:" if owners else "👥 У рецепта пока нет владельцев."
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.manage_owners)


@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_manage_owners")
@admin_router.callback_query(RecipeStates.delete_owner_confirm, F.data == "recipe_owner_delete_cancel")
async def recipe_show_owners(callback: types.CallbackQuery, state: FSMContext):
    await show_recipe_owners(callback, state)
    await callback.answer()


@admin_router.callback_query(RecipeStates.manage_owners, F.data.startswith("recipe_owner_delete_"))
async def recipe_owner_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    index = int(callback.data.rsplit("_", 1)[1])
    data = await state.get_data()
    owners = await db.get_recipe_owners(data['recipe_id'])
    if index < 0 or index >= len(owners):
        await callback.answer("Список владельцев изменился. Откройте его заново.", show_alert=True)
        return
    owner = owners[index]
    await state.update_data(selected_recipe_owner=owner)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="recipe_owner_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_owner_delete_cancel")],
    ])
    await callback.message.edit_text(
        f"Удалить владельца <b>@{escape_html(clean_username(owner))}</b> из рецепта?",
        parse_mode="HTML",
        reply_markup=keyboard,
    )
    await state.set_state(RecipeStates.delete_owner_confirm)
    await callback.answer()


@admin_router.callback_query(RecipeStates.delete_owner_confirm, F.data == "recipe_owner_delete_yes")
async def recipe_owner_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    owner = data.get('selected_recipe_owner')
    if owner:
        await db.remove_recipe_owner(data['recipe_id'], owner)
    await show_recipe_owners(callback, state)
    await callback.answer(f"Владелец @{clean_username(owner)} удалён" if owner else "Владелец не найден")


@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_edit_ingredients")
async def recipe_edit_ingredients_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    if not recipe['ingredients']:
        await callback.answer("Нет ингредиентов", show_alert=True)
        return
    keyboard = []
    for ing in recipe['ingredients']:
        keyboard.append([InlineKeyboardButton(text=f"{ing['emoji']} {ing['name']} — {ing['quantity']} шт.", callback_data=f"recipe_edit_ing_{ing['resource_id']}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к рецепту", callback_data="recipe_back_to_view")])
    await callback.message.edit_text("Выберите ингредиент для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.edit_ingredient)

@admin_router.callback_query(RecipeStates.edit_ingredient, F.data.startswith("recipe_edit_ing_"))
async def recipe_edit_ing_options(callback: types.CallbackQuery, state: FSMContext):
    resource_id = int(callback.data.split("_")[3])
    await state.update_data(edit_resource_id=resource_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить количество", callback_data="recipe_ing_change")],
        [InlineKeyboardButton(text="❌ Удалить ингредиент", callback_data="recipe_ing_delete")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="recipe_back_to_edit_list")]
    ])
    await callback.message.edit_text("Что сделать?", reply_markup=keyboard)
    await state.set_state(RecipeStates.edit_ingredient_quantity)

@admin_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_change")
async def recipe_ing_change_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новое количество:")
    await state.update_data(edit_action='change')

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

@admin_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_delete")
async def recipe_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="recipe_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_back_to_view")]
    ])
    await callback.message.edit_text("Удалить рецепт?", reply_markup=keyboard)
    await state.set_state(RecipeStates.delete_confirm)

@admin_router.callback_query(RecipeStates.delete_confirm, F.data == "recipe_delete_yes")
async def recipe_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    result_type = data.get('recipe_result_type', 'gear')
    await db.delete_recipe(recipe_id)
    await callback.message.edit_text("✅ Рецепт удалён.")
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await callback.message.answer(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

# ============================================================
# МНОЖЕСТВЕННЫЙ ВЫБОР КЛАССОВ ДЛЯ СУЩЕСТВУЮЩЕГО СНАРЯЖЕНИЯ
# ============================================================

@admin_router.callback_query(GenericEditStates.select_field, F.data == "edit_field_classes")
async def gear_edit_classes_start(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get('editing_entity') != 'gear':
        await callback.answer()
        return
    gear = await db.get_gear_by_id(data['entity_id'])
    selected = list(parse_gear_classes(gear.get('classes')))
    await state.update_data(gear_classes=selected)
    await callback.message.edit_text(
        "Выберите один или несколько классов, затем нажмите «Готово»:",
        reply_markup=build_gear_classes_keyboard(selected)
    )
    await state.set_state(GearClassEditStates.selecting)
    await callback.answer()

@admin_router.callback_query(GearClassEditStates.selecting, F.data == "gear_classes_done")
async def gear_edit_classes_save(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = data.get('gear_classes', [])
    if not selected:
        await callback.answer("Выберите хотя бы один класс", show_alert=True)
        return
    gear_id = data['entity_id']
    await db.update_gear(gear_id, classes=", ".join(selected))
    gear = await db.get_gear_by_id(gear_id)
    await show_edit_menu(callback, state, gear_id, ENTITY_CONFIGS['gear'], gear)

# ============================================================
# Регистрация универсальных обработчиков (CRUD)
# ============================================================

register_generic_handlers(admin_router, lambda: ENTITY_CONFIGS)

# ============================================================
# Основная команда для админ-панели
# ============================================================
@admin_router.message(Command("kombat"))
async def admin_panel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🔧 <b>Админ-панель</b>\nВыберите действие:", parse_mode="HTML",
                         reply_markup=get_admin_main_keyboard())
