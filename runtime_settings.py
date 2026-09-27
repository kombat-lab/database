"""Validated deployment configuration; no environment reads in domain code."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppSettings:
    bot_token: str = field(repr=False)
    database_path: str
    admin_ids: frozenset[int] = frozenset()
    fsm_ttl_seconds: int = 30 * 86400
    allow_empty_database: bool = False
    concurrency_limit: int = 64

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "AppSettings":
        env = os.environ if environment is None else environment
        token = env.get("BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("BOT_TOKEN not set")
        ids: set[int] = set()
        for value in env.get("ADMIN_ID", "").split(","):
            value = value.strip()
            if not value:
                continue
            if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 2**63 - 1:
                raise ValueError("ADMIN_ID must contain positive Telegram IDs separated by commas")
            ids.add(int(value))
        raw_path = env.get("DATABASE_PATH", "").strip()
        path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().parent / "game.db"
        if not path.is_absolute():
            path = Path(__file__).resolve().parent / path

        def integer(key: str, default: int, minimum: int, maximum: int) -> int:
            raw = env.get(key, str(default))
            if not raw.isascii() or not raw.isdecimal() or not minimum <= int(raw) <= maximum:
                raise ValueError(f"{key} must be an integer in [{minimum}, {maximum}]")
            return int(raw)

        allow_empty = env.get("ALLOW_EMPTY_DATABASE", "false").strip().lower()
        if allow_empty not in ("true", "false"):
            raise ValueError("ALLOW_EMPTY_DATABASE must be true or false")
        return cls(
            bot_token=token,
            database_path=str(path.resolve()),
            admin_ids=frozenset(ids),
            fsm_ttl_seconds=integer("FSM_TTL_DAYS", 30, 1, 365) * 86400,
            allow_empty_database=allow_empty == "true",
            concurrency_limit=integer("UPDATE_CONCURRENCY", 64, 1, 256),
        )
