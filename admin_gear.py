"""One durable editor for equipment, crafting, learning and drop sources."""

from collections.abc import Mapping
from typing import Literal

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_utils import get_admin_main_keyboard
from database import db
from game_constants import (
    GEAR_CLASS_ORDER, GEAR_SLOT_LABELS, GEAR_SLOTS, RARITY_KEYS, RARITY_LABELS,
    format_gear_classes, parse_gear_classes,
)
from recipe_domain import (
    DomainError, DraftConflictError, GearDraft, GearDraftPayload, MaterialInput,
    validate_draft_payload, MAX_RESOURCE_NAME_LENGTH,
)
from telegram_helpers import (
    get_bound_bot, get_callback_data, get_callback_message, get_message_text, get_message_user,
)
from utils import escape_html, is_valid_emoji
from telegram_text import split_html

gear_router = Router()
PAGE_SIZE = 8
Section = Literal['home', 'profile', 'materials', 'learning', 'sources', 'preview']


class GearEditorStates(StatesGroup):
    editing = State()
    input = State()
    drafts = State()


def draft_callback(draft: GearDraft, action: str) -> str:
    data = f"gw:{draft['draft_id']}:{draft['revision']}:{action}"
    if len(data.encode('utf-8')) > 64:
        raise ValueError('Draft callback is too long')
    return data


def button(draft: GearDraft, label: str, action: str) -> list[list[InlineKeyboardButton]]:
    return [[InlineKeyboardButton(text=label[:100], callback_data=draft_callback(draft, action))]]


def editor_caption(draft: GearDraft) -> str:
    payload = draft['payload']
    identity = f" · ID {payload['gear_id']}" if 'gear_id' in payload else ' · новый предмет'
    return f"<b>{escape_html(payload.get('emoji', '⚔️'))} {escape_html(payload.get('name', 'Снаряжение'))}{identity}</b>"


async def remember(state: FSMContext, draft: GearDraft, *, section: str = 'home') -> None:
    await state.update_data(
        gear_draft_id=draft['draft_id'], gear_draft_revision=draft['revision'],
        gear_draft_message_id=draft['message_id'], gear_draft_section=section,
        gear_draft_input=None,
    )
    await state.set_state(GearEditorStates.editing)


