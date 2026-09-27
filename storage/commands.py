from __future__ import annotations
from storage.types import sql_int
import hashlib
from recipe_domain import (
    DomainError,
    DraftConflictError,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class CommandRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def get_catalog_revision(self) -> int:
        rows = await self.db.execute_query("SELECT revision FROM catalog_metadata WHERE id=1")
        return sql_int(rows[0]["revision"])

    async def _catalog_command_result(
        self, operation_id: str | None, result_type: str, request_json: str
    ) -> int | None:
        if operation_id is None:
            return None
        if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 256:
            raise DomainError("Некорректный идентификатор команды.")
        rows = await self.db.execute_query(
            "SELECT result_id,request_hash FROM command_results WHERE operation_key=?",
            (result_type + ":" + operation_id,),
        )
        if not rows:
            return None
        if rows[0]["request_hash"] != hashlib.sha256(request_json.encode("utf-8")).hexdigest():
            raise DraftConflictError("Эта команда уже сохранена с другими данными. Откройте актуальную карточку.")
        return sql_int(rows[0]["result_id"])

    async def _record_catalog_command(
        self, operation_id: str | None, result_type: str, request_json: str, result_id: int
    ) -> None:
        if operation_id is not None:
            await self.db.execute_query(
                "INSERT INTO command_results(operation_key,result_type,result_id,request_hash) VALUES (?,?,?,?)",
                (
                    result_type + ":" + operation_id,
                    result_type,
                    result_id,
                    hashlib.sha256(request_json.encode("utf-8")).hexdigest(),
                ),
            )
