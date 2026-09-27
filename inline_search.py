"""Revision-aware bounded inline pages and cancellation of obsolete work."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass

from aiogram.types import InlineQueryResultArticle, InputRichMessageContent

from catalog_reads import load_snapshot, search_all
from database import Database
from ui.cards import build_mob_card, build_resource_card, build_gear_card, build_card_card


@dataclass(frozen=True, slots=True)
class InlinePage:
    results: tuple[InlineQueryResultArticle, ...]
    next_offset: str


@dataclass(frozen=True, slots=True)
class CachedPage:
    page: InlinePage
    stored_at: float
    weight: int


class InlineSearchService:
    def __init__(
        self, database: Database, *, max_bytes: int = 8 * 1024 * 1024, max_pages: int = 128, ttl_seconds: float = 60.0
    ) -> None:
        self.database = database
        self.max_bytes = max_bytes
        self.max_pages = max_pages
        self.ttl_seconds = ttl_seconds
        self._cache: OrderedDict[tuple[int, str, int, str | None], CachedPage] = OrderedDict()
        self._weight = 0
        self._latest_revision = -1

    def _prune(self, revision: int) -> None:
        cutoff = time.monotonic() - self.ttl_seconds
        for key, cached in tuple(self._cache.items()):
            if key[0] != revision or cached.stored_at < cutoff:
                self._weight -= self._cache.pop(key).weight
        while self._cache and (len(self._cache) > self.max_pages or self._weight > self.max_bytes):
            _, cached = self._cache.popitem(last=False)
            self._weight -= cached.weight

    async def page(self, query: str, offset: int, bot_username: str | None) -> InlinePage:
        async with self.database.read_transaction():
            revision = await self.database.get_catalog_revision()
            self._latest_revision = max(self._latest_revision, revision)
            self._prune(self._latest_revision)
            key = (revision, query.lower(), offset, bot_username)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached.page
            entries = await search_all(self.database, query, offset=offset)
            snapshot = await load_snapshot(self.database, entries[:50])
        results: list[InlineQueryResultArticle] = []
        for entry in entries[:50]:
            # Permit cancellation between CPU rendering iterations as well as SQL awaits.
            await asyncio.sleep(0)
            record = entry.item
            if entry.kind == "mob":
                card = await build_mob_card(snapshot, record["id"], bot_username=bot_username)
                description = f"❤️ HP: {record.get('hp', 0)} | ⭐ Опыт: {record.get('exp', 0)}"
            elif entry.kind == "resource":
                card = await build_resource_card(snapshot, record["id"], bot_username=bot_username)
                description = "Ресурс"
            elif entry.kind == "gear":
                card = await build_gear_card(snapshot, record["id"], bot_username=bot_username)
                description = f"{record.get('slot', '')} | {record.get('rarity', '')}"
            else:
                card = await build_card_card(snapshot, record["id"], bot_username=bot_username)
                description = f"Карта · {record.get('slot', '')}"
            prefix = "res" if entry.kind == "resource" else entry.kind
            results.append(
                InlineQueryResultArticle(
                    id=f"{prefix}_{record['id']}",
                    title=record["name"],
                    description=description,
                    input_message_content=InputRichMessageContent(rich_message=card.rich_message),
                )
            )
        page = InlinePage(tuple(results), str(offset + 50) if len(entries) > 50 else "")
        # An older snapshot can finish rendering after a newer revision was cached.
        # Return its consistent result without evicting or repopulating the newer cache.
        if revision != self._latest_revision:
            return page
        weight = sum(len(result.model_dump_json().encode("utf-8")) for result in results)
        previous = self._cache.pop(key, None)
        if previous is not None:
            self._weight -= previous.weight
        if weight <= self.max_bytes:
            self._cache[key] = CachedPage(page, time.monotonic(), weight)
            self._weight += weight
        self._prune(revision)
        return page


class LatestInlineQueries:
    """Only active request tasks are retained; logging lives in a separate registry."""

    def __init__(self) -> None:
        self._active: dict[int, tuple[str, set[asyncio.Task[object]]]] = {}

    def begin(self, user_id: int, query: str, *, first_page: bool) -> None:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Inline search requires an asyncio task")
        previous = self._active.get(user_id)
        if previous is not None and first_page and previous[0] != query:
            for old in previous[1]:
                if old is not task:
                    old.cancel()
            previous = None
        if previous is None:
            previous = (query, set())
            self._active[user_id] = previous
        previous[1].add(task)

    def finish(self, user_id: int) -> None:
        previous = self._active.get(user_id)
        if previous is None:
            return
        task = asyncio.current_task()
        if task is not None:
            previous[1].discard(task)
        if not previous[1]:
            self._active.pop(user_id, None)
