from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class UserRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def register_user_if_not_exists(
        self, user_id: int, username: str | None = None, first_name: str | None = None, last_name: str | None = None
    ) -> None:
        async with self.db.transaction():
            await self.db.execute_query(
                """
                INSERT INTO users (user_id, username, first_name, last_name, first_seen, last_activity)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_name = excluded.last_name,
                    last_activity = CURRENT_TIMESTAMP
                """,
                (user_id, username, first_name, last_name),
            )
            await self.db.execute_query(
                "UPDATE recipe_owners SET player_username = ? WHERE user_id = ? AND player_username IS NOT ?",
                (username, user_id, username),
            )