async def render(
    target: types.Message, state: FSMContext, draft: GearDraft, text: str,
    rows: list[list[InlineKeyboardButton]], *, section: str = 'home', input_action: str | None = None,
) -> None:
    # The durable revision is recorded before delivery. A failed Telegram request
    # can be recovered through the drafts list without repeating a catalog write.
    await remember(state, draft, section=section)
    if input_action is not None:
        await state.update_data(gear_draft_input=input_action)
        await state.set_state(GearEditorStates.input)
    try:
        await get_bound_bot(target).edit_message_text(
            chat_id=draft['chat_id'], message_id=draft['message_id'],
            text=f"{editor_caption(draft)}\n\n{text}", parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
    except TelegramBadRequest as error:
        if 'message is not modified' not in str(error).lower():
            raise


async def revision(draft: GearDraft, payload: Mapping[str, object] | None = None) -> GearDraft:
    return await db.update_gear_draft(
        draft['draft_id'], expected_revision=draft['revision'],
        owner_user_id=draft['owner_user_id'], chat_id=draft['chat_id'], message_id=draft['message_id'],
        payload=validate_draft_payload(dict(payload) if payload is not None else draft['payload']),
    )


async def active_draft(callback: types.CallbackQuery, state: FSMContext) -> tuple[GearDraft, str]:
    parts = get_callback_data(callback).split(':', 3)
    if len(parts) != 4 or parts[0] != 'gw' or not parts[2].isdigit():
        raise DraftConflictError('Некорректная кнопка редактора.')
    message = get_callback_message(callback)
    data = await state.get_data()
    draft = await db.get_gear_draft(parts[1], owner_user_id=callback.from_user.id, chat_id=message.chat.id)
    if (
        draft is None or draft['status'] != 'editing' or draft['revision'] != int(parts[2])
        or draft['message_id'] != message.message_id or data.get('gear_draft_id') != draft['draft_id']
        or data.get('gear_draft_revision') != draft['revision']
    ):
        raise DraftConflictError('Эта кнопка устарела. Откройте текущий черновик через админку.')
    return draft, parts[3]


async def show_section(target: types.Message, state: FSMContext, draft: GearDraft, section: str = 'home') -> None:
    if section == 'preview':
        await show_full_preview(target, state, draft, 0)
        return
    p = draft['payload']
    rows: list[list[InlineKeyboardButton]] = []
    if section == 'profile':
        text = 'Профиль предмета. Все изменения сохраняются в черновике.'
        for field, label in [('name', 'Название'), ('emoji', 'Эмодзи'), ('level', 'Уровень'), ('note', 'Описание')]:
            rows += button(draft, f"{label}: {str(p.get(field, '—'))[:55]}", f'input:{field}')
        rows += button(draft, f"Редкость: {RARITY_LABELS.get(p.get('rarity', ''), 'не выбрана')}", 'options:rarity')
        rows += button(draft, f"Слот: {GEAR_SLOT_LABELS.get(p.get('slot', ''), 'не выбран')}", 'options:slot')
        rows += button(draft, f"Классы: {format_gear_classes(p.get('classes'))}", 'options:classes')
    elif section == 'materials':
        enabled = p.get('craftable', False)
        text = 'Материалы расходуются при каждом изготовлении. Изучаемый свиток настраивается отдельно.\n'
        text += f"Результат: {p.get('quantity', 1)} шт.\n"
        for index, material in enumerate(p.get('materials', [])):
            name = material.get('name', '')
            resource_id = material.get('resource_id')
            if resource_id is not None:
                resource = await db.get_resource_by_id(resource_id)
                name = resource['name'] if resource else f'Удалённый ресурс {resource_id}'
            if index < PAGE_SIZE:
                text += f"• {escape_html(name[:160])} × {material['quantity']}\n"
        if len(p.get('materials', [])) > PAGE_SIZE:
            text += f"Всего материалов: {len(p['materials'])}. Откройте список для редактирования.\n"
        rows += button(draft, '☑️ Изготавливается' if enabled else '⬜ Не изготавливается', 'craft')
        if enabled:
            rows += button(draft, '➕ Найти / добавить материал', 'pick:material:0')
            rows += button(draft, '✏️ Количества и удаление', 'material_list:0')
            rows += button(draft, 'Количество результата', 'input:quantity')
    elif section == 'learning':
        scroll = p.get('learning_scroll')
        text = 'Свиток изучается один раз. Он не расходуется при каждом крафте.\n'
        if scroll is None:
            text += 'Изучение свитка не требуется.'
        elif 'resource_id' in scroll:
            resource = await db.get_resource_by_id(scroll['resource_id'])
            text += f"Свиток: {escape_html(resource['name'][:256] if resource else 'ресурс удалён')}"
        else:
            text += f"Будет создан свиток: {escape_html(scroll.get('name', 'Рецепт (' + p.get('name', 'предмет') + ')'))}"
        if p.get('craftable', False):
            rows += button(draft, '☑️ Изучается один раз' if scroll is not None else '⬜ Требуется изучение', 'learning_toggle')
            if scroll is not None:
                rows += button(draft, '📜 Выбрать существующий свиток', 'pick:scroll:0')
                rows += button(draft, '✨ Создать свиток автоматически', 'learning_auto')
        else:
            text += '\nСначала включите изготовление в разделе материалов.'
        if 'gear_id' in p:
            rows += button(draft, '👥 Кто изучил рецепт', 'owners')
        else:
            text += '\nСписок изучивших игроков доступен после первого сохранения.'
    elif section == 'sources':
        text = 'Выберите мобов отдельно для готового предмета и для изучаемого свитка.'
        rows += button(draft, f"⚔️ Предмет: {len(p.get('gear_mob_ids', []))} источников", 'pick:gear_mob:0')
        if p.get('learning_scroll') is not None:
            rows += button(draft, f"📜 Свиток: {len(p.get('scroll_mob_ids', []))} источников", 'pick:scroll_mob:0')
    else:
        text = (
            f"{RARITY_LABELS.get(p.get('rarity', ''), 'Редкость не выбрана')}\n"
            f"{GEAR_SLOT_LABELS.get(p.get('slot', ''), 'Слот не выбран')} · уровень {p.get('level', 1)}\n"
            f"Классы: {format_gear_classes(p.get('classes'))}\n"
            f"Изготовление: {'да' if p.get('craftable', False) else 'нет'}"
            f" · материалов: {len(p.get('materials', []))} · результат: {p.get('quantity', 1)} шт.\n"
            f"Изучение: {'один раз' if p.get('learning_scroll') is not None else 'не требуется'}\n"
            f"Источники предмета: {len(p.get('gear_mob_ids', []))}, свитка: {len(p.get('scroll_mob_ids', []))}\n\n"
            'Черновик сохранён. Каталог изменится после «Сохранить».'
        )
        for key, label in [('profile', '⚔️ Профиль предмета'), ('materials', '🧱 Материалы крафта'),
                           ('learning', '📜 Изучение'), ('sources', '👾 Источники')]:
            rows += button(draft, label, f'section:{key}')
        if section == 'preview':
            try:
                validate_draft_payload(p, complete=True)
            except DomainError as error:
                text += f'\n\nНе готово: {escape_html(str(error))}'
            rows += button(draft, '✅ Сохранить всё', 'save')
        else:
            rows += button(draft, '🔎 Проверить и сохранить', 'section:preview')
    if section == 'home' and 'gear_id' in p:
        rows += button(draft, '🗑 Удалить предмет', 'target_delete_prompt:gear')
        if p.get('craftable', False):
            rows += button(draft, '🗑 Удалить только рецепт', 'target_delete_prompt:recipe')
    if section != 'home':
        rows += button(draft, '🔙 К предмету', 'section:home')
    rows += button(draft, '⏸ Закрыть, сохранив черновик', 'pause')
    rows += button(draft, '🗑 Отменить черновик', 'cancel_prompt')
    await render(target, state, draft, text, rows, section=section)


async def start_gear_editor(
    target: types.Message | types.CallbackQuery, state: FSMContext, *, gear_id: int | None = None,
    payload: GearDraftPayload | None = None,
) -> None:
    if isinstance(target, types.CallbackQuery):
        message = get_callback_message(target)
        user_id = target.from_user.id
    else:
        user_id = get_message_user(target).id
        message = await target.answer('Открываю черновик снаряжения…')
    draft = await db.create_gear_draft(
        owner_user_id=user_id, chat_id=message.chat.id, message_id=message.message_id,
        gear_id=gear_id, payload=payload,
    )
    await state.clear()
    await show_section(message, state, draft, 'home' if gear_id is not None else 'profile')
    if isinstance(target, types.CallbackQuery):
        await target.answer()


async def picker(target: types.Message, state: FSMContext, draft: GearDraft, kind: str, page: int) -> None:
    data = await state.get_data()
    query = str(data.get('gear_draft_query') or '').casefold()
    if kind in ('material', 'scroll'):
        candidates = await db.execute_query('SELECT id, name, emoji, type FROM resources ORDER BY LOWER_UNICODE(name), id')
        candidates = [item for item in candidates if (item['type'] == 'scroll_recipe') == (kind == 'scroll')]
    else:
        candidates = await db.execute_query(
            'SELECT m.id, m.name, m.emoji, l.name AS location_name FROM mobs m '
            'LEFT JOIN locations l ON l.id=m.location_id ORDER BY LOWER_UNICODE(m.name), m.id'
        )
    candidates = [item for item in candidates if query in str(item['name']).casefold()]
    page = min(max(page, 0), max(0, (len(candidates) - 1) // PAGE_SIZE))
    rows: list[list[InlineKeyboardButton]] = []
    p = draft['payload']
    selected = p.get('gear_mob_ids', []) if kind == 'gear_mob' else p.get('scroll_mob_ids', [])
    for item in candidates[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        mark = ('☑️ ' if item['id'] in selected else '⬜ ') if kind.endswith('mob') else ''
        location = f" · {item['location_name']}" if item.get('location_name') else ''
        rows += button(draft, f"{mark}{item['emoji']} {item['name']}{location}", f'choose:{kind}:{item["id"]}')
    if page > 0:
        rows += button(draft, '◀️ Назад', f'pick:{kind}:{page - 1}')
    if (page + 1) * PAGE_SIZE < len(candidates):
        rows += button(draft, 'Вперёд ▶️', f'pick:{kind}:{page + 1}')
    rows += button(draft, '🔎 Поиск по названию', f'search:{kind}')
    if query:
        rows += button(draft, 'Сбросить поиск', f'clearsearch:{kind}')
    if kind == 'material':
        rows += button(draft, '➕ Создать недостающий материал', 'input:new_material_name')
    section = 'materials' if kind == 'material' else 'learning' if kind == 'scroll' else 'sources'
    rows += button(draft, '🔙 Вернуться', f'section:{section}')
    await state.update_data(gear_draft_picker=kind, gear_draft_page=page)
    await render(target, state, draft, f"Выбор · страница {page + 1} · найдено {len(candidates)}\n"
                 f"Поиск: {escape_html(query or 'все')}", rows, section=section)


async def prompt(target: types.Message, state: FSMContext, draft: GearDraft, field: str) -> None:
    labels = {
        'name': 'Введите название предмета.', 'emoji': 'Введите эмодзи предмета.',
        'level': 'Введите минимальный уровень (целое число от 1).',
        'note': 'Введите описание. «-» очищает описание.',
        'quantity': 'Сколько предметов получается за одно изготовление?',
        'new_material_name': 'Введите название нового материала. Он будет создан вместе с рецептом.',
        'new_material_emoji': 'Введите эмодзи нового материала.',
        'new_material_quantity': 'Введите количество нового материала на одно изготовление.',
    }
    text = labels.get(field, 'Введите количество материала (целое число от 1).')
    if field.startswith('search:'):
        text = 'Введите часть названия для поиска. «-» покажет все записи.'
    section = 'materials' if field.startswith(('new_material', 'material:')) or field == 'quantity' else 'profile'
    if field.startswith('search:'):
        kind = field.split(':')[1]
        back = f'pick:{kind}:0'
    else:
        back = f'section:{section}'
    await render(target, state, draft, text, button(draft, '🔙 Назад', back), section=section, input_action=field)


async def options(target: types.Message, state: FSMContext, draft: GearDraft, field: str) -> None:
    rows: list[list[InlineKeyboardButton]] = []
    if field == 'rarity':
        for index, key in enumerate(RARITY_KEYS):
            rows += button(draft, RARITY_LABELS[key], f'set:rarity:{index}')
    elif field == 'slot':
        for index, key in enumerate(GEAR_SLOTS):
            rows += button(draft, GEAR_SLOT_LABELS[key], f'set:slot:{index}')
    elif field == 'classes':
        selected = parse_gear_classes(draft['payload'].get('classes'))
        rows += button(draft, '☑️ Все классы' if not selected else 'Все классы', 'set:classes:all')
        for index, key in enumerate(GEAR_CLASS_ORDER):
            rows += button(draft, ('☑️ ' if not selected or key in selected else '⬜ ') + key, f'set:classes:{index}')
    else:
        raise DomainError('Неизвестное поле.')
    rows += button(draft, '🔙 К профилю', 'section:profile')
    await render(target, state, draft, 'Выберите значение.', rows, section='profile')


async def material_list(target: types.Message, state: FSMContext, draft: GearDraft, page: int) -> None:
    materials = draft['payload'].get('materials', [])
    page = min(max(page, 0), max(0, (len(materials) - 1) // PAGE_SIZE))
    rows: list[list[InlineKeyboardButton]] = []
    for index in range(page * PAGE_SIZE, min(len(materials), (page + 1) * PAGE_SIZE)):
        material = materials[index]
        resource_id = material.get('resource_id')
        resource = await db.get_resource_by_id(resource_id) if resource_id is not None else None
        name = resource['name'] if resource else material.get('name', 'Удалённый ресурс')
        rows += button(draft, f"✏️ {name} × {material['quantity']}", f'input:material:{index}')
        rows += button(draft, f"Удалить {name}", f'remove_material:{index}')
    if page > 0:
        rows += button(draft, '◀️ Назад', f'material_list:{page - 1}')
    if (page + 1) * PAGE_SIZE < len(materials):
        rows += button(draft, 'Вперёд ▶️', f'material_list:{page + 1}')
    rows += button(draft, '🔙 К материалам', 'section:materials')
    await render(target, state, draft, 'Материалы: измените количество или удалите строку.', rows, section='materials')


@gear_router.callback_query(F.data.startswith('gw:'))
async def gear_editor_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    try:
        draft, action = await active_draft(callback, state)
        await handle_action(callback, state, draft, action)
    except (DomainError, ValueError) as error:
        await callback.answer(str(error)[:180], show_alert=True)


async def handle_action(callback: types.CallbackQuery, state: FSMContext, draft: GearDraft, action: str) -> None:
    target = get_callback_message(callback)
    parts = action.split(':')
    key = parts[0]
    payload: dict[str, object] = dict(draft['payload'])
    if key == 'target_delete_confirm' and len(parts) == 3:
        gear_id = draft['payload'].get('gear_id')
        if gear_id is None:
            raise DomainError('Предмет ещё не опубликован.')
        await db.delete_gear_draft_target(draft['draft_id'], expected_revision=draft['revision'],
            owner_user_id=callback.from_user.id, chat_id=target.chat.id, message_id=target.message_id,
            delete_gear=parts[1] == 'gear', delete_scroll=parts[2] == 'scroll')
        await state.clear()
        if parts[1] == 'gear':
            await target.edit_text('Предмет удалён.', reply_markup=get_admin_main_keyboard())
            await callback.answer()
        else:
            await start_gear_editor(callback, state, gear_id=gear_id)
        return
    if key == 'save':
        result = await db.save_gear_draft(
            draft['draft_id'], expected_revision=draft['revision'], owner_user_id=callback.from_user.id,
            chat_id=target.chat.id, message_id=target.message_id,
        )
        await state.clear()
        # The committed result is durable before rendering the next editor.
        await start_gear_editor(callback, state, gear_id=result['gear_id'])
        return
    if key == 'pause':
        await revision(draft)
        await state.clear()
        await target.edit_text('Черновик сохранён. Продолжить можно из админки.', reply_markup=get_admin_main_keyboard())
        await callback.answer()
        return
    if key == 'cancel':
        await db.cancel_gear_draft(
            draft['draft_id'], expected_revision=draft['revision'], owner_user_id=callback.from_user.id,
            chat_id=target.chat.id, message_id=target.message_id,
        )
        await state.clear()
        await target.edit_text('Черновик отменён.', reply_markup=get_admin_main_keyboard())
        await callback.answer()
        return
    if key == 'craft':
        gear_id = draft['payload'].get('gear_id')
        if gear_id is not None and draft['payload'].get('craftable', False):
            gear = await db.get_gear_card(gear_id)
            if gear is not None and gear.get('recipe_id') is not None:
                draft = await revision(draft)
                await show_delete_confirmation(target, state, draft, 'recipe')
                await callback.answer()
                return
        enabled = not draft['payload'].get('craftable', False)
        payload['craftable'] = enabled
        if not enabled:
            payload.update(materials=[], learning_scroll=None, scroll_mob_ids=[])
    elif key == 'learning_toggle':
        if not draft['payload'].get('craftable', False):
            raise DomainError('Сначала включите изготовление.')
        payload['learning_scroll'] = {} if draft['payload'].get('learning_scroll') is None else None
        payload['scroll_mob_ids'] = []
    elif key == 'learning_auto':
        payload['learning_scroll'] = {}
        payload['scroll_mob_ids'] = []
    elif key == 'set' and len(parts) == 3:
        field, raw = parts[1:]
        if field == 'rarity' and raw.isdigit() and int(raw) < len(RARITY_KEYS):
            payload['rarity'] = RARITY_KEYS[int(raw)]
        elif field == 'slot' and raw.isdigit() and int(raw) < len(GEAR_SLOTS):
            payload['slot'] = GEAR_SLOTS[int(raw)]
        elif field == 'classes':
            selected = list(parse_gear_classes(draft['payload'].get('classes')))
            if raw == 'all':
                selected = []
            elif raw.isdigit() and int(raw) < len(GEAR_CLASS_ORDER):
                class_name = GEAR_CLASS_ORDER[int(raw)]
                selected = [item for item in selected if item != class_name] if class_name in selected else selected + [class_name]
            else:
                raise DomainError('Неизвестный класс.')
            payload['classes'] = ', '.join(selected)
        else:
            raise DomainError('Неизвестное значение.')
    elif key == 'remove_material' and len(parts) == 2:
        materials = list(draft['payload'].get('materials', []))
        index = int(parts[1])
        if not 0 <= index < len(materials):
            raise DomainError('Материал уже удалён.')
        del materials[index]
        payload['materials'] = materials
    elif key == 'choose' and len(parts) == 3:
        kind, item_id = parts[1], int(parts[2])
        if kind == 'scroll':
            resource = await db.get_resource_by_id(item_id)
            if resource is None or resource['type'] != 'scroll_recipe':
                raise DomainError('Выберите существующий свиток.')
            payload['learning_scroll'] = {'resource_id': item_id}
            payload['scroll_mob_ids'] = [int(row['mob_id']) for row in await db.execute_query(
                "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id", (item_id,))]
        elif kind == 'material':
            resource = await db.get_resource_by_id(item_id)
            if resource is None or resource['type'] == 'scroll_recipe':
                raise DomainError('Свиток изучения не является материалом.')
            materials = list(draft['payload'].get('materials', []))
            existing = next((index for index, item in enumerate(materials) if item.get('resource_id') == item_id), None)
            await state.update_data(gear_draft_pending_resource=item_id)
            draft = await revision(draft)
            await prompt(target, state, draft, f'material:{existing}' if existing is not None else 'material:new')
            await callback.answer()
            return
        elif kind in ('gear_mob', 'scroll_mob'):
            field_name = 'gear_mob_ids' if kind == 'gear_mob' else 'scroll_mob_ids'
            selected_ids = list(draft['payload'].get('gear_mob_ids', []) if kind == 'gear_mob' else draft['payload'].get('scroll_mob_ids', []))
            if item_id in selected_ids:
                selected_ids.remove(item_id)
            else:
                selected_ids.append(item_id)
            payload[field_name] = selected_ids
        else:
            raise DomainError('Неизвестный список.')
    draft = await revision(draft, payload)
    if key == 'target_delete_prompt' and len(parts) == 2:
        await show_delete_confirmation(target, state, draft, parts[1])
    elif key == 'preview_page' and len(parts) == 2:
        await show_full_preview(target, state, draft, int(parts[1]))
    elif key == 'section' and len(parts) == 2:
        await state.update_data(gear_draft_query='')
        await show_section(target, state, draft, parts[1])
    elif key == 'options' and len(parts) == 2:
        await options(target, state, draft, parts[1])
    elif key == 'set':
        await options(target, state, draft, parts[1])
    elif key == 'input':
        await prompt(target, state, draft, ':'.join(parts[1:]))
    elif key == 'search':
        await prompt(target, state, draft, action)
    elif key in ('pick', 'clearsearch'):
        if key == 'clearsearch':
            await state.update_data(gear_draft_query='')
        await picker(target, state, draft, parts[1], int(parts[2]) if len(parts) == 3 else 0)
    elif key == 'choose' and parts[1].endswith('mob'):
        data = await state.get_data()
        await picker(target, state, draft, parts[1], int(data.get('gear_draft_page', 0)))
    elif key == 'material_list':
        await material_list(target, state, draft, int(parts[1]))
    elif key == 'cancel_prompt':
        await render(target, state, draft, 'Удалить только этот черновик? Опубликованные данные сохранятся.',
                     button(draft, 'Да, отменить черновик', 'cancel') + button(draft, '🔙 Продолжить', 'section:home'))
    elif key == 'owners':
        gear_id = draft['payload'].get('gear_id')
        gear = await db.get_gear_card(gear_id) if gear_id is not None else None
        if gear is None or gear.get('recipe_id') is None:
            await show_section(target, state, draft, 'learning')
            await callback.answer('Сначала сохраните рецепт.', show_alert=True)
            return
        from admin_recipes import RecipeStates, recipe_show_owners
        await state.update_data(recipe_id=gear['recipe_id'], recipe_result_type='gear', recipe_page=1,
                                return_gear_draft_id=draft['draft_id'])
        await state.set_state(RecipeStates.view_recipe)
        await recipe_show_owners(callback, state)
    else:
        section = 'materials' if key in ('craft', 'remove_material') else 'learning' if key.startswith('learning') or key == 'choose' else 'home'
        await show_section(target, state, draft, section)
    await callback.answer()


@gear_router.message(GearEditorStates.input, F.text, ~F.text.startswith('/'))
async def gear_editor_input(message: types.Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft_id = data.get('gear_draft_id')
    if not isinstance(draft_id, str):
        return
    draft = await db.get_gear_draft(draft_id, owner_user_id=get_message_user(message).id, chat_id=message.chat.id)
    if draft is None or draft['status'] != 'editing' or draft['revision'] != data.get('gear_draft_revision'):
        await state.clear()
        await message.answer('Черновик изменился. Откройте его заново из админки.')
        return
    if message.reply_to_message is not None and message.reply_to_message.message_id != draft['message_id']:
        await message.answer('Ответ относится к старому сообщению. Используйте текущее окно редактора.')
        return
    field = str(data.get('gear_draft_input') or '')
    value = get_message_text(message).strip()
    payload: dict[str, object] = dict(draft['payload'])
    try:
        if field.startswith('search:'):
            if len(value) > 256:
                raise DomainError('Поисковый запрос должен быть не длиннее 256 символов.')
            await state.update_data(gear_draft_query='' if value == '-' else value)
            draft = await revision(draft)
            await picker(message, state, draft, field.split(':')[1], 0)
            return
        if field == 'new_material_name':
            if not value or len(value) > MAX_RESOURCE_NAME_LENGTH:
                raise DomainError(f'Название должно содержать от 1 до {MAX_RESOURCE_NAME_LENGTH} символов.')
            await state.update_data(gear_draft_pending_name=value)
            draft = await revision(draft)
            await prompt(message, state, draft, 'new_material_emoji')
            return
        if field == 'new_material_emoji':
            if not is_valid_emoji(value):
                raise DomainError('Введите эмодзи материала.')
            await state.update_data(gear_draft_pending_emoji=value)
            draft = await revision(draft)
            await prompt(message, state, draft, 'new_material_quantity')
            return
        if field in ('name', 'emoji', 'note'):
            payload[field] = '' if value == '-' and field == 'note' else value
        elif field in ('level', 'quantity'):
            payload[field] = int(value)
        elif field.startswith('material:') or field == 'new_material_quantity':
            quantity = int(value)
            if quantity < 1:
                raise DomainError('Количество должно быть не меньше 1.')
            materials = list(draft['payload'].get('materials', []))
            if field == 'new_material_quantity':
                material: MaterialInput = {'name': str(data['gear_draft_pending_name']),
                                           'emoji': str(data['gear_draft_pending_emoji']), 'quantity': quantity}
                materials.append(material)
            elif field == 'material:new':
                materials.append({'resource_id': int(data['gear_draft_pending_resource']), 'quantity': quantity})
            else:
                index = int(field.split(':')[1])
                if not 0 <= index < len(materials):
                    raise DomainError('Материал уже удалён.')
                materials[index] = {**materials[index], 'quantity': quantity}
            payload['materials'] = materials
        else:
            raise DomainError('Поле ввода устарело. Вернитесь к предмету.')
        draft = await revision(draft, payload)
    except (DomainError, ValueError) as error:
        await message.answer(f'Не сохранено: {escape_html(str(error))}', parse_mode='HTML')
        return
    await show_section(message, state, draft, 'materials' if field.startswith(('material:', 'new_material')) or field == 'quantity' else 'profile')


@gear_router.callback_query(F.data == 'gear_drafts')
@gear_router.callback_query(GearEditorStates.drafts, F.data.startswith('gd:'))
async def list_drafts(callback: types.CallbackQuery, state: FSMContext) -> None:
    message = get_callback_message(callback)
    raw = get_callback_data(callback)
    page = 0
    if raw.startswith('gd:'):
        data = await state.get_data()
        if data.get('gear_resume_message_id') != message.message_id:
            await callback.answer('Этот список устарел.', show_alert=True)
            return
        page = max(0, int(raw.split(':')[1]))
    drafts = await db.list_gear_drafts(owner_user_id=callback.from_user.id, chat_id=message.chat.id)
    page = min(page, max(0, (len(drafts) - 1) // PAGE_SIZE))
    rows = [[InlineKeyboardButton(
        text=f"📝 {draft['payload'].get('name', 'Новый предмет')}"[:100],
        callback_data=f"gr:{draft['draft_id']}:{draft['revision']}",
    )] for draft in drafts[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    if page:
        rows.append([InlineKeyboardButton(text='◀️ Назад', callback_data=f'gd:{page - 1}')])
    if (page + 1) * PAGE_SIZE < len(drafts):
        rows.append([InlineKeyboardButton(text='Вперёд ▶️', callback_data=f'gd:{page + 1}')])
    rows.append([InlineKeyboardButton(text='🔙 В админку', callback_data='admin_cancel_edit')])
    await state.clear()
    await state.update_data(gear_resume_message_id=message.message_id)
    await state.set_state(GearEditorStates.drafts)
    await message.edit_text('Выберите черновик.' if drafts else 'Нет незавершённых черновиков.',
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@gear_router.callback_query(GearEditorStates.drafts, F.data.startswith('gr:'))
async def resume_draft(callback: types.CallbackQuery, state: FSMContext) -> None:
    message = get_callback_message(callback)
    data = await state.get_data()
    try:
        _, draft_id, revision_text = get_callback_data(callback).split(':')
        draft = await db.get_gear_draft(draft_id, owner_user_id=callback.from_user.id, chat_id=message.chat.id)
        if (draft is None or draft['status'] != 'editing' or draft['revision'] != int(revision_text)
                or data.get('gear_resume_message_id') != message.message_id):
            raise DraftConflictError('Этот список устарел. Откройте черновики заново.')
        draft = await db.bind_gear_draft_message(
            draft_id, expected_revision=draft['revision'], owner_user_id=callback.from_user.id,
            chat_id=message.chat.id, old_message_id=draft['message_id'], new_message_id=message.message_id,
        )
        await show_section(message, state, draft)
        await callback.answer()
    except (DomainError, ValueError) as error:
        await callback.answer(str(error)[:180], show_alert=True)


async def show_full_preview(target: types.Message, state: FSMContext, draft: GearDraft, page: int) -> None:
    p = draft['payload']
    text = f"<b>Проверка перед сохранением</b>\n{editor_caption(draft)}\n"
    text += f"Редкость: {RARITY_LABELS.get(p.get('rarity', ''), 'не выбрана')}\n"
    text += f"Слот: {GEAR_SLOT_LABELS.get(p.get('slot', ''), 'не выбран')}\nУровень: {p.get('level', 1)}\n"
    text += f"Классы: {format_gear_classes(p.get('classes'))}\nОписание: {escape_html(p.get('note', '') or '—')}\n\n"
    text += f"<b>Изготовление: {'да' if p.get('craftable', False) else 'нет'}</b>\nРезультат: {p.get('quantity', 1)} шт.\n"
    for material in p.get('materials', []):
        resource_id = material.get('resource_id')
        resource = await db.get_resource_by_id(resource_id) if resource_id is not None else None
        name = resource['name'] if resource else material.get('name', 'ресурс удалён')
        text += f"• {escape_html(name)} × {material['quantity']}\n"
    scroll = p.get('learning_scroll')
    if scroll is None:
        text += '\n<b>Изучение свитка не требуется.</b>\n'
    else:
        scroll_id = scroll.get('resource_id')
        resource = await db.get_resource_by_id(scroll_id) if scroll_id is not None else None
        name = resource['name'] if resource else scroll.get('name', f"Рецепт ({p.get('name', 'предмет')})")
        text += f"\n<b>Изучается один раз:</b> {escape_html(name)}"
        text += f" · ресурс ID {scroll_id}\n" if scroll_id is not None else ' · новый свиток\n'
    mobs = {int(item['id']): str(item['name']) for item in await db.execute_query('SELECT id,name FROM mobs')}
    for label, ids in [('Выпадение готового предмета', p.get('gear_mob_ids', [])), ('Выпадение свитка', p.get('scroll_mob_ids', []))]:
        text += f"\n<b>{label}:</b>\n"
        text += '\n'.join(f"• {escape_html(mobs.get(mob_id, 'моб удалён'))} · ID {mob_id}" for mob_id in ids) or 'Нет источников.'
        text += '\n'
    try:
        validate_draft_payload(p, complete=True)
    except DomainError as error:
        text += f"\n<b>Не готово:</b> {escape_html(str(error))}"
    chunks = split_html(text, limit=2800)
    page = min(max(page, 0), len(chunks) - 1)
    rows: list[list[InlineKeyboardButton]] = []
    if page > 0:
        rows += button(draft, '◀️ Предыдущая страница', f'preview_page:{page - 1}')
    if page + 1 < len(chunks):
        rows += button(draft, 'Следующая страница ▶️', f'preview_page:{page + 1}')
    rows += button(draft, '✅ Сохранить всё', 'save')
    rows += button(draft, '🔙 К предмету', 'section:home')
    await render(target, state, draft, f"Страница {page + 1} из {len(chunks)}\n\n{chunks[page].as_html()}", rows, section='preview')


async def return_to_gear_editor(target: types.Message | types.CallbackQuery, state: FSMContext, draft_id: str) -> bool:
    if isinstance(target, types.CallbackQuery):
        message = get_callback_message(target)
        user_id = target.from_user.id
    else:
        user_id = get_message_user(target).id
        message = await target.answer('Возвращаюсь к черновику…')
    draft = await db.get_gear_draft(draft_id, owner_user_id=user_id, chat_id=message.chat.id)
    if draft is None or draft['status'] != 'editing':
        return False
    draft = await db.bind_gear_draft_message(draft_id, expected_revision=draft['revision'], owner_user_id=user_id,
                                            chat_id=message.chat.id, old_message_id=draft['message_id'], new_message_id=message.message_id)
    await state.update_data(return_gear_draft_id=None)
    await show_section(message, state, draft, 'learning')
    return True


async def show_delete_confirmation(target: types.Message, state: FSMContext, draft: GearDraft, kind: str) -> None:
    gear_id = draft['payload'].get('gear_id')
    if gear_id is None or kind not in ('gear', 'recipe'):
        raise DomainError('Нет опубликованного предмета для удаления.')
    gear = await db.get_gear_card(gear_id)
    if gear is None:
        raise DomainError('Предмет уже удалён.')
    recipe_id = gear.get('recipe_id')
    recipe = await db.get_recipe_details(recipe_id) if recipe_id is not None else None
    if kind == 'recipe' and recipe is None:
        raise DomainError('У предмета нет опубликованного рецепта.')
    owners = await db.get_recipe_owner_entries(recipe_id) if recipe_id is not None else []
    text = f"<b>Удалить {'предмет и его рецепт' if kind == 'gear' else 'рецепт, сохранив предмет'}?</b>\n"
    text += f"Предмет: {escape_html(gear['name'])} · ID {gear_id}\n"
    if recipe is not None:
        text += f"Будут удалены формула ID {recipe_id}, {len(recipe['ingredients'])} материалов и {len(owners)} отметок изучения.\n"
    text += 'Текущий черновик будет закрыт. Это действие нельзя отменить.\n'
    rows = button(draft, 'Удалить; свиток и его дроп сохранить', f'target_delete_confirm:{kind}:keep')
    if draft['payload'].get('learning_scroll') is not None:
        rows += button(draft, 'Удалить также свиток и его дроп', f'target_delete_confirm:{kind}:scroll')
    rows += button(draft, '🔙 Отмена', 'section:home')
    await render(target, state, draft, text, rows)
