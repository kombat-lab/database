"""Drafted drop editing from resource and card creation or their edit menu."""

import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from typing import Literal, TypeAlias

from aiogram import F, Router, types
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from admin_contracts import EntityConfig
from admin_commands import admin_transition
from admin_drop_picker import MAX_SOURCE_QUERY_LENGTH, build_drop_picker
from admin_sessions import PENDING_SCREEN_KEY, present_admin_text, validate_admin_input
from admin_utils import GenericEditStates, show_edit_menu
from runtime_scope import database_for
from recipe_domain import DomainError, positive_integer
from telegram_helpers import get_callback_data, get_message_text
from utils import escape_html

logger = logging.getLogger(__name__)
CatalogKind: TypeAlias = Literal["resource", "card"]
Target: TypeAlias = types.Message | types.CallbackQuery
FinishCreation: TypeAlias = Callable[[Target, FSMContext, list[int]], Awaitable[None]]
ReturnToCreation: TypeAlias = Callable[[Target, FSMContext], Awaitable[None]]


class ItemSourcesStates(StatesGroup):
    select = State()
    search = State()


@dataclass(frozen=True, slots=True)
class SourceSelection:
    kind: CatalogKind
    item_id: int | None
    name: str
    selected: list[int]
    baseline: list[int]
    query: str = ""
    page: int = 0
    selected_only: bool = False


def decode_ids(value: object) -> list[int]:
    if not isinstance(value, list) or len(value) > 1000:
        raise DomainError("Некорректный список источников. Откройте предмет заново.")
    ids = [positive_integer(item, "Источник") for item in value]
    if len(ids) != len(set(ids)):
        raise DomainError("Источник указан дважды.")
    return ids


def selection_from_data(value: object) -> SourceSelection:
    if not isinstance(value, dict) or value.get("kind") not in ("resource", "card"):
        raise DomainError("Выбор источников устарел. Откройте предмет заново.")
    kind: CatalogKind = "resource" if value["kind"] == "resource" else "card"
    item_id = value.get("item_id")
    if item_id is not None:
        item_id = positive_integer(item_id, "Предмет")
    name, query, page, selected_only = (
        value.get("name"),
        value.get("query"),
        value.get("page"),
        value.get("selected_only"),
    )
    if (
        not isinstance(name, str)
        or not isinstance(query, str)
        or len(query) > MAX_SOURCE_QUERY_LENGTH
        or isinstance(page, bool)
        or not isinstance(page, int)
        or not 0 <= page <= 1_000_000
        or not isinstance(selected_only, bool)
    ):
        raise DomainError("Некорректное состояние выбора источников.")
    return SourceSelection(
        kind,
        item_id,
        name,
        decode_ids(value.get("selected")),
        decode_ids(value.get("baseline")),
        query,
        page,
        selected_only,
    )


async def current_selection(state: FSMContext) -> SourceSelection:
    return selection_from_data((await state.get_data()).get("item_sources"))


async def store_selection(state: FSMContext, selection: SourceSelection) -> None:
    await state.update_data(item_sources=asdict(selection))
    if selection.item_id is None:
        # Returning to the note step keeps all chosen mobs in this creation.
        await state.update_data(catalog_source_mob_ids=list(selection.selected))


