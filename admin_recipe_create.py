"""Searchable result selection and unpublished alchemy recipe composition."""

import secrets
from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_sessions import present_admin_text, register_protected_callbacks, validate_admin_input
from database import db
from recipe_domain import MaterialInput, positive_integer, validate_craft_location
from telegram_helpers import get_callback_data, get_message_text
from utils import escape_html

creation_router = Router()
register_protected_callbacks(('rc:',))
PAGE_SIZE = 8


class RecipeCreationStates(StatesGroup):
    choosing = State()
    search = State()
    output_quantity = State()
    material_quantity = State()
    preview = State()
    craft_location = State()


def row(label: str, data: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=label[:100], callback_data=f'rc:{data}')]


async def screen(target: types.Message | types.CallbackQuery, state: FSMContext, text: str,
                 rows: list[list[InlineKeyboardButton]]) -> None:
    data = await state.get_data()
    context = {'recipe_create_session': str(data['recipe_create_session'])}
    if isinstance(data.get('recipe_create_result_id'), int):
        context['recipe_create_result_id'] = str(data['recipe_create_result_id'])
        # IDs keep their actual FSM type for the shared screen comparison.
        await present_admin_text(target, state, text, InlineKeyboardMarkup(inline_keyboard=rows),
                                 context={'recipe_create_session': str(data['recipe_create_session']),
                                          'recipe_create_result_id': data['recipe_create_result_id']}, parse_mode='HTML')
    else:
        await present_admin_text(target, state, text, InlineKeyboardMarkup(inline_keyboard=rows),
                                 context=context, parse_mode='HTML')


async def begin_recipe_creation(callback: types.CallbackQuery, state: FSMContext, result_type: str) -> None:
    if result_type not in ('gear', 'resource'):
        await callback.answer('Неизвестный тип рецепта.', show_alert=True)
        return
    await state.clear()
    await state.update_data(recipe_create_session=secrets.token_hex(8), recipe_create_type=result_type,
                            recipe_create_query='', recipe_create_materials=[], recipe_create_quantity=1, recipe_create_location='')
    await choose_page(callback, state, 0)


