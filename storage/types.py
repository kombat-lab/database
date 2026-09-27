"""Validated primitives at the SQLite boundary; no dynamic catalog values."""

from collections.abc import Mapping
from typing import TypeAlias

SCHEMA_VERSION = 3
SqlValue: TypeAlias = int | float | str | bytes | None
SqlParams: TypeAlias = tuple[SqlValue, ...]
DbRow: TypeAlias = dict[str, SqlValue]


def sql_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Expected an integer catalog column")
    return value


def sql_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Expected a text catalog column")
    return value


def sql_optional_int(value: object) -> int | None:
    return None if value is None else sql_int(value)


def sql_optional_text(value: object) -> str | None:
    return None if value is None else sql_text(value)


def sql_row(value: Mapping[str, object]) -> DbRow:
    result: DbRow = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or isinstance(item, bool)
            or (item is not None and not isinstance(item, (int, float, str, bytes)))
        ):
            raise ValueError("Unexpected SQLite row value")
        result[key] = item
    return result
