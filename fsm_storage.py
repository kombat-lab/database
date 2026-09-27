"""SQLite-backed conversations and event locks that disappear when unused."""

import asyncio
import json
import math
import time
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseEventIsolation, BaseStorage, StateType, StorageKey

from database import Database
from storage.types import sql_text

MAX_SESSION_BYTES = 512 * 1024


def storage_key(key: StorageKey) -> str:
    # JSON avoids collisions involving optional thread/business/destiny fields.
    return json.dumps(
        [key.bot_id, key.chat_id, key.user_id, key.thread_id, key.business_connection_id, key.destiny],
        separators=(",", ":"),
    )


def checked_json(value: object, depth: int = 0) -> object:
    if depth > 32:
        raise ValueError("FSM data is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, (list, tuple)):
        return [checked_json(item, depth + 1) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: checked_json(item, depth + 1) for key, item in value.items()}
    raise ValueError("FSM data must contain only JSON values with string keys")


def encode_data(data: Mapping[str, object]) -> str:
    encoded = json.dumps(checked_json(dict(data)), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_SESSION_BYTES:
        raise ValueError("FSM session exceeds the storage limit")
    return encoded


class SQLiteFSMStorage(BaseStorage):
    """Persist only nonempty sessions; reads never create empty records.

    Database ownership belongs to the application. FSM close must not close the
    shared connection before accepted update tasks have drained.
    """

    def __init__(
        self, database: Database, *, ttl_seconds: int = 30 * 86400, clock: Callable[[], float] = time.time
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("FSM TTL must be positive")
        self.database = database
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self._last_prune = float("-inf")

    async def _read(self, key: StorageKey) -> tuple[str | None, dict[str, Any]]:
        rows = await self.database.execute_query(
            "SELECT state,data_json FROM fsm_sessions WHERE key=? AND expires_at>?",
            (storage_key(key), self.clock()),
        )
        if not rows:
            return None, {}
        raw = rows[0]
        encoded = sql_text(raw["data_json"])
        if len(encoded.encode("utf-8")) > MAX_SESSION_BYTES:
            raise ValueError("Stored FSM session exceeds the storage limit")
        value: object = json.loads(encoded)
        if not isinstance(value, dict) or not all(isinstance(name, str) for name in value):
            raise ValueError("Stored FSM data must be a JSON object")
        checked_json(value)
        state = raw["state"]
        if state is not None and not isinstance(state, str):
            raise ValueError("Stored FSM state must be text")
        return state, dict(value)

    async def _write(self, key: StorageKey, state: str | None, data: Mapping[str, Any]) -> None:
        now = self.clock()
        if now - self._last_prune >= 60:
            # Only expiring conversation sessions, never catalog/audit/history.
            await self.prune()
            self._last_prune = now
        if state is None and not data:
            await self.database.execute_query("DELETE FROM fsm_sessions WHERE key=?", (storage_key(key),))
            return
        encoded = encode_data(data)
        await self.database.execute_query(
            "INSERT INTO fsm_sessions(key,state,data_json,expires_at) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET state=excluded.state,data_json=excluded.data_json,expires_at=excluded.expires_at",
            (storage_key(key), state, encoded, self.clock() + self.ttl_seconds),
        )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        name = state.state if isinstance(state, State) else state
        async with self.database.transaction():
            _, data = await self._read(key)
            await self._write(key, name, data)

    async def get_state(self, key: StorageKey) -> str | None:
        return (await self._read(key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        encode_data(data)  # Reject invalid data before starting a write transaction.
        async with self.database.transaction():
            state, _ = await self._read(key)
            await self._write(key, state, data)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return (await self._read(key))[1]

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        encode_data(data)
        async with self.database.transaction():
            state, current = await self._read(key)
            current.update(data)
            await self._write(key, state, current)
            return current

    async def prune(self, *, limit: int = 500) -> int:
        if not 1 <= limit <= 10000:
            raise ValueError("Cleanup batch must be between 1 and 10000")
        async with self.database.transaction():
            rows = await self.database.execute_query(
                "DELETE FROM fsm_sessions WHERE key IN "
                "(SELECT key FROM fsm_sessions WHERE expires_at<=? ORDER BY expires_at LIMIT ?) RETURNING key",
                (self.clock(), limit),
            )
            return len(rows)

    async def close(self) -> None:
        pass


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ScopedEventIsolation(BaseEventIsolation):
    """Reference-count both holders and waiters, including cancelled waiters."""

    def __init__(self) -> None:
        self._entries: dict[StorageKey, _LockEntry] = {}
        self._closed = False

    @property
    def active_keys(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def lock(self, key: StorageKey) -> AsyncGenerator[None, None]:
        if self._closed:
            raise RuntimeError("Event isolation is closed")
        entry = self._entries.setdefault(key, _LockEntry())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._entries[key]

    async def close(self) -> None:
        self._closed = True
        # Active holders/waiters remove their entries in finally; clearing here
        # would permit a second lock for a key while the first one is in use.
