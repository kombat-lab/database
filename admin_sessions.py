"""Bind administrative actions to the screen and object the user actually saw."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, TypeAlias

from aiogram import BaseMiddleware, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message

from telegram_helpers import get_bound_bot, get_callback_message
from admin_commands import admin_transition
from telegram_text import split_html
from utils import escape_html

ScreenValue: TypeAlias = str | int
_SCREEN_KEY = "admin_screen"
PENDING_SCREEN_KEY = "admin_pending_screen"
_PROTECTED_PREFIXES = frozenset(
    {
        "edit_field_",
        "select_opt_",
        "delete_entity",
        "res_type_",
        "card_slot_",
        "optional_note_skip",
        "catalog_create_back",
        "catalog_duplicate_",
        "isd:",
        "item_sources_open",
        "rc:",
        "recipe_",
        "mob_",
        "edit_mob_",
        "drop_",
        "confirm_mob_delete",
        "back_to_mob_",
    }
)


def tag_admin_keyboard(markup: InlineKeyboardMarkup | None, token: str) -> InlineKeyboardMarkup | None:
    if markup is None:
        return None
    rows = []
    for row in markup.inline_keyboard:
        buttons = []
        for button in row:
            payload = button.callback_data
            if payload is not None and not payload.startswith("gw:"):
                payload = f"{payload.split('~', 1)[0]}~{token}"
                if len(payload.encode("utf-8")) > 64:
                    raise ValueError("Administrative callback exceeds 64 bytes")
                button = button.model_copy(update={"callback_data": payload})
            buttons.append(button)
        rows.append(buttons)
    return markup.model_copy(update={"inline_keyboard": rows})


async def remember_admin_screen(
    state: FSMContext,
    event: Message | types.CallbackQuery,
    sent_message: Message,
    token: str,
    context: Mapping[str, ScreenValue] | None = None,
) -> None:
    user = event.from_user
    if user is None:
        raise ValueError("An administrative screen requires a user")
    data = await state.get_data()
    data[_SCREEN_KEY] = {
        "token": token,
        "user_id": user.id,
        "chat_id": sent_message.chat.id,
        "message_id": sent_message.message_id,
        "context": dict(context or {}),
    }
    data.pop(PENDING_SCREEN_KEY, None)
    await state.set_data(data)


class AdminScreenMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, types.CallbackQuery) or event.data is None:
            return await handler(event, data)
        payload, separator, token = event.data.rpartition("~")
        if not separator:
            if any(event.data.startswith(prefix) for prefix in _PROTECTED_PREFIXES):
                await event.answer("Экран устарел. Откройте предмет заново.", show_alert=True)
                return None
            return await handler(event, data)
        state = data.get("state")
        if not isinstance(state, FSMContext):
            await event.answer("Откройте админку заново.", show_alert=True)
            return None
        state_data = await state.get_data()
        screen = state_data.get(_SCREEN_KEY)
        message = event.message
        valid = (
            isinstance(screen, dict)
            and isinstance(message, Message)
            and bool(token)
            and screen.get("token") == token
            and screen.get("user_id") == event.from_user.id
            and screen.get("chat_id") == message.chat.id
            and screen.get("message_id") == message.message_id
            and isinstance(screen.get("context"), dict)
            and all(state_data.get(key) == value for key, value in screen["context"].items())
        )
        if not valid:
            await event.answer("Экран устарел. Откройте предмет заново.", show_alert=True)
            return None
        clean_event = event.model_copy(update={"data": payload})
        # Filters and handlers keep their original callback vocabulary. Only this
        # outer middleware accepts the full bound screen address.
        data["admin_screen_validated"] = True
        return await handler(clean_event, data)


async def validate_admin_input(message: Message, state: FSMContext) -> bool:
    state_data = await state.get_data()
    screen = state_data.get(_SCREEN_KEY)
    if not isinstance(screen, dict) or message.from_user is None:
        await message.answer("Экран редактирования устарел. Откройте предмет заново.")
        return False
    pending = state_data.get(PENDING_SCREEN_KEY)
    if pending is not None:
        previous_id = pending.get("previous_message_id") if isinstance(pending, dict) else None
        if (
            type(previous_id) is not int
            or message.reply_to_message is None
            or message.reply_to_message.message_id != previous_id
        ):
            await message.answer(
                "Новый экран не подтверждён. Ответьте через Reply на предыдущее подтверждённое "
                "приглашение, если оно сохранилось отдельным сообщением, или откройте /kombat заново."
            )
            return False
    context = screen.get("context")
    valid = (
        screen.get("user_id") == message.from_user.id
        and screen.get("chat_id") == message.chat.id
        and isinstance(context, dict)
        and all(state_data.get(key) == value for key, value in context.items())
        and (message.reply_to_message is None or message.reply_to_message.message_id == screen.get("message_id"))
    )
    if not valid:
        await message.answer("Этот ответ относится к другому экрану. Откройте предмет заново.")
    return valid


@asynccontextmanager
async def pending_screen_delivery(
    state: FSMContext, target: Message | types.CallbackQuery, token: str
) -> AsyncIterator[None]:
    """Keep ambiguous/newly delivered prompts from authorizing stale free text.

    The marker is durable before transport. Only a definite Telegram rejection
    restores it; accepted delivery and storage failure leave inputs blocked
    until the screen, payload and next state commit together.
    """
    previous = await state.get_data()
    old_screen = previous.get(_SCREEN_KEY)
    previous_id = old_screen.get("message_id") if isinstance(old_screen, dict) and isinstance(target, Message) else None
    await state.update_data(**{PENDING_SCREEN_KEY: {"token": token, "previous_message_id": previous_id}})
    try:
        yield
    except TelegramBadRequest:
        await state.set_data(previous)
        raise


async def present_admin_text(
    target: Message | types.CallbackQuery,
    state: FSMContext,
    text: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    context: Mapping[str, ScreenValue] | None = None,
    parse_mode: str | None = None,
    commit: Callable[[], Awaitable[None]] | None = None,
) -> Message:
    """Present and bind a plain screen; render oversized HTML through safe delivery."""
    from messaging import cleanup_card_fragments, upsert_rich_card

    token = secrets.token_hex(4)
    tagged = tag_admin_keyboard(keyboard, token)
    message = get_callback_message(target) if isinstance(target, types.CallbackQuery) else target
    safe_html = text if parse_mode == "HTML" else escape_html(text)
    async with pending_screen_delivery(state, target, token):
        if len(split_html(safe_html)) > 1:
            from aiogram.types import InputRichMessage

            sent = await upsert_rich_card(
                bot=get_bound_bot(target),
                chat_id=message.chat.id,
                rich_message=InputRichMessage(html=safe_html),
                plain_text=safe_html,
                reply_markup=tagged,
                current_message=message if isinstance(target, types.CallbackQuery) else None,
                message_thread_id=message.message_thread_id,
            )
        elif isinstance(target, types.CallbackQuery):
            try:
                result = await message.edit_text(text, reply_markup=tagged, parse_mode=parse_mode)
                sent = result if isinstance(result, Message) else message
            except TelegramBadRequest as error:
                if "message is not modified" not in error.message.lower():
                    raise
                sent = message
            await cleanup_card_fragments(get_bound_bot(target), message.chat.id, message.message_id)
        else:
            sent = await message.answer(text, reply_markup=tagged, parse_mode=parse_mode)
    # Delivery can succeed before storage fails. Publish the new token only
    # together with the payload and next state it represents. A partial commit
    # must never authorize a visible button against the previous selection.
    async with admin_transition(state):
        if commit is not None:
            await commit()
        await remember_admin_screen(state, target, sent, token, context)
    return sent


async def present_admin_rich(
    target: Message | types.CallbackQuery,
    state: FSMContext,
    rich_html: str,
    fallback_html: str,
    keyboard: InlineKeyboardMarkup | None = None,
    *,
    context: Mapping[str, ScreenValue] | None = None,
    commit: Callable[[], Awaitable[None]] | None = None,
) -> Message:
    from ui.rich import CardView, present_rich_card

    token = secrets.token_hex(4)
    message = get_callback_message(target) if isinstance(target, types.CallbackQuery) else target
    async with pending_screen_delivery(state, target, token):
        sent = await present_rich_card(
            bot=get_bound_bot(target),
            chat_id=message.chat.id,
            current_message=message if isinstance(target, types.CallbackQuery) else None,
            card=CardView(rich_html, fallback_html),
            reply_markup=tag_admin_keyboard(keyboard, token),
        )
    # Delivery can succeed before storage fails. Publish the new token only
    # together with the payload and next state it represents. A partial commit
    # must never authorize a visible button against the previous selection.
    async with admin_transition(state):
        if commit is not None:
            await commit()
        await remember_admin_screen(state, target, sent, token, context)
    return sent
