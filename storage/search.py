from __future__ import annotations
from catalog_types import (
    SearchItem,
)
from storage.rows import _search_item
from recipe_domain import (
    DomainError,
    normalize_identity,
    validate_page,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class SearchRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def search(self, query: str, *, offset: int = 0, limit: int = 50) -> dict[str, list[SearchItem]]:
        validate_page(offset, limit)
        if not isinstance(query, str) or len(query) > 256:
            raise DomainError("Поисковый запрос должен быть не длиннее 256 символов.")
        needle = normalize_identity(query)
        results: dict[str, list[SearchItem]] = {}
        sources = (
            (
                "mobs",
                "m.id,m.name,m.emoji,m.hp,m.dust_min,m.dust_max,m.exp,l.name AS location_name,l.emoji AS location_emoji",
                "mobs m JOIN locations l ON l.id=m.location_id",
                "m.name",
                "m.id",
            ),
            ("resources", "id,name,emoji,type", "resources", "name", "id"),
            ("gear", "id,name,emoji,rarity,slot", "gear", "name", "id"),
            ("cards", "id,name,emoji,slot", "cards", "name", "id"),
        )
        for kind, fields, source, name, identifier in sources:
            rows = await self.db.execute_query(
                f"""SELECT {fields} FROM {source}
                WHERE INSTR(NORMALIZE_IDENTITY({name}),?) > 0
                ORDER BY CASE WHEN NORMALIZE_IDENTITY({name})=? THEN 0
                              WHEN INSTR(NORMALIZE_IDENTITY({name}),?)=1 THEN 1 ELSE 2 END,
                         NORMALIZE_IDENTITY({name}),{identifier} LIMIT ? OFFSET ?""",
                (needle, needle, needle, limit, offset),
            )
            results[kind] = [_search_item(row) for row in rows]
        return results
