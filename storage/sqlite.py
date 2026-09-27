"""Cancellation-safe SQLite unit of work shared by catalog repositories."""

from __future__ import annotations
import asyncio
import logging
import os
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass
from time import perf_counter
from typing import TypeVar
import aiosqlite
from catalog_types import LocationRow
from recipe_domain import normalize_identity
from storage.context import OperationContext, current_operation
from storage.types import sql_int, DbRow, SqlParams, SCHEMA_VERSION, sql_row

logger = logging.getLogger(__name__)
DB_PATH = os.getenv("DATABASE_PATH", "game.db")
_T = TypeVar("_T")


@dataclass(slots=True)
class SqlMetrics:
    counter: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0

    def record(self, seconds: float) -> None:
        self.counter += 1
        self.total_seconds += seconds
        self.max_seconds = max(self.max_seconds, seconds)


def _lower_unicode(value: str | None) -> str | None:
    return value.lower() if value is not None else None


class SqliteStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or DB_PATH
        self.metrics = SqlMetrics()
        self._conn: aiosqlite.Connection | None = None
        self._locations_cache: dict[int, LocationRow] = {}
        self._connection_lock = asyncio.Lock()
        self._connection_lock_owner: asyncio.Task[object] | None = None
        self._connection_lock_depth = 0
        self._transaction_depth = 0
        self._operation_context: OperationContext = current_operation()

    @asynccontextmanager
    async def _connection_guard(self) -> AsyncIterator[None]:
        """Serialize work on the shared connection while allowing nested DB calls."""
        task = asyncio.current_task()
        if self._connection_lock_owner is task:
            self._connection_lock_depth += 1
            try:
                yield
            finally:
                self._connection_lock_depth -= 1
            return

        await self._connection_lock.acquire()
        self._connection_lock_owner = task
        self._operation_context = current_operation()
        self._connection_lock_depth = 1
        try:
            yield
        finally:
            self._connection_lock_depth = 0
            self._connection_lock_owner = None
            self._connection_lock.release()

    @staticmethod
    async def _finish_sqlite(operation: Awaitable[_T]) -> tuple[_T, bool]:
        """Drain a queued SQLite operation before the connection lock is released.

        aiosqlite cannot stop SQL already running on its worker thread. Shielding
        and draining also handles repeated cancellation during rollback/close.
        """
        pending = asyncio.ensure_future(operation)
        cancelled = False
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                cancelled = True
        return pending.result(), cancelled

    def _require_connection(self) -> aiosqlite.Connection:
        connection = self._conn
        if connection is None:
            raise RuntimeError("Database is not connected")
        return connection

    async def _discard_connection(self) -> None:
        connection = self._conn
        self._conn = None
        if connection is not None:
            await self._finish_sqlite(connection.close())

    async def _rollback_safely(self) -> None:
        if self._conn is not None:
            try:
                await self._finish_sqlite(self._conn.rollback())
            except BaseException:
                # A failed rollback makes the connection unsafe for later work.
                await self._discard_connection()
                raise

    async def connect(self) -> None:
        async with self._connection_guard():
            if self._conn is not None:
                return
            if self._transaction_depth:
                raise RuntimeError("Cannot reconnect inside an unfinished transaction")
            try:
                connection, cancelled = await self._finish_sqlite(aiosqlite.connect(self.path, timeout=30.0))
                self._conn = connection
                if cancelled:
                    raise asyncio.CancelledError
                connection.row_factory = aiosqlite.Row
                await connection.execute("PRAGMA foreign_keys = ON")
                version = await self.execute_query("PRAGMA user_version")
                if sql_int(version[0]["user_version"]) > SCHEMA_VERSION:
                    raise RuntimeError("Database schema is newer than this application")
                await connection.execute("PRAGMA journal_mode = WAL")
                await connection.execute("PRAGMA busy_timeout = 30000")
                await connection.create_function("LOWER_UNICODE", 1, _lower_unicode, deterministic=True)
                await connection.create_function("NORMALIZE_IDENTITY", 1, normalize_identity, deterministic=True)
                await connection.create_function("catalog_actor", 0, lambda: self._operation_context.actor_user_id)
                await connection.create_function(
                    "catalog_operation_id", 0, lambda: self._operation_context.operation_id
                )
                await connection.create_function("catalog_source", 0, lambda: self._operation_context.source)
                await self.initialize_schema()
            except BaseException:
                await self._discard_connection()
                raise
            logger.info("Database connected: %s", self.path)

    async def close(self) -> None:
        async with self._connection_guard():
            connection = self._conn
            self._conn = None
            if connection is not None:
                _, cancelled = await self._finish_sqlite(connection.close())
                if cancelled:
                    raise asyncio.CancelledError

    async def execute_query(self, query: str, params: SqlParams = ()) -> list[DbRow]:
        async with self._connection_guard():
            connection = self._require_connection()
            started_at = perf_counter()
            try:
                async with connection.execute(query, params) as cursor:
                    rows = list(await cursor.fetchall())
                    if not query.lstrip().upper().startswith(("SELECT", "PRAGMA")) and self._transaction_depth == 0:
                        await connection.commit()
                    if not rows:
                        return []
                    if not hasattr(rows[0], "keys"):
                        if cursor.description is None:
                            raise RuntimeError("SQL returned rows without column metadata")
                        col_names = [desc[0] for desc in cursor.description]
                        return [sql_row(dict(zip(col_names, row))) for row in rows]
                    return [sql_row(dict(row)) for row in rows]
            except BaseException as e:
                if self._transaction_depth == 0:
                    await self._rollback_safely()
                if not isinstance(e, asyncio.CancelledError):
                    logger.error("[DB ERROR] %s", e)
                raise
            finally:
                self.metrics.record(perf_counter() - started_at)

    async def execute_insert(self, query: str, params: SqlParams = ()) -> int:
        """Execute one INSERT and return its row id without a concurrency race."""
        async with self._connection_guard():
            connection = self._require_connection()
            started_at = perf_counter()
            try:
                cursor = await connection.execute(query, params)
                try:
                    row_id = cursor.lastrowid
                finally:
                    await cursor.close()
                if self._transaction_depth == 0:
                    await connection.commit()
                if row_id is None:
                    raise RuntimeError("INSERT did not return a row id")
                return row_id
            except BaseException:
                if self._transaction_depth == 0:
                    await self._rollback_safely()
                raise
            finally:
                self.metrics.record(perf_counter() - started_at)

    def transaction(self) -> AbstractAsyncContextManager[None]:
        return self._transaction(immediate=True)

    def read_transaction(self) -> AbstractAsyncContextManager[None]:
        """A consistent read snapshot without reserving the SQLite writer lock."""
        return self._transaction(immediate=False)

    @asynccontextmanager
    async def _transaction(self, *, immediate: bool) -> AsyncIterator[None]:
        """Keep BEGIN, body, commit and cancellation cleanup under one lock."""
        async with self._connection_guard():
            if self._conn is None:
                raise RuntimeError("Database is not connected")
            nested = self._transaction_depth > 0
            savepoint = f"nested_{self._transaction_depth + 1}"
            started = entered = finished = False
            try:
                sql = f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE" if immediate else "BEGIN DEFERRED"
                cursor, cancelled = await self._finish_sqlite(self._conn.execute(sql))
                started = True
                _, close_cancelled = await self._finish_sqlite(cursor.close())
                if cancelled or close_cancelled:
                    raise asyncio.CancelledError
                self._transaction_depth += 1
                entered = True
                yield
                connection = self._require_connection()
                if nested:
                    cursor, cancelled = await self._finish_sqlite(connection.execute(f"RELEASE SAVEPOINT {savepoint}"))
                    finished = True
                    _, close_cancelled = await self._finish_sqlite(cursor.close())
                    cancelled = cancelled or close_cancelled
                else:
                    _, cancelled = await self._finish_sqlite(connection.commit())
                    finished = True
                # Once COMMIT has completed cancellation cannot undo it, but the
                # connection is settled and cannot leak work into another handler.
                if cancelled:
                    raise asyncio.CancelledError
            except BaseException:
                if not finished:
                    if nested and started:
                        try:
                            connection = self._require_connection()
                            for sql in (f"ROLLBACK TO SAVEPOINT {savepoint}", f"RELEASE SAVEPOINT {savepoint}"):
                                cursor, _ = await self._finish_sqlite(connection.execute(sql))
                                await self._finish_sqlite(cursor.close())
                        except BaseException:
                            # The outer caller may catch this failure. Discard the
                            # entire connection so it cannot commit the inner work.
                            await self._discard_connection()
                            raise
                    elif not nested:
                        await self._rollback_safely()
                raise
            finally:
                if entered:
                    self._transaction_depth -= 1

    async def initialize_schema(self) -> None:
        raise NotImplementedError("A schema initializer is required")