async def choose_page(target: types.Message | types.CallbackQuery, state: FSMContext, page: int) -> None:
    data = await state.get_data()
    kind = data['recipe_create_type']
    selecting_material = isinstance(data.get('recipe_create_result_id'), int)
    query = str(data.get('recipe_create_query') or '').casefold()
    if selecting_material:
        rows = await db.execute_query("SELECT id,name,emoji FROM resources WHERE type!='scroll_recipe' AND id!=? ORDER BY LOWER_UNICODE(name),id",
                                      (data['recipe_create_result_id'],))
    elif kind == 'gear':
        rows = await db.get_all_gear_simple()
    else:
        rows = await db.execute_query("SELECT id,name,emoji FROM resources WHERE type='alchemy' AND id NOT IN "
                                      "(SELECT result_id FROM recipes WHERE result_type='resource') ORDER BY LOWER_UNICODE(name),id")
    rows = [item for item in rows if query in str(item['name']).casefold()]
    page = min(max(page, 0), max(0, (len(rows) - 1) // PAGE_SIZE))
    keyboard = [row(f"{item['emoji']} {item['name']}", f"select:{item['id']}") for item in rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]]
    if page:
        keyboard.append(row('◀️ Назад', f'page:{page - 1}'))
    if (page + 1) * PAGE_SIZE < len(rows):
        keyboard.append(row('Вперёд ▶️', f'page:{page + 1}'))
    keyboard.append(row('🔎 Найти по названию', 'search'))
    if query:
        keyboard.append(row('Сбросить поиск', 'clearsearch'))
    if selecting_material:
        keyboard.append(row(f"🔎 Проверить рецепт · материалов {len(data['recipe_create_materials'])}", 'preview'))
    elif kind == 'gear':
        keyboard.append(row('➕ Новый предмет и рецепт', 'newgear'))
    keyboard.append([InlineKeyboardButton(text='🔙 В админку', callback_data='admin_cancel_edit')])
    label = 'расходуемый материал' if selecting_material else 'снаряжение' if kind == 'gear' else 'результат алхимии'
    await state.update_data(recipe_create_page=page)
    await state.set_state(RecipeCreationStates.choosing)
    await screen(target, state, f'Выберите {label}. Страница {page + 1}; найдено {len(rows)}.\nПоиск: {escape_html(query or "все")}', keyboard)


async def show_preview(target: types.Message | types.CallbackQuery, state: FSMContext, page: int = 0) -> None:
    data = await state.get_data()
    resource = await db.get_resource_by_id(data['recipe_create_result_id'])
    materials = decode_materials(data['recipe_create_materials'])
    page = min(max(page, 0), max(0, (len(materials) - 1) // PAGE_SIZE))
    text = f"<b>Рецепт алхимии: {escape_html(resource['name'] if resource else 'ресурс удалён')}</b>\n"
    text += f"Результат: {data['recipe_create_quantity']} шт.\nМесто изготовления: {escape_html(data.get('recipe_create_location') or 'не указано')}\nМатериалы:\n"
    keyboard = []
    for index in range(page * PAGE_SIZE, min(len(materials), (page + 1) * PAGE_SIZE)):
        material = materials[index]
        ingredient = await db.get_resource_by_id(material['resource_id'])
        name = ingredient['name'] if ingredient else 'ресурс удалён'
        text += f"• {escape_html(name)} × {material['quantity']}\n"
        keyboard.append(row(f"Удалить {name}", f'remove:{index}'))
    if page:
        keyboard.append(row('◀️ Предыдущие материалы', f'preview:{page - 1}'))
    if (page + 1) * PAGE_SIZE < len(materials):
        keyboard.append(row('Следующие материалы ▶️', f'preview:{page + 1}'))
    if materials:
        keyboard.append(row('✅ Сохранить рецепт', 'save'))
    else:
        text += 'Добавьте хотя бы один материал.'
    keyboard += [row('➕ Добавить / изменить материал', 'page:0'), row('Изменить количество результата', 'output'), row('Место изготовления', 'location')]
    keyboard.append([InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_edit')])
    await state.set_state(RecipeCreationStates.preview)
    await screen(target, state, text + '\n\nДо сохранения каталог не изменяется.', keyboard)


def decode_materials(value: object) -> list[MaterialInput]:
    if not isinstance(value, list):
        raise ValueError('Некорректные материалы черновика.')
    result: list[MaterialInput] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('Некорректный материал.')
        result.append({'resource_id': positive_integer(item.get('resource_id'), 'Материал'),
                       'quantity': positive_integer(item.get('quantity'), 'Количество')})
    return result


@creation_router.callback_query(F.data.startswith('rc:'))
async def create_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if 'recipe_create_session' not in data:
        await callback.answer('Откройте создание рецепта заново.', show_alert=True)
        return
    parts = get_callback_data(callback).split(':')
    action = parts[1]
    if action == 'newgear':
        from admin_gear import start_gear_editor
        await start_gear_editor(callback, state, payload={'craftable': True, 'emoji': '⚔️', 'level': 1})
        return
    if action in ('page', 'clearsearch'):
        if action == 'clearsearch':
            await state.update_data(recipe_create_query='')
        await choose_page(callback, state, int(parts[2]) if action == 'page' else 0)
    elif action == 'search':
        await state.set_state(RecipeCreationStates.search)
        await screen(callback, state, 'Введите часть названия. «-» покажет всё.', [row('🔙 Назад', 'page:0')])
    elif action == 'select':
        selected_id = int(parts[2])
        if isinstance(data.get('recipe_create_result_id'), int):
            resource = await db.get_resource_by_id(selected_id)
            if resource is None or resource['type'] == 'scroll_recipe' or selected_id == data['recipe_create_result_id']:
                await callback.answer('Этот ресурс нельзя добавить в материалы.', show_alert=True)
                return
            await state.update_data(recipe_create_material_id=selected_id)
            await state.set_state(RecipeCreationStates.material_quantity)
            await screen(callback, state, f"Количество материала {escape_html(resource['name'])}:", [row('🔙 Назад', 'page:0')])
        elif data['recipe_create_type'] == 'gear':
            from admin_gear import start_gear_editor
            await start_gear_editor(callback, state, gear_id=selected_id)
            return
        else:
            resource = await db.get_resource_by_id(selected_id)
            if resource is None or resource['type'] != 'alchemy':
                await callback.answer('Выберите результат алхимии.', show_alert=True)
                return
            await state.update_data(recipe_create_result_id=selected_id, recipe_create_query='')
            await state.set_state(RecipeCreationStates.output_quantity)
            await screen(callback, state, f"Сколько единиц {escape_html(resource['name'])} получается за одно изготовление?", [row('🔙 К материалам', 'page:0')])
    elif action == 'output':
        await state.set_state(RecipeCreationStates.output_quantity)
        await screen(callback, state, 'Количество результата за одно изготовление:', [row('🔙 К рецепту', 'preview')])
    elif action == 'location':
        await state.set_state(RecipeCreationStates.craft_location)
        await screen(callback, state, 'Введите место изготовления. «-» означает, что место не указано.', [row('🔙 Назад', 'preview')])
    elif action == 'preview':
        await show_preview(callback, state, int(parts[2]) if len(parts) == 3 else 0)
    elif action == 'remove':
        materials = decode_materials(data['recipe_create_materials'])
        index = int(parts[2])
        if 0 <= index < len(materials):
            del materials[index]
        await state.update_data(recipe_create_materials=materials)
        await show_preview(callback, state)
    elif action == 'save':
        try:
            recipe_id = await db.save_resource_recipe(data['recipe_create_result_id'], data['recipe_create_quantity'],
                                                      decode_materials(data['recipe_create_materials']), craft_location=str(data.get('recipe_create_location') or ''))
        except ValueError as error:
            await callback.answer(str(error)[:180], show_alert=True)
            return
        from admin_recipes import RecipeStates, show_recipe
        await state.clear()
        await state.update_data(recipe_id=recipe_id, recipe_result_type='resource', recipe_page=1)
        await state.set_state(RecipeStates.view_recipe)
        await show_recipe(callback, await db.get_recipe_details(recipe_id), state)
    await callback.answer()


@creation_router.message(RecipeCreationStates.search, F.text, ~F.text.startswith('/'))
async def search_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    query = get_message_text(message).strip()
    if len(query) > 256:
        await message.answer('Поисковый запрос должен быть не длиннее 256 символов.')
        return
    await state.update_data(recipe_create_query='' if query == '-' else query)
    await choose_page(message, state, 0)


@creation_router.message(RecipeCreationStates.output_quantity, F.text, ~F.text.startswith('/'))
@creation_router.message(RecipeCreationStates.material_quantity, F.text, ~F.text.startswith('/'))
async def quantity_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    try:
        quantity = positive_integer(int(get_message_text(message).strip()), 'Количество')
    except ValueError:
        await message.answer('Введите положительное целое число.')
        return
    data = await state.get_data()
    if await state.get_state() == RecipeCreationStates.output_quantity.state:
        await state.update_data(recipe_create_quantity=quantity)
        await choose_page(message, state, 0)
    else:
        materials = decode_materials(data['recipe_create_materials'])
        resource_id = data['recipe_create_material_id']
        materials = [item for item in materials if item['resource_id'] != resource_id]
        materials.append({'resource_id': resource_id, 'quantity': quantity})
        await state.update_data(recipe_create_materials=materials)
        await show_preview(message, state)


@creation_router.message(RecipeCreationStates.craft_location, F.text, ~F.text.startswith('/'))
async def location_input(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    value = get_message_text(message).strip()
    try:
        value = validate_craft_location('' if value == '-' else value)
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.update_data(recipe_create_location=value)
    await show_preview(message, state)
