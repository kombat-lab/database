from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .callbacks import EntityNavigateCallback


class EntityLinkMode(str, Enum):
    DEEP_LINK = "deep_link"
    CALLBACK = "callback"


@dataclass(frozen=True)
class MarkupPair:
    rich: str
    fallback: str

    @classmethod
    def same(cls, value: str) -> MarkupPair:
        return cls(value, value)


def combine_markup(*parts: MarkupPair | str, separator: str = "") -> MarkupPair:
    pairs = [part if isinstance(part, MarkupPair) else MarkupPair.same(part) for part in parts]
    return MarkupPair(
        rich=separator.join(part.rich for part in pairs),
        fallback=separator.join(part.fallback for part in pairs),
    )


def build_deep_link(
    bot_username: str,
    item_type: str,
    item_id: int,
    return_param: str | None = None,
) -> str:
    payload = f"{item_type}_{item_id}"
    if return_param:
        candidate = f"{payload}-r-{return_param}"
        if len(candidate) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            payload = candidate
    return f"https://t.me/{bot_username}?start={payload}"


@dataclass(frozen=True)
class EntityLinkBuilder:
    """Builds one semantic entity link for RichMessage and HTML fallback."""

    bot_username: str
    mode: EntityLinkMode = EntityLinkMode.DEEP_LINK
    source_type: str | None = None
    source_id: int | None = None

    def link(
        self,
        item_type: str,
        item_id: int,
        label_html: str,
        return_param: str | None = None,
    ) -> MarkupPair:
        url = build_deep_link(
            self.bot_username,
            item_type,
            item_id,
            return_param,
        )
        fallback = f'<a href="{url}">{label_html}</a>'
        if self.mode is EntityLinkMode.DEEP_LINK:
            return MarkupPair.same(fallback)

        if not self.source_type or not self.source_id:
            return MarkupPair.same(fallback)

        callback_data = EntityNavigateCallback(
            entity_type=item_type,
            entity_id=item_id,
            source_type=self.source_type,
            source_id=self.source_id,
        ).pack()
        rich = (
            '<tg-button type="callback_data" style="link" '
            f'data="{callback_data}">{label_html}</tg-button>'
        )
        return MarkupPair(rich=rich, fallback=fallback)
