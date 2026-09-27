from catalog_types import RecipeDetailsRow, RecipeOwnerEntry, ResourceRow
from storage.types import sql_int, sql_text
from admin_recipe_create import create_recipe_creation_router, begin_recipe_creation
from collections.abc import Sequence
from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from admin_contracts import EntityRow
from admin_commands import admin_transition
from admin_utils import ADMIN_ITEMS_PER_PAGE, get_admin_main_keyboard, prepare_delete_confirmation, consume_delete_confirmation
from runtime_scope import database_for
from database import db
from telegram_helpers import get_callback_data, get_callback_message, get_message_text
from utils import clean_username, escape_html

from admin_sessions import present_admin_text, present_admin_rich, register_protected_callbacks, validate_admin_input
register_protected_callbacks(('recipe_',))

async def present_recipe(target: types.Message | types.CallbackQuery, state: FSMContext,
                      text: str, reply_markup: InlineKeyboardMarkup | None = None,
                      parse_mode: str | None = None) -> types.Message:
    data = await state.get_data()
    context = {key: value for key in ('recipe_id', 'new_recipe_result_id', 'temp_resource_id', 'edit_resource_id', 'selected_owner_id') if isinstance((value := data.get(key)), (str, int))}
    return await present_admin_text(target, state, text, reply_markup, context=context, parse_mode=parse_mode)


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
    output_quantity = State()
    craft_location = State()

async def get_recipe_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Снаряжение", callback_data="recipe_type_gear")],
        [InlineKeyboardButton(text="⚗️ Алхимия", callback_data="recipe_type_resource")],
        [InlineKeyboardButton(text="🔙 Назад в админку", callback_data="admin_cancel_edit")]
    ])


def get_recipe_type_title(result_type: str) -> str:
    return "Снаряжение" if result_type == "gear" else "Алхимия"

