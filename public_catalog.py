from __future__ import annotations
import os
import re
from aiogram import Router, F, types
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultsButton,
    InputMediaPhoto,
    InputRichMessage,
    InputRichMessageMedia,
)
from game_constants import (
    GEAR_SLOT_LABELS as SLOT_NAMES,
    GEAR_SLOTS as GEAR_SLOT_ORDER,
    RARITY_NAMES,
    RARITY_KEYS,
)
from routing import CallbackMessageGuard
from telegram_helpers import get_bound_bot, get_callback_data, get_callback_message
from telegram_text import split_formatted_text
from search_rendering import build_search_content
from navigation import (
    MAX_SQLITE_ID,
    build_resource_return_param as build_resource_return_param,
    build_gear_return_param as build_gear_return_param,
    build_recipe_owner_callback as build_recipe_owner_callback,
    parse_gear_view_callback,
    parse_recipe_owner_callback,
    parse_return_param,
    parse_resource_page_callback,
    parse_resource_view_callback,
    parse_location_callback,
    parse_gear_list_callback,
    parse_card_callback,
    return_button_data,
)
from ui.callbacks import (
    CardViewCallback,
    EntityBackCallback,
    EntityNavigateCallback,
    MobViewCallback,
    ResourceLocationViewCallback,
    ResourceViewCallback,
)
from ui.navigation import EntityRef


from public_presentation import (
    PublicContext as PublicContext,
    PublicPresentation,
    MAIN_MENU_BUTTONS,
    MAX_SEARCH_QUERY_LENGTH,
    RESOURCE_TYPE_NAMES,
    logger,
)