async def present_selection(target: Target, state: FSMContext, selection: SourceSelection) -> None:
    rows = []
    if selection.selected:
        rows.append([InlineKeyboardButton(text="Снять все отметки", callback_data="isd:none")])
    save_label = (
        ("✅ Создать ресурс" if selection.kind == "resource" else "✅ Создать карту")
        if selection.item_id is None
        else "✅ Сохранить источники"
    )
    rows.append([InlineKeyboardButton(text=save_label, callback_data="isd:done")])
    rows.append(
        [
            InlineKeyboardButton(
                text="🔙 К примечанию" if selection.item_id is None else "🔙 Назад без сохранения",
                callback_data="isd:back",
            )
        ]
    )
    if selection.item_id is None:
        rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin_cancel_edit")])
    view = await build_drop_picker(
        database_for(),
        selection.selected,
        query=selection.query,
        page=selection.page,
        selected_only=selection.selected_only,
        callback=lambda action: f"isd:{action}",
        extra_rows=rows,
    )
    selection = replace(selection, page=view.page)
    data = await state.get_data()

    async def commit_selection() -> None:
        await store_selection(state, selection)
        await state.set_state(ItemSourcesStates.select)

    await present_admin_text(
        target,
        state,
        f"<b>{escape_html(selection.name)}</b>\n\n{view.text}",
        view.keyboard,
        context={
            "item_source_session": str(data["item_source_session"]),
            "item_source_kind": selection.kind,
            "item_source_entity_id": selection.item_id or 0,
        },
        parse_mode="HTML",
        commit=commit_selection,
    )


async def start_item_sources(target: Target, state: FSMContext, kind: CatalogKind, item_id: int | None = None) -> None:
    data = await state.get_data()
    if item_id is None:
        if data.get("catalog_creation_kind") != kind or not isinstance(data.get("catalog_creation_session"), str):
            raise DomainError("Создание предмета устарело. Откройте админку заново.")
        name = data.get("res_name" if kind == "resource" else "card_name")
        if not isinstance(name, str):
            raise DomainError("Сначала задайте название предмета.")
        selected = decode_ids(data.get("catalog_source_mob_ids", []))
        baseline: list[int] = []
    else:
        item = (
            await database_for().get_resource_by_id(item_id)
            if kind == "resource"
            else await database_for().get_card_by_id(item_id)
        )
        if item is None:
            raise DomainError("Предмет уже удалён.")
        name = item["name"]
        selected = await database_for().get_item_drop_mob_ids(kind, item_id)
        baseline = list(selected)
    await state.update_data(
        item_source_session=secrets.token_hex(8), item_source_kind=kind, item_source_entity_id=item_id or 0
    )
    await present_selection(target, state, SourceSelection(kind, item_id, name, selected, baseline))


