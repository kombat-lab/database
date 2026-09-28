from aiogram import Bot, Router, types
from aiogram.fsm.context import FSMContext

from admin_handlers import create_admin_router
from database import Database
from runtime_scope import RuntimeScope, use_runtime_scope


def create_test_scope(database: Database, *admin_ids: int) -> RuntimeScope:
    return RuntimeScope(database, frozenset(admin_ids))


def create_test_admin_router(database: Database, *admin_ids: int) -> Router:
    """Build a fresh admin router with isolated application dependencies."""
    return create_admin_router(create_test_scope(database, *admin_ids))


async def propagate_admin_event(
    router: Router,
    event: types.TelegramObject,
    *,
    bot: Bot,
    state: FSMContext,
    user: types.User,
) -> object:
    kind = "callback_query" if isinstance(event, types.CallbackQuery) else "message"
    return await router.propagate_event(
        kind,
        event,
        bot=bot,
        state=state,
        raw_state=await state.get_state(),
        event_from_user=user,
    )


__all__ = [
    "RuntimeScope",
    "create_test_admin_router",
    "create_test_scope",
    "propagate_admin_event",
    "use_runtime_scope",
]
