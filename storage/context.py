"""Operation attribution independent of transport and repository layers."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import secrets


@dataclass(frozen=True, slots=True)
class OperationContext:
    actor_user_id: int | None
    operation_id: str
    source: str


_current: ContextVar[OperationContext | None] = ContextVar("catalog_operation", default=None)


def current_operation() -> OperationContext:
    return _current.get() or OperationContext(None, secrets.token_hex(16), "application")


@contextmanager
def catalog_operation(
    *, actor_user_id: int | None = None, operation_id: str | None = None, source: str = "application"
) -> Iterator[OperationContext]:
    if actor_user_id is not None and (
        isinstance(actor_user_id, bool) or not isinstance(actor_user_id, int) or actor_user_id <= 0
    ):
        raise ValueError("actor_user_id must be a positive integer")
    if not isinstance(source, str) or not 1 <= len(source) <= 128:
        raise ValueError("source must contain 1..128 characters")
    identifier = operation_id if operation_id is not None else secrets.token_hex(16)
    if not isinstance(identifier, str) or not 1 <= len(identifier) <= 256:
        raise ValueError("operation_id must contain 1..256 characters")
    value = OperationContext(actor_user_id, identifier, source)
    token = _current.set(value)
    try:
        yield value
    finally:
        _current.reset(token)
