"""Per-application dependencies bound while administrative handlers run."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
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


def current_scope() -> RuntimeScope:
    scope = _current.get()
    if scope is None:
        raise RuntimeError("RuntimeScope is not bound to the current task")
    return scope


def database_for() -> Database:
    """Return the database bound to the current application task."""
    return current_scope().database


@contextmanager
def use_runtime_scope(scope: RuntimeScope) -> Iterator[None]:
    """Bind explicit application dependencies around direct service calls."""
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


class RuntimeScopeMiddleware(BaseMiddleware):
    def __init__(self, scope: RuntimeScope) -> None:
        self.scope = scope

    async def __call__(
        self,
        handler: Callable[[types.TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: types.TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        with use_runtime_scope(self.scope):
            return await handler(event, data)


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
