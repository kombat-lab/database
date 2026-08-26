from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass


ENTITY_TYPES = frozenset({"mob", "resource", "gear", "card"})


@dataclass(frozen=True)
class EntityRef:
    entity_type: str
    entity_id: int

    @property
    def is_valid(self) -> bool:
        return self.entity_type in ENTITY_TYPES and self.entity_id > 0


NavigationKey = tuple[int, int, int]


class EntityNavigationHistory:
    """Bounded in-memory browser history for one interactive bot message."""

    def __init__(self, *, max_sessions: int = 512, max_depth: int = 20):
        self.max_sessions = max_sessions
        self.max_depth = max_depth
        self._sessions: OrderedDict[NavigationKey, list[EntityRef]] = OrderedDict()

    def visit(
        self,
        key: NavigationKey,
        source: EntityRef,
        target: EntityRef,
    ) -> None:
        if not source.is_valid or not target.is_valid:
            raise ValueError("Invalid entity navigation target")

        history = self._sessions.get(key)
        if not history or history[-1] != source:
            history = [source]
            self._sessions[key] = history

        if history[-1] != target:
            history.append(target)
            if len(history) > self.max_depth:
                del history[:-self.max_depth]

        self._touch(key)

    def previous(self, key: NavigationKey) -> EntityRef | None:
        history = self._sessions.get(key)
        if not history or len(history) < 2:
            return None
        self._touch(key)
        return history[-2]

    def back(self, key: NavigationKey) -> EntityRef | None:
        history = self._sessions.get(key)
        if not history or len(history) < 2:
            return None
        history.pop()
        self._touch(key)
        return history[-1]

    def transfer(self, old_key: NavigationKey, new_key: NavigationKey) -> None:
        if old_key == new_key:
            return
        history = self._sessions.pop(old_key, None)
        if history:
            self._sessions[new_key] = history
            self._touch(new_key)

    def clear(self, key: NavigationKey) -> None:
        self._sessions.pop(key, None)

    def _touch(self, key: NavigationKey) -> None:
        self._sessions.move_to_end(key)
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
