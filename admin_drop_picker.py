"""Shared bounded mob selection for every administrative item editor."""

from collections.abc import Callable
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from database import Database
from utils import escape_html

PAGE_SIZE = 8
MAX_SOURCE_QUERY_LENGTH = 256


@dataclass(frozen=True, slots=True)
class DropPickerView:
    text: str
    keyboard: InlineKeyboardMarkup
    page: int


async def build_drop_picker(
    database: Database,
    selected_ids: list[int],
    *,
    query: str = "",
    page: int = 0,
    selected_only: bool = False,
    callback: Callable[[str], str],
    extra_rows: list[list[InlineKeyboardButton]] | None = None,
) -> DropPickerView:
    if len(query) > MAX_SOURCE_QUERY_LENGTH or not 0 <= page <= 1_000_000:
        raise ValueError("Некорректный поиск или страница источников.")
    selected = set(selected_ids)
    candidates = await database.get_drop_source_mobs(
        query,
        page * PAGE_SIZE,
        PAGE_SIZE + 1,
        mob_ids=selected_ids if selected_only else None,
    )
    if page and not candidates:
        page = 0
        candidates = await database.get_drop_source_mobs(
            query,
            0,
            PAGE_SIZE + 1,
            mob_ids=selected_ids if selected_only else None,
        )
    rows: list[list[InlineKeyboardButton]] = []
    for mob in candidates[:PAGE_SIZE]:
        mark = "☑️" if mob["id"] in selected else "⬜"
        # Show location even for identical names; keep the leading identifier
        # visible if long catalog names have to be shortened for the keyboard.
        label = f"{mark} #{mob['id']} {mob['emoji']} {mob['name'][:44]} · {mob['location_name'][:30]}"
        rows.append([InlineKeyboardButton(text=label, callback_data=callback(f"toggle:{mob['id']}"))])
    navigation: list[InlineKeyboardButton] = []
    if page:
        navigation.append(InlineKeyboardButton(text="◀️ Назад", callback_data=callback(f"page:{page - 1}")))
    if len(candidates) > PAGE_SIZE:
        navigation.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=callback(f"page:{page + 1}")))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="🔎 Найти моба или локацию", callback_data=callback("search"))])
    if query:
        rows.append([InlineKeyboardButton(text="Сбросить поиск", callback_data=callback("clear"))])
    rows.append(
        [
            InlineKeyboardButton(
                text="Показать всех мобов" if selected_only else f"☑️ Показать выбранных ({len(selected)})",
                callback_data=callback("selected"),
            )
        ]
    )
    if extra_rows:
        rows.extend(extra_rows)
    text = (
        "<b>👾 С кого падает</b>\n"
        "Отметьте одного или нескольких мобов. Повторное нажатие снимает выбор.\n"
        f"Выбрано: {len(selected)} · страница {page + 1}\n"
    )
    if selected_only:
        text += "Показаны только выбранные источники.\n"
    if query:
        text += f"Поиск: {escape_html(query)}\n"
    if not candidates:
        text += "Ничего не найдено. Измените поиск или переключитесь на всех мобов.\n"
    text += "Если источник пока неизвестен или предмет не падает с мобов, можно оставить выбор пустым."
    return DropPickerView(text=text, keyboard=InlineKeyboardMarkup(inline_keyboard=rows), page=page)