async def manage_recipes_type(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await present_recipe(callback, state, "Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)

async def get_recipe_list_keyboard(result_type: str, page: int) -> InlineKeyboardMarkup:
    offset = (page-1)*ADMIN_ITEMS_PER_PAGE
    recipes = await database_for(db).get_all_recipes(result_type, offset, ADMIN_ITEMS_PER_PAGE+1)
    has_next = len(recipes) > ADMIN_ITEMS_PER_PAGE
    recipes = recipes[:ADMIN_ITEMS_PER_PAGE]
    keyboard = []
    for recipe in recipes:
        recipe_id = sql_int(recipe['id'])
        text = (
            f"{sql_text(recipe['result_emoji'])} {sql_text(recipe['result_name'])} "
            f"(ID рец.{recipe_id}) | ингр:{sql_int(recipe['ingredient_count'])}"
        )
        if result_type == 'gear':
            text += f" влад:{sql_int(recipe['owner_count'])}"
        keyboard.append([InlineKeyboardButton(text=text, callback_data=f"recipe_view_{recipe_id}")])
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

async def recipe_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    result_type = get_callback_data(callback).split("_")[2]
    await state.update_data(recipe_result_type=result_type, recipe_page=1)
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await present_recipe(callback, state, f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

async def recipe_list_page(callback: types.CallbackQuery, state: FSMContext) -> None:
    parts = get_callback_data(callback).split("_")
    result_type = parts[2]
    page = int(parts[3])
    await state.update_data(recipe_result_type=result_type, recipe_page=page)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await present_recipe(callback, state, f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)

async def recipe_back_to_type(callback: types.CallbackQuery, state: FSMContext) -> None:
    await present_recipe(callback, state, "Выберите тип результата рецепта:", reply_markup=await get_recipe_type_keyboard())
    await state.set_state(RecipeStates.list_type)

def admin_recipe_owner_label(owner: RecipeOwnerEntry) -> str:
    username = owner.get('player_username')
    if username:
        return f"@{clean_username(username)}"
    user_id = owner.get('user_id')
    return f"Игрок {user_id}" if user_id is not None else "Неизвестный владелец"


def admin_recipe_owner_labels(recipe: RecipeDetailsRow) -> list[str]:
    if 'owner_entries' in recipe:
        return [admin_recipe_owner_label(owner) for owner in recipe['owner_entries']]
    return [f"@{clean_username(owner)}" for owner in recipe.get('owners', [])]


async def show_recipe(target: types.Message | types.CallbackQuery, recipe: RecipeDetailsRow | None, state: FSMContext) -> None:
    if recipe is None:
        await state.clear()
        message = get_callback_message(target) if isinstance(target, types.CallbackQuery) else target
        await present_recipe(message, state, "Рецепт больше не существует.", reply_markup=get_admin_main_keyboard())
        return
    if recipe['result_type'] == 'gear':
        from admin_gear import start_gear_editor, return_to_gear_editor
        data = await state.get_data()
        draft_id = data.get('return_gear_draft_id')
        if isinstance(draft_id, str) and await return_to_gear_editor(target, state, draft_id):
            return
        await start_gear_editor(target, state, gear_id=recipe['result_id'])
        return
    if recipe['result_type'] == 'gear':
        gear = await database_for(db).get_gear_by_id(recipe['result_id'])
        result_info = f"{escape_html(gear['emoji'])} {escape_html(gear['name'])}" if gear else f"ID {recipe['result_id']}"
    else:
        res = await database_for(db).get_resource_by_id(recipe['result_id'])
        result_info = f"{escape_html(res['emoji'])} {escape_html(res['name'])}" if res else f"ID {recipe['result_id']}"
    text = f"📜 Рецепт ID {recipe['id']}\n🎁 Результат: {result_info} (количество: {recipe['quantity']})\n\n"
    text += f"Место изготовления: {escape_html(recipe.get('craft_location') or 'не указано')}\n\n"
    text += "<b>Ингредиенты:</b>\n"
    for ing in recipe['ingredients']:
        text += f"  {escape_html(ing['emoji'])} {escape_html(ing['name'])} — {ing['quantity']} шт.\n"
    if not recipe['ingredients']:
        text += "<i>Нет ингредиентов</i>\n"

    if recipe['result_type'] == 'gear':
        text += "\n👥 <b>Владельцы:</b>\n"
        owner_labels = admin_recipe_owner_labels(recipe)
        for label in owner_labels:
            text += f"  {escape_html(label)}\n"
        if not owner_labels:
            text += "<i>Нет владельцев</i>\n"

    keyboard = []
    if recipe['can_learn']:
        keyboard.append([InlineKeyboardButton(text="👤 Добавить владельца", callback_data="recipe_add_owner")])
        keyboard.append([InlineKeyboardButton(text="👥 Управлять владельцами", callback_data="recipe_manage_owners")])
    keyboard.append([InlineKeyboardButton(text="Количество результата", callback_data="recipe_output_quantity")])
    keyboard.append([InlineKeyboardButton(text="Место изготовления", callback_data="recipe_craft_location")])
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
            f"Место изготовления: {escape_html(recipe.get('craft_location') or 'не указано')}<br>"
            "<table><tbody><tr><th>Ингредиент</th><th>Количество</th></tr>"
            f"{ingredient_rows}</tbody></table>"
        )
        if recipe['result_type'] == 'gear':
            owners = "<br>".join(
                escape_html(label) for label in admin_recipe_owner_labels(recipe)
            ) or "Нет владельцев"
            rich_html += f"<details><summary>👥 Владельцы</summary>{owners}</details>"
        await present_admin_rich(target, state, rich_html, text, InlineKeyboardMarkup(inline_keyboard=keyboard),
                                 context={'recipe_id': int(recipe['id'])})
    else:
        await present_recipe(target, state, text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.view_recipe)

async def recipe_view(callback: types.CallbackQuery, state: FSMContext) -> None:
    recipe_id = int(get_callback_data(callback).split("_")[2])
    recipe = await database_for(db).get_recipe_details(recipe_id)
    if not recipe:
        await present_recipe(callback, state, "Рецепт не найден.")
        return
    await state.update_data(recipe_id=recipe_id, recipe_result_type=recipe['result_type'])
    await show_recipe(callback, recipe, state)
    await callback.answer()

async def recipe_back_to_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    result_type = data.get('recipe_result_type', 'gear')
    page = data.get('recipe_page', 1)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await present_recipe(callback, state, f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

async def recipe_add_choose_item(callback: types.CallbackQuery, state: FSMContext) -> None:
    result_type = get_callback_data(callback).split("_")[2]
    await begin_recipe_creation(callback, state, result_type)

async def recipe_create(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer('Этот способ создания устарел. Откройте новый редактор рецепта.', show_alert=True)

async def recipe_add_ingredient_select(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    resources = await get_available_ingredients(data['recipe_id'])
    if not resources:
        await callback.answer("Все доступные ресурсы уже добавлены в рецепт.", show_alert=True)
        return
    await state.update_data(ingredient_resources=resources, ingredient_page=1)
    await show_ingredient_page(callback, resources, 1, state)

async def get_available_ingredients(recipe_id: int) -> list[ResourceRow]:
    recipe = await database_for(db).get_recipe_details(recipe_id)
    if not recipe:
        return []
    existing_ids = {item['resource_id'] for item in recipe['ingredients']}
    if recipe['result_type'] == 'resource':
        existing_ids.add(recipe['result_id'])
    rows = await database_for(db).get_recipe_resource_choices('material')
    return [item for item in rows if item['id'] not in existing_ids]


async def show_ingredient_page(target: types.Message | types.CallbackQuery, resources: Sequence[EntityRow], page: int, state: FSMContext) -> None:
    per_page = ADMIN_ITEMS_PER_PAGE
    last_page = max(1, (len(resources) + per_page - 1) // per_page)
    page = min(max(1, page), last_page)
    await state.update_data(ingredient_resources=resources, ingredient_page=page)
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
        await present_recipe(target, state, "Выберите ресурс для добавления в ингредиенты:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    else:
        await present_recipe(target, state, "Выберите ресурс:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.add_ingredient)

async def recipe_ing_page(callback: types.CallbackQuery, state: FSMContext) -> None:
    page = int(get_callback_data(callback).split("_")[3])
    data = await state.get_data()
    resources = data.get('ingredient_resources')
    if not resources:
        await callback.answer("Ошибка", show_alert=True)
        return
    await show_ingredient_page(callback, resources, page, state)

async def recipe_ing_quantity(callback: types.CallbackQuery, state: FSMContext) -> None:
    parts = get_callback_data(callback).split("_")
    resource_id = int(parts[3])
    page = int(parts[4]) if len(parts)>4 else 1
    await state.update_data(temp_resource_id=resource_id, ingredient_return_page=page, edit_action='add')
    await present_recipe(callback, state, "Введите количество (целое число):")
    await state.set_state(RecipeStates.edit_ingredient_quantity)

async def recipe_ing_save_quantity(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    try:
        qty = int(get_message_text(message).strip())
        if qty <= 0:
            raise ValueError
    except (TypeError, ValueError):
        await present_recipe(message, state, "Введите положительное целое число.")
        return
    data = await state.get_data()
    recipe_id = data['recipe_id']
    action = data.get('edit_action')
    if action == 'add':
        resource_id = data['temp_resource_id']
        try:
            async with admin_transition(state):
                await database_for(db).add_ingredient(recipe_id, resource_id, qty)
                await state.update_data(edit_action=None, temp_resource_id=None)
                await state.set_state(RecipeStates.add_ingredient)
        except ValueError as error:
            await present_recipe(message, state, f"❌ {escape_html(error)}", parse_mode="HTML")
        else:
            await state.update_data(edit_action=None, temp_resource_id=None)
            await state.set_state(RecipeStates.add_ingredient)
            await present_recipe(message, state, "✅ Ингредиент добавлен. Выберите следующий или нажмите 'Готово'.")
        resources = await get_available_ingredients(recipe_id)
        await show_ingredient_page(message, resources, data.get('ingredient_return_page', 1), state)
        return
    elif action == 'change':
        resource_id = data['edit_resource_id']
        try:
            async with admin_transition(state):
                await database_for(db).update_ingredient(recipe_id, resource_id, qty)
                await state.update_data(edit_action=None)
                await state.set_state(RecipeStates.view_recipe)
        except ValueError as error:
            await present_recipe(message, state, f"❌ {escape_html(error)}", parse_mode="HTML")
            return
        await state.update_data(edit_action=None)
        await state.set_state(RecipeStates.view_recipe)
        await present_recipe(message, state, "✅ Количество обновлено.")
    else:
        await present_recipe(message, state, "Ошибка.")
        return
    recipe = await database_for(db).get_recipe_details(recipe_id)
    await show_recipe(message, recipe, state)

async def recipe_show_current(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await database_for(db).get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)
    await callback.answer()

async def recipe_add_owner_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipe_id = data.get('recipe_id')
    recipe = await database_for(db).get_recipe_details(recipe_id) if isinstance(recipe_id, int) else None
    if recipe is None or not recipe['can_learn']:
        await callback.answer("Сначала сохраните рецепт с требованием изучения свитка.", show_alert=True)
        return
    await present_recipe(callback, state, "Введите username изучившего рецепт игрока (без @). Это ручная запись.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="recipe_back_to_view")]]))
    await state.set_state(RecipeStates.add_owner)

async def recipe_add_owner_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    username = get_message_text(message).strip().lstrip('@')
    if not username:
        await present_recipe(message, state, "Имя не может быть пустым.")
        return
    data = await state.get_data()
    recipe_id = data['recipe_id']
    try:
        async with admin_transition(state):
            await database_for(db).add_recipe_owner(recipe_id, username)
            await state.set_state(RecipeStates.view_recipe)
    except Exception as e:
        await present_recipe(message, state, f"❌ Ошибка: {e}")
        return
    await state.set_state(RecipeStates.view_recipe)
    recipe = await database_for(db).get_recipe_details(recipe_id)
    await show_recipe(message, recipe, state)


async def show_recipe_owners(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await database_for(db).get_recipe_details(recipe_id)
    if recipe is None or not recipe['can_learn']:
        await callback.answer('Для этого рецепта изучение не требуется.', show_alert=True)
        return
    owners = await database_for(db).get_recipe_owner_entries(recipe_id)
    keyboard = [
        [InlineKeyboardButton(
            text=f"❌ {admin_recipe_owner_label(owner)}",
            callback_data=f"recipe_owner_select_{owner['owner_id']}",
        )]
        for owner in owners
    ]
    keyboard.append([InlineKeyboardButton(text="➕ Добавить изучившего игрока", callback_data="recipe_add_owner")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к рецепту", callback_data="recipe_owners_back")])
    text = "👥 Изучившие рецепт. Выберите запись для удаления:" if owners else "👥 Пока никто не отметил изучение рецепта."
    await present_recipe(callback, state, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.manage_owners)


async def recipe_show_owners(callback: types.CallbackQuery, state: FSMContext) -> None:
    await show_recipe_owners(callback, state)
    await callback.answer()


async def recipe_owner_delete_confirm(callback: types.CallbackQuery, state: FSMContext) -> None:
    owner_id = int(get_callback_data(callback).rsplit("_", 1)[1])
    data = await state.get_data()
    owners = await database_for(db).get_recipe_owner_entries(data['recipe_id'])
    owner = next((entry for entry in owners if entry['owner_id'] == owner_id), None)
    if not owner:
        await callback.answer("Список владельцев изменился. Откройте его заново.", show_alert=True)
        return
    owner_label = admin_recipe_owner_label(owner)
    await state.update_data(selected_recipe_owner_id=owner_id)
    confirmation_callback = await prepare_delete_confirmation(
        callback, state, 'recipe_owner', owner_id, 'recipe_owner_delete_yes_',
        context=data['recipe_id'],
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=confirmation_callback)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_owner_delete_cancel")],
    ])
    await present_recipe(callback, state, f"Удалить владельца <b>{escape_html(owner_label)}</b> из рецепта?", parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(RecipeStates.delete_owner_confirm)
    await callback.answer()


async def recipe_owner_delete_execute(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    owner_id = data.get('selected_recipe_owner_id')
    if not await consume_delete_confirmation(
        callback, state, 'recipe_owner', owner_id, 'recipe_owner_delete_yes_',
        RecipeStates.delete_owner_confirm, context=data.get('recipe_id'),
    ) or not isinstance(owner_id, int):
        return
    async with admin_transition(state):
        await database_for(db).remove_recipe_owner_entry(data['recipe_id'], owner_id)
        await state.set_state(RecipeStates.manage_owners)
    await show_recipe_owners(callback, state)
    await callback.answer("Владелец удалён")


async def recipe_edit_ingredients_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await database_for(db).get_recipe_details(recipe_id)
    if recipe is None:
        await show_recipe(callback, None, state)
        await callback.answer()
        return
    if not recipe['ingredients']:
        await callback.answer("Нет ингредиентов", show_alert=True)
        return
    keyboard = []
    for ing in recipe['ingredients']:
        keyboard.append([InlineKeyboardButton(text=f"{ing['emoji']} {ing['name']} — {ing['quantity']} шт.", callback_data=f"recipe_edit_ing_{ing['resource_id']}")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к рецепту", callback_data="recipe_back_to_view")])
    await present_recipe(callback, state, "Выберите ингредиент для изменения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await state.set_state(RecipeStates.edit_ingredient)

async def recipe_edit_ing_options(callback: types.CallbackQuery, state: FSMContext) -> None:
    resource_id = int(get_callback_data(callback).split("_")[3])
    data = await state.get_data()
    recipe = await database_for(db).get_recipe_details(data['recipe_id'])
    ingredient = next((item for item in recipe['ingredients'] if item['resource_id'] == resource_id), None) if recipe else None
    if not ingredient:
        await callback.answer("Ингредиент больше не найден. Откройте рецепт заново.", show_alert=True)
        return
    await state.update_data(edit_resource_id=resource_id, edit_action=None)
    delete_callback = await prepare_delete_confirmation(
        callback, state, 'ingredient', resource_id, 'recipe_ing_delete_', context=data['recipe_id'],
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить количество", callback_data="recipe_ing_change")],
        [InlineKeyboardButton(text="❌ Удалить ингредиент", callback_data=delete_callback)],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="recipe_back_to_edit_list")]
    ])
    await present_recipe(callback, state, f"Ингредиент: {ingredient['name']} (ID {resource_id}). Что сделать?", reply_markup=keyboard)
    await state.set_state(RecipeStates.edit_ingredient_quantity)

async def recipe_ing_change_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    await present_recipe(callback, state, "Введите новое количество:")
    await state.update_data(edit_action='change', admin_delete_confirmation=None)

async def recipe_ing_delete(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipe_id = data.get('recipe_id')
    resource_id = data.get('edit_resource_id')
    if not await consume_delete_confirmation(
        callback, state, 'ingredient', resource_id, 'recipe_ing_delete_',
        RecipeStates.edit_ingredient_quantity, context=recipe_id,
    ) or not isinstance(recipe_id, int) or not isinstance(resource_id, int):
        return
    try:
        async with admin_transition(state):
            await database_for(db).remove_ingredient(recipe_id, resource_id)
            await state.update_data(edit_action=None)
            await state.set_state(RecipeStates.view_recipe)
    except ValueError as error:
        await callback.answer(str(error), show_alert=True)
        await recipe_edit_ingredients_list(callback, state)
        return
    await state.update_data(edit_action=None)
    await state.set_state(RecipeStates.view_recipe)
    await callback.answer("Ингредиент удалён", show_alert=True)
    recipe = await database_for(db).get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)

async def recipe_back_to_edit_list(callback: types.CallbackQuery, state: FSMContext) -> None:
    await recipe_edit_ingredients_list(callback, state)

async def recipe_delete_confirm(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    confirmation_callback = await prepare_delete_confirmation(
        callback, state, 'recipe', data['recipe_id'], 'recipe_delete_yes_',
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=confirmation_callback)],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_back_to_view")]
    ])
    await present_recipe(callback, state, f"Удалить рецепт ID {data['recipe_id']}?", reply_markup=keyboard)
    await state.set_state(RecipeStates.delete_confirm)

async def recipe_delete_execute(callback: types.CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    recipe_id = data.get('recipe_id')
    if not await consume_delete_confirmation(
        callback, state, 'recipe', recipe_id, 'recipe_delete_yes_', RecipeStates.delete_confirm,
    ) or not isinstance(recipe_id, int):
        return
    result_type = data.get('recipe_result_type', 'gear')
    async with admin_transition(state):
        await database_for(db).delete_recipe(recipe_id)
        await state.set_state(RecipeStates.list_page)
    await present_recipe(callback, state, "✅ Рецепт удалён.")
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await present_recipe(callback, state, f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

# ============================================================


async def recipe_output_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RecipeStates.output_quantity)
    await present_recipe(callback, state, 'Количество результата за одно изготовление:',
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='recipe_back_to_view')]]))


async def recipe_output_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    data = await state.get_data()
    try:
        quantity = int(get_message_text(message).strip())
        async with admin_transition(state):
            await database_for(db).update_recipe_quantity(data['recipe_id'], quantity)
            await state.set_state(RecipeStates.view_recipe)
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.set_state(RecipeStates.view_recipe)
    await show_recipe(message, await database_for(db).get_recipe_details(data['recipe_id']), state)


async def recipe_location_prompt(callback: types.CallbackQuery, state: FSMContext) -> None:
    await state.set_state(RecipeStates.craft_location)
    await present_recipe(callback, state, 'Введите место изготовления. «-» очищает поле.',
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='🔙 Назад', callback_data='recipe_back_to_view')]]))


async def recipe_location_save(message: types.Message, state: FSMContext) -> None:
    if not await validate_admin_input(message, state):
        return
    data = await state.get_data()
    text = get_message_text(message).strip()
    try:
        async with admin_transition(state):
            await database_for(db).update_recipe_craft_location(data['recipe_id'], '' if text == '-' else text)
            await state.set_state(RecipeStates.view_recipe)
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.set_state(RecipeStates.view_recipe)
    await show_recipe(message, await database_for(db).get_recipe_details(data['recipe_id']), state)


def create_recipe_router() -> Router:
    recipe_router = Router()
    recipe_router.include_router(create_recipe_creation_router())
    recipe_router.callback_query(F.data == "admin_manage_recipes")(manage_recipes_type)
    recipe_router.callback_query(RecipeStates.list_type, F.data.startswith("recipe_type_"))(recipe_list)
    recipe_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_page_"))(recipe_list_page)
    recipe_router.callback_query(RecipeStates.list_page, F.data == "recipe_back_to_type")(recipe_back_to_type)
    recipe_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_view_"))(recipe_view)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_back_to_list")(recipe_back_to_list)
    recipe_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_add_"))(recipe_add_choose_item)
    recipe_router.callback_query(RecipeStates.add_confirm, F.data.startswith("recipe_new_target_"))(recipe_create)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_ingredient")(recipe_add_ingredient_select)
    recipe_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_page_"))(recipe_ing_page)
    recipe_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_select_"))(recipe_ing_quantity)
    recipe_router.message(RecipeStates.edit_ingredient_quantity, F.text, ~F.text.startswith('/'))(recipe_ing_save_quantity)
    recipe_router.callback_query(F.data == "recipe_back_to_view")(recipe_show_current)
    recipe_router.callback_query(RecipeStates.manage_owners, F.data == "recipe_owners_back")(recipe_show_current)
    recipe_router.callback_query(RecipeStates.add_ingredient, F.data == "recipe_finish_adding")(recipe_show_current)
    recipe_router.callback_query(StateFilter(RecipeStates.view_recipe, RecipeStates.manage_owners), F.data == "recipe_add_owner")(recipe_add_owner_prompt)
    recipe_router.message(RecipeStates.add_owner, F.text, ~F.text.startswith('/'))(recipe_add_owner_save)
    recipe_router.callback_query(RecipeStates.delete_owner_confirm, F.data == "recipe_owner_delete_cancel")(recipe_show_owners)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_manage_owners")(recipe_show_owners)
    recipe_router.callback_query(RecipeStates.manage_owners, F.data.startswith("recipe_owner_select_"))(recipe_owner_delete_confirm)
    recipe_router.callback_query(F.data.startswith("recipe_owner_delete_yes"))(recipe_owner_delete_execute)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_edit_ingredients")(recipe_edit_ingredients_list)
    recipe_router.callback_query(RecipeStates.edit_ingredient, F.data.startswith("recipe_edit_ing_"))(recipe_edit_ing_options)
    recipe_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_change")(recipe_ing_change_prompt)
    recipe_router.callback_query(F.data.startswith("recipe_ing_delete"))(recipe_ing_delete)
    recipe_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_back_to_edit_list")(recipe_back_to_edit_list)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_delete")(recipe_delete_confirm)
    recipe_router.callback_query(F.data.startswith("recipe_delete_yes"))(recipe_delete_execute)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == 'recipe_output_quantity')(recipe_output_prompt)
    recipe_router.message(RecipeStates.output_quantity, F.text, ~F.text.startswith('/'))(recipe_output_save)
    recipe_router.callback_query(RecipeStates.view_recipe, F.data == 'recipe_craft_location')(recipe_location_prompt)
    recipe_router.message(RecipeStates.craft_location, F.text, ~F.text.startswith('/'))(recipe_location_save)
    return recipe_router


recipe_router = create_recipe_router()
