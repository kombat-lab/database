from __future__ import annotations
from storage.types import sql_int, sql_text
from catalog_types import (
    LocationRow,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class LocationRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def _load_locations_cache(self) -> None:
        locations = await self.db.execute_query("SELECT id,name,emoji,parent_id FROM locations ORDER BY id")
        self.db._locations_cache = {
            sql_int(row["id"]): LocationRow(
                id=sql_int(row["id"]),
                name=sql_text(row["name"]),
                emoji=sql_text(row["emoji"]),
                parent_id=sql_int(row["parent_id"]) if row["parent_id"] is not None else None,
            )
            for row in locations
        }

    async def get_location_by_id(self, location_id: int) -> LocationRow | None:
        rows = await self.db.execute_query("SELECT id,name,emoji,parent_id FROM locations WHERE id=?", (location_id,))
        if not rows:
            return None
        row = rows[0]
        return LocationRow(
            id=sql_int(row["id"]),
            name=sql_text(row["name"]),
            emoji=sql_text(row["emoji"]),
            parent_id=sql_int(row["parent_id"]) if row["parent_id"] is not None else None,
        )

    async def get_locations(self) -> list[LocationRow]:
        rows = await self.db.execute_query("SELECT id,name,emoji,parent_id FROM locations ORDER BY id")
        return [
            LocationRow(
                id=sql_int(row["id"]),
                name=sql_text(row["name"]),
                emoji=sql_text(row["emoji"]),
                parent_id=sql_int(row["parent_id"]) if row["parent_id"] is not None else None,
            )
            for row in rows
        ]

    async def get_location_children(self, parent_id: int | None) -> list[LocationRow]:
        return [row for row in await self.db.get_locations() if row["parent_id"] == parent_id]

    async def get_location_parent(self, location_id: int) -> LocationRow | None:
        location = await self.db.get_location_by_id(location_id)
        return (
            await self.db.get_location_by_id(location["parent_id"])
            if location is not None and location["parent_id"] is not None
            else None
        )
