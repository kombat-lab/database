from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_utils import ADMIN_ITEMS_PER_PAGE, edit_admin_rich
from database import db
from utils import RICH_TABLE_OPEN, clean_username, escape_html

recipe_router = Router()
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

@recipe_router.callback_query(F.data == "admin_manage_recipes")
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

@recipe_router.callback_query(RecipeStates.list_type, F.data.startswith("recipe_type_"))
async def recipe_list(callback: types.CallbackQuery, state: FSMContext):
    result_type = callback.data.split("_")[2]
    await state.update_data(recipe_result_type=result_type, recipe_page=1)
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await callback.message.edit_text(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

@recipe_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_page_"))
async def recipe_list_page(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    result_type = parts[2]
    page = int(parts[3])
    await state.update_data(recipe_result_type=result_type, recipe_page=page)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)

@recipe_router.callback_query(RecipeStates.list_page, F.data == "recipe_back_to_type")
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

@recipe_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_view_"))
async def recipe_view(callback: types.CallbackQuery, state: FSMContext):
    recipe_id = int(callback.data.split("_")[2])
    recipe = await db.get_recipe_details(recipe_id)
    if not recipe:
        await callback.message.edit_text("Рецепт не найден.")
        return
    await state.update_data(recipe_id=recipe_id, recipe_result_type=recipe['result_type'])
    await show_recipe(callback, recipe, state)
    await callback.answer()

@recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_back_to_list")
async def recipe_back_to_list(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    result_type = data.get('recipe_result_type', 'gear')
    page = data.get('recipe_page', 1)
    keyboard = await get_recipe_list_keyboard(result_type, page)
    await callback.message.edit_text(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)

@recipe_router.callback_query(RecipeStates.list_page, F.data.startswith("recipe_add_"))
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

@recipe_router.callback_query(RecipeStates.add_confirm, F.data.startswith("recipe_new_target_"))
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

@recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_ingredient")
async def recipe_add_ingredient_select(callback: types.CallbackQuery, state: FSMContext):
    resources = await db.get_all_resources_simple()
    if not resources:
        await callback.answer("Нет ресурсов", show_alert=True)
        return
    await state.update_data(ingredient_resources=resources, ingredient_page=1)
    await show_ingredient_page(callback, resources, 1, state)

async def show_ingredient_page(target, resources, page, state):
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

@recipe_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_page_"))
async def recipe_ing_page(callback: types.CallbackQuery, state: FSMContext):
    page = int(callback.data.split("_")[3])
    data = await state.get_data()
    resources = data.get('ingredient_resources')
    if not resources:
        await callback.answer("Ошибка", show_alert=True)
        return
    await show_ingredient_page(callback, resources, page, state)

@recipe_router.callback_query(RecipeStates.add_ingredient, F.data.startswith("recipe_ing_select_"))
async def recipe_ing_quantity(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    resource_id = int(parts[3])
    page = int(parts[4]) if len(parts)>4 else 1
    await state.update_data(temp_resource_id=resource_id, ingredient_return_page=page, edit_action='add')
    await callback.message.edit_text("Введите количество (целое число):")
    await state.set_state(RecipeStates.edit_ingredient_quantity)

@recipe_router.message(RecipeStates.edit_ingredient_quantity, F.text)
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

@recipe_router.callback_query(RecipeStates.add_ingredient, F.data == "recipe_finish_adding")
@recipe_router.callback_query(RecipeStates.manage_owners, F.data == "recipe_owners_back")
@recipe_router.callback_query(
    StateFilter(RecipeStates.edit_ingredient, RecipeStates.delete_confirm),
    F.data == "recipe_back_to_view",
)
async def recipe_show_current(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)
    await callback.answer()

@recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_add_owner")
async def recipe_add_owner_prompt(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data.get('recipe_id')
    recipe = await db.get_recipe_details(recipe_id)
    if recipe and recipe['result_type'] != 'gear':
        await callback.answer("Владельцы добавляются только для рецептов снаряжения.", show_alert=True)
        return
    await callback.message.edit_text("Введите username владельца (без @):")
    await state.set_state(RecipeStates.add_owner)

@recipe_router.message(RecipeStates.add_owner, F.text)
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


@recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_manage_owners")
@recipe_router.callback_query(RecipeStates.delete_owner_confirm, F.data == "recipe_owner_delete_cancel")
async def recipe_show_owners(callback: types.CallbackQuery, state: FSMContext):
    await show_recipe_owners(callback, state)
    await callback.answer()


@recipe_router.callback_query(RecipeStates.manage_owners, F.data.startswith("recipe_owner_delete_"))
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


@recipe_router.callback_query(RecipeStates.delete_owner_confirm, F.data == "recipe_owner_delete_yes")
async def recipe_owner_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    owner = data.get('selected_recipe_owner')
    if owner:
        await db.remove_recipe_owner(data['recipe_id'], owner)
    await show_recipe_owners(callback, state)
    await callback.answer(f"Владелец @{clean_username(owner)} удалён" if owner else "Владелец не найден")


@recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_edit_ingredients")
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

@recipe_router.callback_query(RecipeStates.edit_ingredient, F.data.startswith("recipe_edit_ing_"))
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

@recipe_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_change")
async def recipe_ing_change_prompt(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новое количество:")
    await state.update_data(edit_action='change')

@recipe_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_ing_delete")
async def recipe_ing_delete(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    resource_id = data['edit_resource_id']
    await db.remove_ingredient(recipe_id, resource_id)
    await callback.answer("Ингредиент удалён", show_alert=True)
    recipe = await db.get_recipe_details(recipe_id)
    await show_recipe(callback, recipe, state)

@recipe_router.callback_query(RecipeStates.edit_ingredient_quantity, F.data == "recipe_back_to_edit_list")
async def recipe_back_to_edit_list(callback: types.CallbackQuery, state: FSMContext):
    await recipe_edit_ingredients_list(callback, state)

@recipe_router.callback_query(RecipeStates.view_recipe, F.data == "recipe_delete")
async def recipe_delete_confirm(callback: types.CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data="recipe_delete_yes")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="recipe_back_to_view")]
    ])
    await callback.message.edit_text("Удалить рецепт?", reply_markup=keyboard)
    await state.set_state(RecipeStates.delete_confirm)

@recipe_router.callback_query(RecipeStates.delete_confirm, F.data == "recipe_delete_yes")
async def recipe_delete_execute(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    recipe_id = data['recipe_id']
    result_type = data.get('recipe_result_type', 'gear')
    await db.delete_recipe(recipe_id)
    await callback.message.edit_text("✅ Рецепт удалён.")
    keyboard = await get_recipe_list_keyboard(result_type, 1)
    await callback.message.answer(f"Рецепты: {get_recipe_type_title(result_type)}", reply_markup=keyboard)
    await state.set_state(RecipeStates.list_page)
