"""Typed catalog commands used by Telegram editors.

Database dependencies are resolved at call time so isolated runtimes and tests
never inherit a bound connection from import time.
"""

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager

from aiogram.fsm.context import FSMContext
from fsm_storage import SQLiteFSMStorage
from database import Database
from admin_contracts import EntityName, EntityRow
from recipe_domain import DomainError


@asynccontextmanager
async def admin_transition(state: FSMContext) -> AsyncIterator[None]:
    """Commit a catalog command and its next durable input state together.

    Keep Telegram delivery outside this context. MemoryStorage is supported for
    isolated legacy integrations, restoring its previous state on failed work.
    """
    if isinstance(state.storage, SQLiteFSMStorage):
        async with state.storage.database.transaction():
            yield
        return
    previous_state, previous_data = await state.get_state(), await state.get_data()
    try:
        yield
    except BaseException:
        await state.storage.set_data(state.key, previous_data)
        await state.storage.set_state(state.key, previous_state)
        raise


class EntityCommands:
    def __init__(self, database: Callable[[], Database], kind: EntityName) -> None:
        self.database = database
        self.kind = kind

    async def page(self, offset: int, limit: int) -> Sequence[EntityRow]:
        db = self.database()
        if self.kind == "resource":
            return await db.get_resources_page(offset, limit)
        if self.kind == "gear":
            return await db.get_all_gear(offset, limit)
        return await db.get_cards_page(offset, limit)

    async def get(self, entity_id: int) -> EntityRow | None:
        db = self.database()
        if self.kind == "resource":
            return await db.get_resource_by_id(entity_id)
        if self.kind == "gear":
            return await db.get_gear_by_id(entity_id)
        return await db.get_card_by_id(entity_id)

    async def delete(self, entity_id: int) -> None:
        db = self.database()
        if self.kind == "resource":
            await db.delete_resource(entity_id)
        elif self.kind == "gear":
            await db.delete_gear(entity_id)
        else:
            await db.delete_card(entity_id)

    async def update(self, entity_id: int, field: str, value: str | int) -> None:
        db = self.database()
        if self.kind == "gear" and field == "level":
            if type(value) is not int:
                raise DomainError("Уровень должен быть целым числом.")
            await db.update_gear(entity_id, level=value)
            return
        if not isinstance(value, str):
            raise DomainError("Значение поля должно быть текстом.")
        if self.kind == "resource":
            if field == "name":
                await db.update_resource(entity_id, name=value)
            elif field == "emoji":
                await db.update_resource(entity_id, emoji=value)
            elif field == "type":
                await db.update_resource(entity_id, resource_type=value)
            elif field == "note":
                await db.update_resource(entity_id, note=value)
            else:
                raise DomainError("Неизвестное поле ресурса.")
        elif self.kind == "gear":
            if field == "name":
                await db.update_gear(entity_id, name=value)
            elif field == "rarity":
                await db.update_gear(entity_id, rarity=value)
            elif field == "slot":
                await db.update_gear(entity_id, slot=value)
            elif field == "emoji":
                await db.update_gear(entity_id, emoji=value)
            elif field == "classes":
                await db.update_gear(entity_id, classes=value)
            elif field == "note":
                await db.update_gear(entity_id, note=value)
            else:
                raise DomainError("Неизвестное поле снаряжения.")
        elif field in {"name", "emoji", "slot", "bonus1", "bonus2", "bonus3", "bonus4", "note"}:
            await db.update_card(entity_id, **{field: value})
        else:
            raise DomainError("Неизвестное поле карты.")
