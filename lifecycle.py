"""Track accepted updates and background work until shared resources can close."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any, TypeVar

from aiogram import BaseMiddleware, Dispatcher
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)
T = TypeVar("T")


async def _finish_despite_cancellation(task: asyncio.Task[None]) -> None:
    """Defer caller cancellation until cleanup has actually completed."""
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


class _TaskRegistry:
    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._accepting = True
        self._close_task: asyncio.Task[None] | None = None

    @property
    def active_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def close(self, timeout: float = 30.0) -> None:
        """Stop acceptance, drain, then cancel overdue work and await its cleanup.

        The timeout bounds the grace period, not cancellation cleanup: database
        operations must finish rolling back before their connection is closed.
        """
        if timeout < 0:
            raise ValueError("Shutdown timeout must be nonnegative")
        if asyncio.current_task() in self._tasks:
            raise RuntimeError("A tracked task cannot close its own registry")
        if self._close_task is None:
            self._accepting = False
            self._close_task = asyncio.create_task(self._drain(timeout))
        await _finish_despite_cancellation(self._close_task)

    async def _drain(self, timeout: float) -> None:
        tasks = set(self._tasks)
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            logger.warning("Cancelling %d tasks after shutdown grace period", len(pending))
        await asyncio.gather(*tasks, return_exceptions=True)


class UpdateTaskTracker(_TaskRegistry, BaseMiddleware):
    """Track the complete update, including waiting for its FSM isolation lock."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._accepting:
            return None
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Update processing requires an asyncio task")
        self._tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self._tasks.discard(task)


def install_update_tracker(dispatcher: Dispatcher, tracker: UpdateTaskTracker) -> None:
    """Install before FSM isolation using aiogram's public middleware API.

    Call once, before polling starts. Reinsert FSM only when it was enabled.
    """
    try:
        dispatcher.update.outer_middleware.unregister(dispatcher.fsm)
    except ValueError:
        dispatcher.update.outer_middleware(tracker)
    else:
        dispatcher.update.outer_middleware(tracker)
        dispatcher.update.outer_middleware(dispatcher.fsm)


class BackgroundTaskRegistry(_TaskRegistry):
    def create_task(self, coroutine: Coroutine[Any, Any, T], *, name: str | None = None) -> asyncio.Task[T]:
        if not self._accepting:
            coroutine.close()
            raise RuntimeError("Background task registry is closed")
        task = asyncio.create_task(coroutine, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._completed)
        return task

    def _completed(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(
                "Background task failed",
                exc_info=(type(error), error, error.__traceback__),
            )


class DrainingDispatcher(Dispatcher):
    """Drain accepted updates before aiogram's automatic FSM shutdown callback."""

    def __init__(
        self, tracker: UpdateTaskTracker, *, storage: BaseStorage, events_isolation: BaseEventIsolation
    ) -> None:
        super().__init__(storage=storage, events_isolation=events_isolation)
        self.update_tracker = tracker

    async def emit_shutdown(self, *args: Any, **kwargs: Any) -> None:
        # start_polling invokes shutdown before returning to the composition
        # root. Closing FSM first would reject accepted tasks still entering
        # isolation; the public lifecycle hook keeps the ordering explicit.
        await self.update_tracker.close(timeout=30.0)
        await super().emit_shutdown(*args, **kwargs)