def register_item_sources_handlers(
    router: Router,
    get_configs: Callable[[], dict[str, EntityConfig]],
    finish_creation: FinishCreation,
    return_to_creation: ReturnToCreation,
) -> None:
    async def return_to_item(target: Target, state: FSMContext, selection: SourceSelection) -> None:
        if selection.item_id is None:
            await return_to_creation(target, state)
            return
        entity = (
            await database_for().get_resource_by_id(selection.item_id)
            if selection.kind == "resource"
            else await database_for().get_card_by_id(selection.item_id)
        )
        if entity is None:
            await state.clear()
            if isinstance(target, types.CallbackQuery):
                await target.answer("Предмет уже удалён.", show_alert=True)
            else:
                await target.answer("Предмет уже удалён.")
            return
        await show_edit_menu(target, state, selection.item_id, get_configs()[selection.kind], entity)

    @router.callback_query(GenericEditStates.select_field, F.data == "item_sources_open")
    async def open_sources(callback: types.CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        kind = data.get("editing_entity")
        if kind not in ("resource", "card"):
            await callback.answer("Источники этого предмета редактируются в его карточке.", show_alert=True)
            return
        try:
            await start_item_sources(
                callback,
                state,
                "resource" if kind == "resource" else "card",
                positive_integer(data.get("entity_id"), "Предмет"),
            )
        except ValueError as error:
            await callback.answer(str(error)[:180], show_alert=True)
            return
        await callback.answer()

    @router.callback_query(StateFilter(ItemSourcesStates.select, ItemSourcesStates.search), F.data.startswith("isd:"))
    async def source_action(callback: types.CallbackQuery, state: FSMContext) -> None:
        try:
            selected = await current_selection(state)
            action = get_callback_data(callback).removeprefix("isd:")
            if action == "done":
                if selected.item_id is None:
                    await finish_creation(callback, state, selected.selected)
                    return
                try:
                    async with admin_transition(state):
                        await database_for().set_item_drop_sources(
                            selected.kind, selected.item_id, selected.selected, expected_mob_ids=selected.baseline
                        )
                        await state.set_state(GenericEditStates.select_field)
                except DomainError:
                    raise
                except Exception:
                    logger.exception("Не удалось обновить источники предмета")
                    await callback.answer("Не удалось сохранить. Повторите попытку.", show_alert=True)
                    return
                # The write is complete even if the next Telegram screen fails.
                await state.set_state(GenericEditStates.select_field)
                await return_to_item(callback, state, selected)
                return
            if action == "back":
                previous_data = await state.get_data()
                previous_state = await state.get_state()
                try:
                    await return_to_item(callback, state, selected)
                except Exception:
                    current_data = await state.get_data()
                    # The generic editor enters its state before delivery. Keep
                    # this picker usable if that delivery never bound a new screen.
                    if current_data.get("admin_screen") == previous_data.get("admin_screen"):
                        if PENDING_SCREEN_KEY in current_data:
                            previous_data[PENDING_SCREEN_KEY] = current_data[PENDING_SCREEN_KEY]
                        await state.set_data(previous_data)
                        await state.set_state(previous_state)
                    raise
                return
            if action == "search":
                data = await state.get_data()
                await present_admin_text(
                    callback,
                    state,
                    "Введите часть имени моба или названия локации. «-» сбрасывает поиск.",
                    InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="🔙 К источникам", callback_data=f"isd:page:{selected.page}")]
                        ]
                    ),
                    context={
                        "item_source_session": str(data["item_source_session"]),
                        "item_source_kind": selected.kind,
                        "item_source_entity_id": selected.item_id or 0,
                    },
                )
                await state.set_state(ItemSourcesStates.search)
                await callback.answer()
                return
            if action.startswith("toggle:"):
                mob_id = positive_integer(int(action.removeprefix("toggle:")), "Моб")
                ids = list(selected.selected)
                if mob_id in ids:
                    ids.remove(mob_id)
                else:
                    if not await database_for().get_drop_source_mobs(mob_ids=[mob_id]):
                        raise DomainError("Моб уже удалён. Обновите список.")
                    if len(ids) >= 1000:
                        raise DomainError("Допустимо не больше 1000 источников.")
                    ids.append(mob_id)
                selected = replace(selected, selected=sorted(ids))
            elif action.startswith("page:"):
                page = int(action.removeprefix("page:"))
                if not 0 <= page <= 1_000_000:
                    raise DomainError("Некорректная страница.")
                selected = replace(selected, page=page)
            elif action == "clear":
                selected = replace(selected, query="", page=0)
            elif action == "selected":
                selected = replace(selected, selected_only=not selected.selected_only, page=0)
            elif action == "none":
                selected = replace(selected, selected=[], page=0)
            else:
                raise DomainError("Неизвестное действие.")
            await present_selection(callback, state, selected)
            await callback.answer()
        except DomainError as error:
            await callback.answer(str(error)[:180], show_alert=True)
        except ValueError:
            await callback.answer("Некорректная кнопка источников.", show_alert=True)

    @router.message(ItemSourcesStates.search, F.text, ~F.text.startswith("/"))
    async def source_search(message: types.Message, state: FSMContext) -> None:
        if not await validate_admin_input(message, state):
            return
        query = get_message_text(message).strip()
        if len(query) > MAX_SOURCE_QUERY_LENGTH:
            await message.answer(f"Поиск: не больше {MAX_SOURCE_QUERY_LENGTH} символов.")
            return
        try:
            selected = await current_selection(state)
        except DomainError as error:
            await message.answer(str(error))
            return
        await present_selection(message, state, replace(selected, query="" if query == "-" else query, page=0))
