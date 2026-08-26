from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


ENTITY_TYPES = frozenset({"mob", "resource", "gear", "card"})


@dataclass(frozen=True)
class EntityRef:
    entity_type: str
    entity_id: int

    @property
    def is_valid(self) -> bool:
        return self.entity_type in ENTITY_TYPES and self.entity_id > 0


NavigationKey = tuple[int, int, int]


@dataclass
class NavigationSession:
    history: list[EntityRef]
    root_state: Any = None


class EntityNavigationHistory:
    """Bounded in-memory browser history for one interactive bot message."""

    def __init__(self, *, max_sessions: int = 512, max_depth: int = 20):
        self.max_sessions = max_sessions
        self.max_depth = max_depth
        self._sessions: OrderedDict[NavigationKey, NavigationSession] = OrderedDict()

    def visit(
        self,
        key: NavigationKey,
        source: EntityRef,
        target: EntityRef,
        *,
        root_state: Any = None,
    ) -> None:
        if not source.is_valid or not target.is_valid:
            raise ValueError("Invalid entity navigation target")

        session = self._sessions.get(key)
        if not session or session.history[-1] != source:
            session = NavigationSession([source], root_state)
            self._sessions[key] = session

        if session.history[-1] != target:
            session.history.append(target)
            if len(session.history) > self.max_depth:
                session.history.pop(1)

        self._touch(key)

    def previous(self, key: NavigationKey) -> EntityRef | None:
        session = self._sessions.get(key)
        if not session or len(session.history) < 2:
            return None
        self._touch(key)
        return session.history[-2]

    def back(self, key: NavigationKey) -> EntityRef | None:
        session = self._sessions.get(key)
        if not session or len(session.history) < 2:
            return None
        session.history.pop()
        self._touch(key)
        return session.history[-1]

    def root_state(self, key: NavigationKey) -> Any:
        session = self._sessions.get(key)
        if not session:
            return None
        self._touch(key)
        return session.root_state

    def transfer(self, old_key: NavigationKey, new_key: NavigationKey) -> None:
        if old_key == new_key:
            return
        session = self._sessions.pop(old_key, None)
        if session:
            self._sessions[new_key] = session
            self._touch(new_key)

    def clear(self, key: NavigationKey) -> None:
        self._sessions.pop(key, None)

    def _touch(self, key: NavigationKey) -> None:
        self._sessions.move_to_end(key)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
