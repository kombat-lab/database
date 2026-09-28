"""Validation of persisted FSM values at the application boundary."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from recipe_domain import DomainError, MAX_NAME_LENGTH, MAX_NOTE_LENGTH, MAX_RESOURCE_NAME_LENGTH


def form_text(data: Mapping[str, object], key: str, *, maximum: int = 500, empty: bool = False) -> str:
    value = data.get(key)
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > maximum:
        raise DomainError("Форма неполна или устарела. Откройте ввод заново.")
    return value


def form_id(data: Mapping[str, object], key: str, *, minimum: int = 1) -> int:
    value = data.get(key)
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise DomainError("Форма неполна или устарела. Откройте ввод заново.")
    return value


@dataclass(frozen=True, slots=True)
class CatalogCreation:
    kind: Literal["resource", "card"]
    session: str
    name: str
    emoji: str
    category: str
    note: str
    bonuses: tuple[str, str, str, str]

    @classmethod
    def decode(cls, data: Mapping[str, object]) -> "CatalogCreation":
        kind = data.get("catalog_creation_kind")
        if kind not in ("resource", "card"):
            raise DomainError("Неизвестный вид предмета.")
        prefix = "res" if kind == "resource" else "card"
        bonuses = (
            tuple(form_text(data, f"card_bonus{i}", empty=True) for i in range(1, 5))
            if kind == "card"
            else ("", "", "", "")
        )
        return cls(
            "resource" if kind == "resource" else "card",
            form_text(data, "catalog_creation_session", maximum=64),
            form_text(
                data, f"{prefix}_name", maximum=MAX_RESOURCE_NAME_LENGTH if kind == "resource" else MAX_NAME_LENGTH
            ),
            form_text(data, f"{prefix}_emoji", maximum=64),
            form_text(data, "res_type" if kind == "resource" else "card_slot"),
            form_text(data, "catalog_creation_note", maximum=MAX_NOTE_LENGTH, empty=True),
            (bonuses[0], bonuses[1], bonuses[2], bonuses[3]),
        )
