"""Shared checks for callbacks that require an accessible chat message."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class CallbackMessageGuard(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, CallbackQuery) and not isinstance(event.message, Message):
            await event.answer("Сообщение недоступно. Открой меню заново.", show_alert=True)
            return None
        return await handler(event, data)
