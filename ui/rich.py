from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InputRichMessage

from messaging import upsert_rich_card
from utils import RICH_TABLE_OPEN
from .links import MarkupPair

SECTION_DIVIDER = "<hr/>"


def _pair(value: MarkupPair | str) -> MarkupPair:
    return value if isinstance(value, MarkupPair) else MarkupPair.same(value)


@dataclass(frozen=True)
class CardView:
    """One card definition with RichMessage and classic HTML representations."""

    rich_html: str
    fallback_html: str

    @property
    def rich_message(self) -> InputRichMessage:
        return InputRichMessage(html=self.rich_html)


@dataclass
class CardComposer:
    """Builds both representations from the same ordered content blocks."""

    _sections: list[MarkupPair] = field(default_factory=list)

    def add(self, rich: str, fallback: str | None = None) -> None:
        self._sections.append(MarkupPair(rich, rich if fallback is None else fallback))

    def add_pair(self, value: MarkupPair | str) -> None:
        self._sections.append(_pair(value))

    def add_divider(self) -> None:
        self._sections.append(MarkupPair(SECTION_DIVIDER, ""))

    def add_list(
        self,
        title: MarkupPair | str,
        items: Iterable[MarkupPair | str],
    ) -> None:
        title_pair = _pair(title)
        item_pairs = [_pair(item) for item in items]
        if not item_pairs:
            return
        self._sections.append(
            MarkupPair(
                rich=f"<b>{title_pair.rich}</b><br>" + "<br>".join(item.rich for item in item_pairs),
                fallback=f"<b>{title_pair.fallback}</b>\n" + "\n".join(item.fallback for item in item_pairs),
            )
        )

    def add_table(
        self,
        rows: Sequence[Sequence[MarkupPair | str]],
        *,
        headers: Sequence[MarkupPair | str] | None = None,
        title: MarkupPair | str | None = None,
        fallback_rows: Sequence[MarkupPair | str] | None = None,
        details_summary: MarkupPair | str | None = None,
        fallback_spoiler: bool = False,
    ) -> None:
        row_pairs = [[_pair(cell) for cell in row] for row in rows]
        header_pairs = [_pair(cell) for cell in headers] if headers else []
        title_pair = _pair(title) if title is not None else None
        summary_pair = _pair(details_summary) if details_summary is not None else None

        header_html = ""
        if header_pairs:
            header_html = "<tr>" + "".join(f"<th>{cell.rich}</th>" for cell in header_pairs) + "</tr>"
        body_html = "".join("<tr>" + "".join(f"<td>{cell.rich}</td>" for cell in row) + "</tr>" for row in row_pairs)
        table_html = f"{RICH_TABLE_OPEN}<tbody>{header_html}{body_html}</tbody></table>"
        if title_pair:
            table_html = f"<b>{title_pair.rich}</b><br>{table_html}"
        if summary_pair:
            table_html = f"<details><summary>{summary_pair.rich}</summary>{table_html}</details>"

        if fallback_rows is None:
            fallback_pairs = [
                MarkupPair(
                    rich=" — ".join(cell.rich for cell in row),
                    fallback=" — ".join(cell.fallback for cell in row),
                )
                for row in row_pairs
            ]
        else:
            fallback_pairs = [_pair(row) for row in fallback_rows]
        fallback_body = "\n".join(row.fallback for row in fallback_pairs)
        if title_pair:
            fallback_body = f"<b>{title_pair.fallback}</b>\n{fallback_body}"
        if summary_pair:
            summary = f"<b>{summary_pair.fallback}</b>"
            fallback_body = f"{summary}\n{fallback_body}"
            if fallback_spoiler:
                fallback_body = f"<tg-spoiler>{fallback_body}</tg-spoiler>"

        self._sections.append(MarkupPair(table_html, fallback_body))

    def build(self) -> CardView:
        return CardView(
            rich_html="<br>".join(section.rich for section in self._sections if section.rich).strip(),
            fallback_html="\n\n".join(section.fallback for section in self._sections if section.fallback).strip(),
        )


async def present_rich_card(
    *,
    bot: Bot,
    chat_id: int,
    card: CardView,
    reply_markup: InlineKeyboardMarkup | None = None,
    current_message: types.Message | None = None,
    message_thread_id: int | None = None,
) -> types.Message:
    """Deliver both representations through the shared safe replacement policy."""
    return await upsert_rich_card(
        bot=bot,
        chat_id=chat_id,
        rich_message=card.rich_message,
        plain_text=card.fallback_html,
        reply_markup=reply_markup,
        current_message=current_message,
        message_thread_id=message_thread_id,
    )