class PublicCatalogHandlers(PublicPresentation):
    def __init__(self, context: PublicContext) -> None:
        super().__init__(context)
        self.router = Router(name="public_catalog")
        self.router.callback_query.outer_middleware(CallbackMessageGuard())
        self.router.message(Command("start", "menu"))(self.send_menu)
        self.router.message(Command("search"))(self.search_command)
        self.router.message(F.text == "🐾 Мобы")(self.mobs_button)
        self.router.message(F.text == "📦 Ресурсы")(self.resources_button)
        self.router.message(F.text == "⚔️ Снаряжение")(self.gear_button)
        self.router.message(F.text == "🔍 Поиск")(self.search_button)
        self.router.callback_query(F.data == "resource_cat_alchemy")(self.resource_cat_alchemy)
        self.router.callback_query(F.data == "resource_cat_cards")(self.resource_cat_cards)
        self.router.message(
            StateFilter(None), F.text & ~F.text.startswith("/") & ~F.text.in_(MAIN_MENU_BUTTONS) & ~F.via_bot
        )(self.handle_search)
        self.router.inline_query()(self.inline_search_handler)
        self.router.chosen_inline_result()(self.chosen_inline_result_handler)
        self.router.callback_query(EntityNavigateCallback.filter())(self.navigate_related_entity)
        self.router.callback_query(EntityBackCallback.filter())(self.navigate_related_entity_back)
        self.router.callback_query(F.data == "gear_rarities")(self.gear_rarities_callback)
        self.router.callback_query(F.data.startswith("mobs_location_group_"))(self.location_group_callback)
        self.router.callback_query(F.data == "mobs_dead_forest_locations")(self.legacy_location_group_callback)
        self.router.callback_query(F.data.startswith("back_to_locations_"))(self.back_to_locations)
        self.router.callback_query(
            F.data.startswith(("list_mobs_", "list_resources_", "page_mobs_", "page_resources_"))
        )(self.list_or_page_callback)
        self.router.callback_query(F.data.startswith("gear_slots_"))(self.gear_slots_callback)
        self.router.callback_query(F.data == "gear_empty_category")(self.gear_empty_category_callback)
        self.router.callback_query(F.data.startswith(("gear_slot_", "page_gear_")))(self.gear_list_or_page_callback)
        self.router.callback_query(F.data.startswith("view_mobs_"))(self.view_mob)
        self.router.callback_query(F.data.startswith(("view_resources_", "nav_resources_")))(self.view_resource)
        self.router.callback_query(F.data.startswith(("view_gear_", "nav_gear_")))(self.view_gear)
        self.router.callback_query(F.data.startswith("cards_page_"))(self.cards_page_callback)
        self.router.callback_query(F.data.startswith("view_card_"))(self.view_card)
        self.router.callback_query(F.data.startswith(("recipe_claim_", "recipe_relinquish_")))(self.update_recipe_owner)
        self.router.callback_query(F.data.startswith("resource_cat_"))(self.resource_category_callback)
        self.router.callback_query(F.data.startswith("res_page_"))(self.resource_page_callback)
        self.router.callback_query(F.data == "back_to_resource_cats")(self.back_to_resource_categories)
        self.router.callback_query(F.data.startswith(("view_resource_", "nav_resource_")))(self.view_resource_by_type)
        self.router.callback_query(F.data == "back_to_main_menu")(self.back_to_main_menu)
        self.router.callback_query(F.data.startswith("main_section_"))(self.main_menu_section)

    async def send_menu(self, message: types.Message, state: FSMContext) -> None:
        await state.clear()
        args = (message.text or "").split(maxsplit=1)
        match = None
        return_context = None
        if len(args) > 1 and len(args[1]) <= 64:
            payload, separator, return_param = args[1].partition("-r-")
            match = re.fullmatch(r"(resource|mob|gear|card)_(\d+)", payload)
            return_context = parse_return_param(return_param if separator else None)
        if match and 1 <= int(match.group(2)) <= MAX_SQLITE_ID:
            target_type, target_id = match.group(1), int(match.group(2))
            link_mode = self.get_card_link_mode(message.chat)
            keyboard: InlineKeyboardMarkup | None = None
            if target_type == "mob":
                mob_data = await self.db.get_mob_full_card(target_id)
                if mob_data is None:
                    await message.answer("Моб не найден.")
                    return
                location_id = mob_data["location_id"]
                card_view = await self.build_mob_card(
                    self.db,
                    target_id,
                    location_id,
                    data=mob_data,
                    bot_username=self.BOT_USERNAME,
                    link_mode=link_mode,
                )
                target_callback = f"view_mobs_{target_id}_{location_id}_1"
            elif target_type == "resource":
                resource_data = await self.db.get_resource_card(target_id)
                if resource_data is None:
                    await message.answer("Ресурс не найден.")
                    return
                resource_type = resource_data["type"]
                card_view = await self.build_resource_card(
                    self.db,
                    target_id,
                    "type",
                    resource_type,
                    data=resource_data,
                    bot_username=self.BOT_USERNAME,
                    link_mode=link_mode,
                )
                target_callback = f"view_resource_{target_id}_{resource_type}_1"
            elif target_type == "gear":
                gear_data = await self.db.get_gear_card(target_id)
                if gear_data is None:
                    await message.answer("Предмет не найден.")
                    return
                rarity = gear_data["rarity"]
                target_id = gear_data["id"]
                slot_index = GEAR_SLOT_ORDER.index(gear_data["slot"]) if gear_data["slot"] in GEAR_SLOT_ORDER else None
                card_view = await self.build_gear_card(
                    self.db,
                    target_id,
                    rarity,
                    data=gear_data,
                    slot_index=slot_index,
                    bot_username=self.BOT_USERNAME,
                    link_mode=link_mode,
                )
                if message.from_user:
                    keyboard = await self.build_gear_card_keyboard(
                        gear_data, message.from_user.id, 1, slot_index, personal=message.chat.type == ChatType.PRIVATE
                    )
                slot = f"{slot_index}_" if slot_index is not None else ""
                target_callback = f"view_gear_{target_id}_{rarity}_{slot}1"
            else:
                card_data = await self.db.get_card_by_id(target_id)
                if card_data is None:
                    await message.answer("Карта не найдена.")
                    return
                card_view = await self.build_card_card(
                    self.db,
                    target_id,
                    data=card_data,
                    bot_username=self.BOT_USERNAME,
                    link_mode=link_mode,
                )
                target_callback = f"view_card_{target_id}_1"
            if keyboard is None:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="📋 Открыть в каталоге", callback_data=target_callback),
                        ]
                    ]
                )
            if return_context:
                callback_data, button_text = return_button_data(return_context)
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text=button_text, callback_data=callback_data)],
                        *keyboard.inline_keyboard,
                    ]
                )
            await self.upsert_rich_card(
                bot=get_bound_bot(message),
                chat_id=message.chat.id,
                rich_message=card_view.rich_message,
                plain_text=card_view.fallback_html,
                reply_markup=keyboard,
                message_thread_id=message.message_thread_id,
            )
            if message.from_user:
                view_loggers = {
                    "mob": self.analytics.log_view_mob,
                    "resource": self.analytics.log_view_resource,
                    "gear": self.analytics.log_view_gear,
                    "card": self.analytics.log_view_card,
                }
                await view_loggers[target_type](message.from_user.id, target_id)
            try:
                await message.delete()
            except TelegramAPIError:
                logger.debug("Deep-link command could not be deleted", exc_info=True)
            return
        if message.from_user:
            await self.analytics.log_start(message.from_user.id)
        await message.answer("📋 Главное меню", reply_markup=self.get_main_menu_reply_keyboard())

    async def search_command(self, message: types.Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("🔎 Напиши название моба, ресурса, снаряжения или карты.")

    async def mobs_button(self, message: types.Message, state: FSMContext) -> None:
        await state.clear()
        map_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "assets",
            "world_map.png",
        )

        if not os.path.isfile(map_path):
            logger.error("World map image not found: %s", map_path)
            await message.answer(
                "Выбери локацию мобов:",
                reply_markup=await self.get_locations_keyboard("mobs"),
            )
            return

        keyboard = await self.get_locations_keyboard("mobs")
        rich_message = InputRichMessage(
            html=(
                '<figure><img src="tg://photo?id=world_map"/>'
                "<figcaption>🐾 <b>Выбери локацию мобов:</b></figcaption>"
                "</figure>"
            ),
            media=[
                InputRichMessageMedia(
                    id="world_map",
                    media=InputMediaPhoto(media=FSInputFile(map_path)),
                )
            ],
        )
        await self.upsert_rich_card(
            bot=get_bound_bot(message),
            chat_id=message.chat.id,
            rich_message=rich_message,
            plain_text="🐾 <b>Выбери локацию мобов:</b>",
            reply_markup=keyboard,
            message_thread_id=message.message_thread_id,
        )

    async def resources_button(self, message: types.Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Выбери категорию ресурсов:", reply_markup=self.get_resource_categories_keyboard())

    async def gear_button(self, message: types.Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Выбери редкость снаряжения:", reply_markup=self.get_rarities_keyboard())

    async def search_button(self, message: types.Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(
            "Нажми на кнопку ниже, чтобы включить поиск.\nЗатем просто введи запрос (например, <b>бронзовик</b> или <b>хитин</b>).",
            reply_markup=self.get_inline_search_button(),
            parse_mode="HTML",
        )

    async def resource_cat_alchemy(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await self.show_resources_by_type(callback, "alchemy", 1)
        await callback.answer()

    async def resource_cat_cards(self, callback: types.CallbackQuery) -> None:
        await self.show_cards_list(callback, 1)
        await callback.answer()

    async def handle_search(self, message: types.Message, state: FSMContext) -> None:
        query_text = (message.text or "").strip()
        if len(query_text) < 2:
            await message.answer("Введи хотя бы 2 символа для поиска.")
            return

        if len(query_text) > MAX_SEARCH_QUERY_LENGTH:
            await message.answer(f"Запрос слишком длинный. Максимум {MAX_SEARCH_QUERY_LENGTH} символов.")
            return
        if message.from_user:
            await self.analytics.log_search(message.from_user.id, query_text)

        results = await self.db.search(query_text)
        if not any(results.values()):
            await message.answer("Ничего не найдено.")
            return

        content = build_search_content(results, self.BOT_USERNAME)
        for chunk in split_formatted_text(content):
            await message.answer(chunk.text, entities=list(chunk.entities), parse_mode=None)

    async def inline_search_handler(self, inline_query: InlineQuery) -> None:
        query = inline_query.query.strip()
        try:
            offset = int(inline_query.offset or "0")
        except ValueError:
            offset = -1
        if offset == 0:
            previous = self.inline_log_tasks.pop(inline_query.from_user.id, None)
            if previous is not None:
                previous.cancel()
        if not 2 <= len(query) <= MAX_SEARCH_QUERY_LENGTH:
            self.latest_inline_queries.begin(inline_query.from_user.id, query, first_page=offset == 0)
            self.latest_inline_queries.finish(inline_query.from_user.id)
            await inline_query.answer(
                [],
                cache_time=5,
                is_personal=True,
                button=InlineQueryResultsButton(text="Введи от 2 до 256 символов", start_parameter="start"),
            )
            return
        if offset < 0 or offset > 1_000_000 or offset % 50:
            await inline_query.answer([], cache_time=0, is_personal=True)
            return
        if offset == 0:
            self.inline_log_tasks[inline_query.from_user.id] = self.background_tasks.create_task(
                self.delayed_log_inline_search(inline_query.from_user.id, query),
                name=f"inline-search-{inline_query.from_user.id}",
            )
        self.latest_inline_queries.begin(inline_query.from_user.id, query, first_page=offset == 0)
        try:
            page = await self.inline_search.page(query, offset, self.BOT_USERNAME)
            await inline_query.answer(list(page.results), cache_time=0, is_personal=True, next_offset=page.next_offset)
        finally:
            self.latest_inline_queries.finish(inline_query.from_user.id)

    async def chosen_inline_result_handler(self, chosen_result: types.ChosenInlineResult) -> None:
        await self.analytics.log_inline_result_chosen(
            chosen_result.from_user.id, result_id=chosen_result.result_id, query=chosen_result.query
        )

    async def navigate_related_entity(
        self,
        callback: types.CallbackQuery,
        callback_data: EntityNavigateCallback,
    ) -> None:
        message = callback.message
        if not isinstance(message, types.Message) or message.chat.type != ChatType.PRIVATE:
            await callback.answer("Открой карточку в личном чате с ботом.", show_alert=True)
            return
        source = EntityRef(callback_data.source_type, callback_data.source_id)
        target = EntityRef(callback_data.entity_type, callback_data.entity_id)
        if not source.is_valid or not target.is_valid:
            await callback.answer("Некорректная ссылка.", show_alert=True)
            return
        key = self.get_navigation_key(callback)
        await callback.answer()
        sent = await self.present_interactive_entity(callback, target, source)
        self.entity_navigation.visit(key, source, target, root_state=message.reply_markup)
        self.entity_navigation.transfer(key, (callback.from_user.id, sent.chat.id, sent.message_id))
        await self.log_interactive_entity_view(callback.from_user.id, target)

    async def navigate_related_entity_back(
        self,
        callback: types.CallbackQuery,
        callback_data: EntityBackCallback,
    ) -> None:
        message = callback.message
        if not isinstance(message, types.Message) or message.chat.type != ChatType.PRIVATE:
            await callback.answer("Открой карточку в личном чате с ботом.", show_alert=True)
            return
        fallback = EntityRef(callback_data.entity_type, callback_data.entity_id)
        if not fallback.is_valid:
            await callback.answer("История переходов устарела.", show_alert=True)
            return
        key = self.get_navigation_key(callback)
        target = self.entity_navigation.previous(key) or fallback
        previous = self.entity_navigation.previous_after_back(key)
        root_markup = self.entity_navigation.root_state(key) if previous is None else None
        await callback.answer()
        sent = await self.present_interactive_entity(callback, target, previous, root_markup=root_markup)
        self.entity_navigation.back(key)
        self.entity_navigation.transfer(key, (callback.from_user.id, sent.chat.id, sent.message_id))
        await self.log_interactive_entity_view(callback.from_user.id, target)

    async def gear_rarities_callback(self, callback: types.CallbackQuery) -> None:
        await self.replace_callback_message_text(
            callback, "Выбери редкость снаряжения:", reply_markup=self.get_rarities_keyboard()
        )
        await callback.answer()

    async def location_group_callback(self, callback: types.CallbackQuery) -> None:
        raw_id = get_callback_data(callback).removeprefix("mobs_location_group_")
        if not raw_id.isdecimal() or not 1 <= int(raw_id) <= MAX_SQLITE_ID:
            await callback.answer("Некорректная локация.", show_alert=True)
            return
        parent = await self.db.get_location_by_id(int(raw_id))
        if parent is None:
            await callback.answer("Локация не найдена.", show_alert=True)
            return
        await self.edit_callback_window(
            callback,
            f"{parent['name']} — выбери локацию:",
            reply_markup=await self.get_location_group_keyboard(parent["id"]),
        )
        await callback.answer()

    async def legacy_location_group_callback(self, callback: types.CallbackQuery) -> None:
        # Old keyboards remain safe after the hierarchy is edited or migrated.
        await self.edit_callback_window(
            callback, "Выбери локацию мобов:", reply_markup=await self.get_locations_keyboard("mobs")
        )
        await callback.answer()

    async def back_to_locations(self, callback: types.CallbackQuery) -> None:
        category = get_callback_data(callback).removeprefix("back_to_locations_")
        if category not in {"mobs", "resources"}:
            await callback.answer("Некорректная категория.", show_alert=True)
            return
        text = "Выбери локацию для мобов:" if category == "mobs" else "Выбери локацию для ресурсов:"
        keyboard = await self.get_locations_keyboard(category)
        await self.edit_callback_window(callback, text, reply_markup=keyboard)
        await callback.answer()

    async def list_or_page_callback(self, callback: types.CallbackQuery) -> None:
        parsed = parse_location_callback(get_callback_data(callback))
        if not parsed:
            await callback.answer("Некорректная ссылка на локацию.", show_alert=True)
            return
        category, _, loc_id, page = parsed
        location = await self.db.get_location_by_id(loc_id)
        if location is None:
            await callback.answer("Локация удалена. Открой меню заново.", show_alert=True)
            return
        keyboard = await self.get_items_keyboard(category, loc_id, page)
        title = self.get_location_list_title(location, category, page)
        await self.edit_callback_window(callback, title, reply_markup=keyboard)
        await callback.answer()

    async def gear_slots_callback(self, callback: types.CallbackQuery) -> None:
        rarity = get_callback_data(callback).removeprefix("gear_slots_")
        if rarity not in RARITY_KEYS:
            await callback.answer("Некорректная редкость.", show_alert=True)
            return
        keyboard = await self.get_gear_slots_keyboard(rarity)
        await self.replace_callback_message_text(callback, "Выбери слот снаряжения:", reply_markup=keyboard)
        await callback.answer()

    async def gear_empty_category_callback(self, callback: types.CallbackQuery) -> None:
        await callback.answer("В этой категории пока нет предметов.", show_alert=False)

    async def gear_list_or_page_callback(self, callback: types.CallbackQuery) -> None:
        parsed = parse_gear_list_callback(get_callback_data(callback))
        if not parsed:
            await callback.answer("Некорректная ссылка на снаряжение.", show_alert=True)
            return
        rarity, slot_index, page = parsed
        slot = GEAR_SLOT_ORDER[slot_index]
        keyboard = await self.get_gear_by_slot_keyboard(rarity, slot_index, page)
        text = f"⚔️ <b>{RARITY_NAMES.get(rarity, rarity)} · {SLOT_NAMES[slot]}</b>\nСтраница {page}"
        await self.replace_callback_message_text(callback, text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()

    async def view_mob(self, callback: types.CallbackQuery) -> None:
        await callback.answer()
        parsed = parse_location_callback(get_callback_data(callback))
        if not parsed or parsed[1] is None:
            return
        _, mob_id, location_id, page = parsed
        if mob_id is None:
            return

        await self.analytics.log_view_mob(callback.from_user.id, mob_id)

        card_view = await self.build_mob_card(
            self.db,
            mob_id,
            location_id,
            page,
            bot_username=self.BOT_USERNAME,
            link_mode=self.get_card_link_mode(get_callback_message(callback).chat),
        )

        # Формируем клавиатуру
        neighbours = await self.db.get_prev_next_mob_by_hp(
            mob_id,
            location_id,
        )
        nav_buttons = []
        if neighbours["prev_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Предыдущий", callback_data=MobViewCallback(neighbours["prev_id"], location_id, page).pack()
                )
            )
        if neighbours["next_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Следующий ▶️", callback_data=MobViewCallback(neighbours["next_id"], location_id, page).pack()
                )
            )
        back_button = InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"list_mobs_{location_id}_{page}")

        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([back_button])
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

        await self.upsert_rich_card(
            bot=get_bound_bot(callback),
            chat_id=get_callback_message(callback).chat.id,
            rich_message=card_view.rich_message,
            plain_text=card_view.fallback_html,
            reply_markup=reply_markup,
            current_message=get_callback_message(callback),
        )

    async def view_resource(self, callback: types.CallbackQuery) -> None:
        await callback.answer()
        is_navigation = get_callback_data(callback).startswith("nav_resources_")
        parsed = parse_location_callback(get_callback_data(callback))
        if not parsed or parsed[1] is None:
            return
        _, res_id, location_id, page = parsed
        if res_id is None:
            return
        await self.analytics.log_view_resource(callback.from_user.id, res_id)

        card_view = await self.build_resource_card(
            self.db,
            res_id,
            context_type="location",
            context_id=location_id,
            page=page,
            bot_username=self.BOT_USERNAME,
            link_mode=self.get_card_link_mode(get_callback_message(callback).chat),
        )

        neighbours = await self.db.get_prev_next_resource_by_location(
            res_id,
            location_id,
        )

        nav_buttons = []
        if neighbours["prev_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Предыдущий",
                    callback_data=ResourceLocationViewCallback(neighbours["prev_id"], location_id, page).pack(
                        navigation=True
                    ),
                )
            )
        if neighbours["next_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Следующий ▶️",
                    callback_data=ResourceLocationViewCallback(neighbours["next_id"], location_id, page).pack(
                        navigation=True
                    ),
                )
            )
        back_button = InlineKeyboardButton(
            text="🔙 Назад к списку", callback_data=f"list_resources_{location_id}_{page}"
        )

        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([back_button])
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

        render_card = self.replace_rich_card if is_navigation else self.upsert_rich_card
        await render_card(
            bot=get_bound_bot(callback),
            chat_id=get_callback_message(callback).chat.id,
            rich_message=card_view.rich_message,
            plain_text=card_view.fallback_html,
            reply_markup=reply_markup,
            current_message=get_callback_message(callback),
        )

    async def view_gear(self, callback: types.CallbackQuery) -> None:
        parsed = parse_gear_view_callback(get_callback_data(callback))
        if not parsed:
            await callback.answer("Некорректная ссылка на снаряжение.", show_alert=True)
            return
        gear_id, rarity, slot_index, page = parsed
        await callback.answer()
        await self.analytics.log_view_gear(callback.from_user.id, gear_id)
        await self.render_gear_card(
            callback,
            gear_id,
            rarity,
            page,
            slot_index,
            replace=get_callback_data(callback).startswith("nav_gear_"),
        )

    async def cards_page_callback(self, callback: types.CallbackQuery) -> None:
        parsed = parse_card_callback(get_callback_data(callback))
        if not parsed:
            await callback.answer("Некорректная страница.", show_alert=True)
            return
        _, page = parsed
        await self.show_cards_list(callback, page)
        await callback.answer()

    async def view_card(self, callback: types.CallbackQuery) -> None:
        await callback.answer()
        parsed = parse_card_callback(get_callback_data(callback))
        if not parsed or parsed[0] is None:
            return
        card_id, page = parsed
        if card_id is None:
            return
        await self.analytics.log_view_card(callback.from_user.id, card_id)
        card_view = await self.build_card_card(
            self.db,
            card_id,
            page,
            bot_username=self.BOT_USERNAME,
            link_mode=self.get_card_link_mode(get_callback_message(callback).chat),
        )

        neighbours = await self.db.get_prev_next_card_by_slot(card_id)

        nav_buttons = []
        if neighbours["prev_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Предыдущая", callback_data=CardViewCallback(neighbours["prev_id"], page).pack()
                )
            )
        if neighbours["next_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Следующая ▶️", callback_data=CardViewCallback(neighbours["next_id"], page).pack()
                )
            )

        back_button = InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"cards_page_{page}")

        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([back_button])

        await self.upsert_rich_card(
            bot=get_bound_bot(callback),
            chat_id=get_callback_message(callback).chat.id,
            rich_message=card_view.rich_message,
            plain_text=card_view.fallback_html,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            current_message=get_callback_message(callback),
        )

    async def update_recipe_owner(self, callback: types.CallbackQuery) -> None:
        parsed = parse_recipe_owner_callback(get_callback_data(callback))
        if not parsed:
            await callback.answer("Некорректная кнопка рецепта.", show_alert=True)
            return
        action, recipe_id, gear_id, rarity, slot_index, page = parsed
        gear = await self.db.get_gear_card(gear_id)
        if not gear or gear.get("recipe_id") != recipe_id:
            await callback.answer(
                "Рецепт изменился или был удалён. Открой карточку заново.",
                show_alert=True,
            )
            return
        if not gear.get("can_learn", False):
            await callback.answer(
                "Для этого снаряжения учёт владельцев рецепта недоступен.",
                show_alert=True,
            )
            return

        try:
            if action == "claim":
                await self.db.claim_recipe_owner(
                    recipe_id,
                    callback.from_user.id,
                    callback.from_user.username,
                    expected_gear_id=gear_id,
                )
                result_text = "✅ Отмечено: ты изучил рецепт."
            else:
                await self.db.relinquish_recipe_owner(recipe_id, callback.from_user.id)
                result_text = "Отметка об изучении рецепта удалена."
        except ValueError as error:
            await callback.answer(str(error), show_alert=True)
            return
        await callback.answer(result_text, show_alert=False)

        key = self.get_navigation_key(callback)
        entity = self.entity_navigation.current(key)
        if entity and entity.entity_type == "gear" and entity.entity_id in {gear_id, gear["id"]}:
            previous = self.entity_navigation.previous(key)
            sent = await self.present_interactive_entity(
                callback,
                EntityRef("gear", gear["id"]),
                previous,
                root_markup=self.entity_navigation.root_state(key) if previous is None else None,
            )
            self.entity_navigation.transfer(key, (callback.from_user.id, sent.chat.id, sent.message_id))
            return
        await self.render_gear_card(
            callback,
            gear_id,
            rarity,
            page,
            slot_index,
        )

    async def resource_category_callback(self, callback: types.CallbackQuery) -> None:
        resource_type = get_callback_data(callback).removeprefix("resource_cat_")
        if resource_type not in RESOURCE_TYPE_NAMES:
            await callback.answer("Неверная категория.", show_alert=True)
            return
        await self.show_resources_by_type(callback, resource_type, 1)
        await callback.answer()

    async def resource_page_callback(self, callback: types.CallbackQuery) -> None:
        parsed = parse_resource_page_callback(get_callback_data(callback), "res_page_")
        if not parsed:
            await callback.answer("Неверная страница.", show_alert=True)
            return
        resource_type, page = parsed
        await self.show_resources_by_type(callback, resource_type, page)
        await callback.answer()

    async def back_to_resource_categories(self, callback: types.CallbackQuery) -> None:
        await self.replace_callback_message_text(
            callback, "Выбери категорию ресурсов:", reply_markup=self.get_resource_categories_keyboard()
        )
        await callback.answer()

    async def view_resource_by_type(self, callback: types.CallbackQuery) -> None:
        parsed = parse_resource_view_callback(get_callback_data(callback))
        if not parsed:
            await callback.answer("Неверная ссылка на ресурс.", show_alert=True)
            return
        resource_id, resource_type, page = parsed
        is_navigation = get_callback_data(callback).startswith("nav_resource_")
        await callback.answer()
        await self.analytics.log_view_resource(callback.from_user.id, resource_id)

        card_view = await self.build_resource_card(
            self.db,
            resource_id,
            context_type="type",
            context_id=resource_type,
            page=page,
            bot_username=self.BOT_USERNAME,
            link_mode=self.get_card_link_mode(get_callback_message(callback).chat),
        )

        neighbours = await self.db.get_prev_next_resource_by_type(
            resource_id,
            resource_type,
        )

        nav_buttons = []
        if neighbours["prev_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Предыдущий",
                    callback_data=ResourceViewCallback(neighbours["prev_id"], resource_type, page).pack(
                        navigation=True
                    ),
                )
            )
        if neighbours["next_id"]:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Следующий ▶️",
                    callback_data=ResourceViewCallback(neighbours["next_id"], resource_type, page).pack(
                        navigation=True
                    ),
                )
            )

        back_button = InlineKeyboardButton(text="🔙 Назад к списку", callback_data=f"res_page_{resource_type}_{page}")

        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([back_button])
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

        render_card = self.replace_rich_card if is_navigation else self.upsert_rich_card
        await render_card(
            bot=get_bound_bot(callback),
            chat_id=get_callback_message(callback).chat.id,
            rich_message=card_view.rich_message,
            plain_text=card_view.fallback_html,
            reply_markup=reply_markup,
            current_message=get_callback_message(callback),
        )

    async def back_to_main_menu(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await self.replace_callback_message_text(
            callback, "📋 Главное меню", reply_markup=self.get_main_menu_inline_keyboard()
        )
        await callback.answer()

    async def main_menu_section(self, callback: types.CallbackQuery, state: FSMContext) -> None:
        section = get_callback_data(callback).removeprefix("main_section_")
        if section not in {"mobs", "resources", "gear", "search"}:
            await callback.answer("Неизвестный раздел.", show_alert=True)
            return
        await state.clear()
        if section == "mobs":
            text = "Выбери локацию для мобов:"
            keyboard = await self.get_locations_keyboard("mobs")
        elif section == "resources":
            text = "Выбери категорию ресурсов:"
            keyboard = self.get_resource_categories_keyboard()
        elif section == "gear":
            text = "Выбери редкость снаряжения:"
            keyboard = self.get_rarities_keyboard()
        else:
            text = "Нажми кнопку поиска и введи название моба, ресурса, снаряжения или карты."
            keyboard = self.get_inline_search_button()
        rows = list(keyboard.inline_keyboard)
        if not any(button.callback_data == "back_to_main_menu" for row in rows for button in row):
            rows.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main_menu")])
        await self.replace_callback_message_text(
            callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
        await callback.answer()


def create_public_router(context: PublicContext) -> Router:
    return PublicCatalogHandlers(context).router
