"""Explicit catalog configuration, with dynamic rows only at SQL/FSM boundaries."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Literal, NotRequired, TypeAlias, TypedDict

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State
from aiogram.types import CallbackQuery, InlineKeyboardButton

EntityRow: TypeAlias = Mapping[str, object]
StateData: TypeAlias = Mapping[str, object]
EntityName: TypeAlias = Literal["resource", "gear", "card"]
GetEntityPage: TypeAlias = Callable[[int, int], Awaitable[Sequence[EntityRow]]]
GetEntityById: TypeAlias = Callable[[int], Awaitable[EntityRow | None]]
DeleteEntity: TypeAlias = Callable[[int], Awaitable[None]]
UpdateEntity: TypeAlias = Callable[[int, str, str | int], Awaitable[None]]
ExtraEditButtons: TypeAlias = Callable[[int], Sequence[Sequence[InlineKeyboardButton]]]
ReturnToEntityList: TypeAlias = Callable[[CallbackQuery, FSMContext, StateData], Awaitable[None]]


class EntityConfig(TypedDict):
    name: EntityName
    name_ru: str
    get_page_func: GetEntityPage
    get_by_id_func: GetEntityById
    update_func: UpdateEntity
    delete_func: DeleteEntity
    item_callback_prefix: str
    list_state: State
    list_title: str
    add_button: bool
    add_button_text: str
    add_callback: str
    edit_fields: Sequence[tuple[str, str]]
    integer_fields: Sequence[str]
    select_options: Mapping[str, Sequence[str]]
    display_mapping: Mapping[str, Mapping[str, str]]
    integer_minimums: NotRequired[Mapping[str, int]]
    field_formatters: NotRequired[Mapping[str, Callable[[object], str]]]
    extra_edit_buttons: NotRequired[ExtraEditButtons]
    back_to_list_func: NotRequired[ReturnToEntityList]
    # A nonempty explanation blocks deletion before asking for confirmation.
    delete_impact_func: NotRequired[Callable[[int], Awaitable[str]]]
