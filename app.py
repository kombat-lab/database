"""Composition root: one independently configured bot application per instance."""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher

from analytics import AnalyticsMiddleware, AnalyticsService
from database import Database
from fsm_storage import SQLiteFSMStorage, ScopedEventIsolation
from lifecycle import BackgroundTaskRegistry, DrainingDispatcher, UpdateTaskTracker, install_update_tracker
from runtime_scope import CatalogAuditMiddleware, RuntimeScope
from runtime_settings import AppSettings
from routing import CallbackMessageGuard


@dataclass(slots=True)
class Application:
    settings: AppSettings
    database: Database
    dispatcher: Dispatcher
    storage: SQLiteFSMStorage
    update_tasks: UpdateTaskTracker
    background_tasks: BackgroundTaskRegistry

    async def run(self, bot: Bot) -> None:
        try:
            await self.database.connect()
            me = await bot.me()
            from admin_handlers import create_admin_router
            from public_catalog import create_public_router
            from public_presentation import PublicContext

            scope = RuntimeScope(self.database, self.settings.admin_ids)
            public_router = create_public_router(
                PublicContext(
                    db=self.database,
                    bot_username=me.username,
                    analytics=AnalyticsService(self.database),
                    background_tasks=self.background_tasks,
                )
            )
            self.dispatcher.include_router(public_router)
            self.dispatcher.include_router(create_admin_router(scope))
            await bot.delete_webhook(drop_pending_updates=False)
            await self.dispatcher.start_polling(
                bot,
                close_bot_session=False,
                tasks_concurrency_limit=self.settings.concurrency_limit,
            )
        finally:
            try:
                await self.update_tasks.close(timeout=30.0)
            finally:
                try:
                    await self.background_tasks.close(timeout=2.0)
                finally:
                    try:
                        await self.dispatcher.fsm.close()
                    finally:
                        try:
                            await self.database.close()
                        finally:
                            await bot.session.close()


def create_application(settings: AppSettings, database: Database | None = None) -> Application:
    if database is None:
        path = Path(settings.database_path)
        if not settings.allow_empty_database and not path.is_file():
            raise ValueError(
                "Database file does not exist; check DATABASE_PATH or explicitly enable ALLOW_EMPTY_DATABASE"
            )
        database = Database(settings.database_path)
    storage = SQLiteFSMStorage(database, ttl_seconds=settings.fsm_ttl_seconds)
    tracker = UpdateTaskTracker()
    dispatcher = DrainingDispatcher(tracker, storage=storage, events_isolation=ScopedEventIsolation())
    background = BackgroundTaskRegistry()
    install_update_tracker(dispatcher, tracker)
    dispatcher.callback_query.outer_middleware(CallbackMessageGuard())
    dispatcher.update.outer_middleware(CatalogAuditMiddleware())
    dispatcher.update.middleware(AnalyticsMiddleware(database))
    return Application(settings, database, dispatcher, storage, tracker, background)


async def main() -> None:
    settings = AppSettings.from_env()
    logging.basicConfig(level=logging.INFO)
    application = create_application(settings)
    bot = Bot(token=settings.bot_token)
    await application.run(bot)


if __name__ == "__main__":
    asyncio.run(main())
