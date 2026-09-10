"""Send catalog cards without deleting the last usable navigation on failure."""

import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InputRichMessage, Message

from telegram_text import split_html

logger = logging.getLogger(__name__)

# UI housekeeping only: Telegram deletion is limited to recent messages, and
# older/restarted sessions may retain harmless message history. No message text,
# Telegram objects, or user profile data is kept here.
_FRAGMENT_TTL_SECONDS = 48 * 60 * 60
_MAX_TRACKED_CARDS = 512
_MAX_TRACKED_FRAGMENT_IDS = 4096


@dataclass(frozen=True, slots=True)
class _CardFragments:
    message_ids: tuple[int, ...]
    created_at: float


_card_fragments: OrderedDict[tuple[int, int, int], _CardFragments] = OrderedDict()


def _prune_fragments() -> None:
    cutoff = time.monotonic() - _FRAGMENT_TTL_SECONDS
    for key, record in tuple(_card_fragments.items()):
        if record.created_at < cutoff:
            del _card_fragments[key]
    while _card_fragments and (
        len(_card_fragments) > _MAX_TRACKED_CARDS
        or sum(len(record.message_ids) for record in _card_fragments.values()) > _MAX_TRACKED_FRAGMENT_IDS
    ):
        _card_fragments.popitem(last=False)


def _remember_fragments(bot: Bot, chat_id: int, message_id: int, extras: Sequence[int]) -> None:
    if not extras:
        return
    # Even one exceptionally large card must not defeat the metadata bound.
    ids = tuple(fragment for fragment in extras if fragment != message_id)[-_MAX_TRACKED_FRAGMENT_IDS:]
    if not ids:
        return
    key = (bot.id, chat_id, message_id)
    _card_fragments[key] = _CardFragments(ids, time.monotonic())
    _card_fragments.move_to_end(key)
    _prune_fragments()


async def _delete_confirmed_messages(bot: Bot, chat_id: int, message_ids: Sequence[int]) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            logger.warning("Could not remove a confirmed card fragment", exc_info=True)


async def cleanup_card_fragments(bot: Bot, chat_id: int, message_id: int) -> None:
    """Best-effort removal of extra parts after replacing the anchor's content.

    The anchor message_id itself is never deleted. Call only after the new
    screen has been successfully sent or edited, to preserve the old card on
    delivery failures. Evicted/expired metadata simply leaves harmless history.
    """
    _prune_fragments()
    record = _card_fragments.pop((bot.id, chat_id, message_id), None)
    if record is not None:
        await _delete_confirmed_messages(bot, chat_id, record.message_ids)



def _not_modified(error: TelegramBadRequest) -> bool:
    return "message is not modified" in error.message.lower()


async def _delete_old(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        logger.warning("Could not remove the previous card", exc_info=True)


async def _send_card(
    *,
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    plain_text: str,
    reply_markup: InlineKeyboardMarkup | None,
    message_thread_id: int | None,
) -> Message:
    try:
        return await bot.send_rich_message(
            chat_id=chat_id, rich_message=rich_message, reply_markup=reply_markup,
            message_thread_id=message_thread_id,
        )
    except TelegramBadRequest as error:
        # A rejected format is safe to retry as text. Network errors are ambiguous:
        # Telegram may already have accepted the message. Do not duplicate it.
        logger.info("Rich card rejected; using text with explicit entities: %s", error)
    chunks = split_html(plain_text)
    if not chunks:
        raise ValueError("A card must contain text")
    sent: Message | None = None
    confirmed_ids: list[int] = []
    try:
        for index, chunk in enumerate(chunks):
            sent = await bot.send_message(
                chat_id=chat_id, text=chunk.text, entities=list(chunk.entities), parse_mode=None,
                reply_markup=reply_markup if index == len(chunks) - 1 else None,
                message_thread_id=message_thread_id,
            )
            confirmed_ids.append(sent.message_id)
    except BaseException:
        # A failed/ambiguous request supplies no message ID. Only earlier parts
        # whose successful responses were received are safe to remove.
        await _delete_confirmed_messages(bot, chat_id, confirmed_ids)
        raise
    assert sent is not None
    _remember_fragments(bot, chat_id, sent.message_id, confirmed_ids[:-1])
    return sent


async def upsert_rich_card(
    *,
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    plain_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    current_message: Message | None = None,
    message_thread_id: int | None = None,
) -> Message:
    if current_message is not None:
        message_thread_id = current_message.message_thread_id
        try:
            edited = await bot.edit_message_text(
                chat_id=chat_id, message_id=current_message.message_id,
                rich_message=rich_message, reply_markup=reply_markup,
            )
            await cleanup_card_fragments(bot, chat_id, current_message.message_id)
            return edited if isinstance(edited, Message) else current_message
        except TelegramBadRequest as error:
            if _not_modified(error):
                await cleanup_card_fragments(bot, chat_id, current_message.message_id)
                return current_message
        chunks = split_html(plain_text)
        if len(chunks) == 1:
            try:
                edited = await bot.edit_message_text(
                    chat_id=chat_id, message_id=current_message.message_id,
                    text=chunks[0].text, entities=list(chunks[0].entities), parse_mode=None,
                    reply_markup=reply_markup,
                )
                await cleanup_card_fragments(bot, chat_id, current_message.message_id)
                return edited if isinstance(edited, Message) else current_message
            except TelegramBadRequest as error:
                if _not_modified(error):
                    await cleanup_card_fragments(bot, chat_id, current_message.message_id)
                    return current_message
    sent = await _send_card(
        bot=bot, chat_id=chat_id, rich_message=rich_message, plain_text=plain_text,
        reply_markup=reply_markup, message_thread_id=message_thread_id,
    )
    if current_message is not None:
        await cleanup_card_fragments(bot, chat_id, current_message.message_id)
        await _delete_old(current_message)
    return sent


async def replace_rich_card(
    *,
    bot: Bot,
    chat_id: int,
    rich_message: InputRichMessage,
    plain_text: str,
    reply_markup: InlineKeyboardMarkup | None,
    current_message: Message,
) -> Message:
    """Use a fresh message for client layout, preserving the old one until success."""
    sent = await _send_card(
        bot=bot, chat_id=chat_id, rich_message=rich_message, plain_text=plain_text,
        reply_markup=reply_markup, message_thread_id=current_message.message_thread_id,
    )
    await cleanup_card_fragments(bot, chat_id, current_message.message_id)
    await _delete_old(current_message)
    return sent
