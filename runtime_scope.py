"""Per-application dependencies for legacy adapters and administrative handlers.

New services receive Database explicitly. This scope confines compatibility
functions to one application without mutating module globals.
"""

from collections.abc import Awaitable, Callable, Collection
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware, Bot, types

from database import Database


@dataclass(frozen=True, slots=True)
class RuntimeScope:
    database: Database
    admin_ids: frozenset[int]


_current: ContextVar[RuntimeScope | None] = ContextVar("application_scope", default=None)


def database_for(fallback: Database) -> Database:
    scope = _current.get()
    return fallback if scope is None else scope.database


def admin_ids_for(fallback: Collection[int]) -> Collection[int]:
    scope = _current.get()
    return fallback if scope is None else scope.admin_ids


class RuntimeScopeMiddleware(BaseMiddleware):
    def __init__(self, scope: RuntimeScope) -> None:
        self.scope = scope

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        token = _current.set(self.scope)
        try:
            return await handler(event, data)
        finally:
            _current.reset(token)


class CatalogAuditMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from storage.context import catalog_operation

        user = data.get("event_from_user")
        actor = user.id if isinstance(user, types.User) else None
        bot = data.get("bot")
        bot_id = bot.id if isinstance(bot, Bot) else 0
        operation_id = f"telegram:{bot_id}:{event.update_id}" if isinstance(event, types.Update) else None
        with catalog_operation(actor_user_id=actor, operation_id=operation_id, source="telegram"):
            return await handler(event, data)
