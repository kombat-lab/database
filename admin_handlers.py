import logging
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
    GenericEditStates,
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
from utils import is_valid_emoji

logger = logging.getLogger(__name__)

ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",") if x.strip().isdigit()]

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_router = Router()

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
admin_router.include_router(mob_router)
admin_router.include_router(recipe_router)
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
