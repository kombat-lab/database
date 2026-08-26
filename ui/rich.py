from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from aiogram import Bot, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InputRichMessage

from utils import RICH_TABLE_OPEN
from .links import MarkupPair

logger = logging.getLogger(__name__)


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

    def add_list(
        self,
        title: MarkupPair | str,
        items: Iterable[MarkupPair | str],
    ) -> None:
        title_pair = _pair(title)
        item_pairs = [_pair(item) for item in items]
        if not item_pairs:
            return
        self._sections.append(MarkupPair(
            rich=f"<b>{title_pair.rich}</b><br>" + "<br>".join(item.rich for item in item_pairs),
            fallback=f"<b>{title_pair.fallback}</b>\n" + "\n".join(item.fallback for item in item_pairs),
        ))

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
            header_html = "<tr>" + "".join(
                f"<th>{cell.rich}</th>" for cell in header_pairs
            ) + "</tr>"
        body_html = "".join(
            "<tr>" + "".join(f"<td>{cell.rich}</td>" for cell in row) + "</tr>"
            for row in row_pairs
        )
        table_html = f"{RICH_TABLE_OPEN}<tbody>{header_html}{body_html}</tbody></table>"
        if title_pair:
            table_html = f"<b>{title_pair.rich}</b><br>{table_html}"
        if summary_pair:
            table_html = (
                f"<details><summary>{summary_pair.rich}</summary>"
                f"{table_html}</details>"
            )

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
            rich_html="<br>".join(section.rich for section in self._sections).strip(),
            fallback_html="\n\n".join(
                section.fallback for section in self._sections
            ).strip(),
        )


async def _send_card(
    *,
    bot: Bot,
    chat_id: int,
    card: CardView,
    reply_markup: InlineKeyboardMarkup | None,
) -> types.Message:
    try:
        return await bot.send_rich_message(
            chat_id=chat_id,
            rich_message=card.rich_message,
            reply_markup=reply_markup,
        )
    except TelegramAPIError as error:
        logger.warning("Rich Message send failed, using HTML fallback: %s", error)
        return await bot.send_message(
            chat_id=chat_id,
            text=card.fallback_html,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )


async def present_rich_card(
    *,
    bot: Bot,
    chat_id: int,
    card: CardView,
    reply_markup: InlineKeyboardMarkup | None = None,
    current_message: types.Message | None = None,
) -> types.Message:
    """Edits one card in place and falls back safely when editing is impossible."""
    if current_message:
        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=current_message.message_id,
                rich_message=card.rich_message,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as error:
            if (
                isinstance(error, TelegramBadRequest)
                and "message is not modified" in str(error).lower()
            ):
                return current_message
            logger.info("Rich Message edit failed, using HTML fallback: %s", error)

        try:
            return await bot.edit_message_text(
                chat_id=chat_id,
                message_id=current_message.message_id,
                text=card.fallback_html,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except TelegramAPIError as error:
            logger.info("HTML edit failed, sending a replacement: %s", error)

    sent = await _send_card(
        bot=bot,
        chat_id=chat_id,
        card=card,
        reply_markup=reply_markup,
    )
    if current_message:
        try:
            await current_message.delete()
        except TelegramAPIError:
            logger.debug("Old card could not be deleted", exc_info=True)
    return sent
