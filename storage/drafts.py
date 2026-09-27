from __future__ import annotations
from storage.types import sql_int, sql_text
import json
import hashlib
import secrets
from catalog_types import (
    DropItemType,
)
from storage.types import DbRow
from recipe_domain import (
    DomainError,
    DuplicateIdentityError,
    DraftConflictError,
    GearDraft,
    GearDraftPayload,
    GearSaveResult,
    LearningScrollInput,
    MaterialInput,
    positive_integer,
    validate_draft_payload,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from database import Database


class GearDraftRepository:
    def __init__(self, database: Database) -> None:
        self.db = database

    async def get_gear_draft_payload(self, gear_id: int) -> GearDraftPayload | None:
        gear = await self.db.get_gear_by_id(gear_id)
        if gear is None:
            return None
        payload = GearDraftPayload(
            gear_id=gear["id"],
            name=gear["name"],
            rarity=gear["rarity"],
            slot=gear["slot"],
            emoji=gear["emoji"],
            level=gear["level"],
            classes=gear["classes"],
            note=gear["note"],
            craftable=False,
            quantity=1,
            materials=[],
            learning_scroll=None,
            gear_mob_ids=[
                sql_int(row["mob_id"])
                for row in await self.db.execute_query(
                    "SELECT mob_id FROM drops WHERE item_type='gear' AND item_id=? ORDER BY mob_id", (gear["id"],)
                )
            ],
            scroll_mob_ids=[],
        )
        rows = await self.db.execute_query(
            "SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear["id"],)
        )
        if rows:
            recipe = await self.db.get_recipe_details(sql_int(rows[0]["id"]))
            if recipe is not None:
                payload["craftable"] = True
                payload["quantity"] = recipe["quantity"]
                payload["materials"] = [
                    MaterialInput(resource_id=item["resource_id"], quantity=item["quantity"])
                    for item in recipe["ingredients"]
                ]
                scroll = recipe["learning_scroll"]
                if scroll is not None:
                    payload["learning_scroll"] = LearningScrollInput(resource_id=scroll["id"])
                    payload["scroll_mob_ids"] = [
                        sql_int(row["mob_id"])
                        for row in await self.db.execute_query(
                            "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id",
                            (scroll["id"],),
                        )
                    ]
        return payload

    @staticmethod
    def _payload_fingerprint(payload: GearDraftPayload) -> str:
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def _gear_owner_fingerprint(self, gear_id: int) -> str:
        rows = await self.db.execute_query(
            "SELECT ro.owner_id,ro.user_id,ro.player_username FROM recipe_owners ro JOIN recipes r ON r.id=ro.recipe_id WHERE r.result_type='gear' AND r.result_id=? ORDER BY ro.owner_id",
            (gear_id,),
        )
        return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    async def _scroll_reference(self, scroll_id: int) -> tuple[str, list[int]]:
        resource = await self.db.get_resource_by_id(scroll_id)
        if resource is None or resource["type"] != "scroll_recipe":
            raise DomainError("Выбранный свиток удалён или изменил тип.")
        drops = [
            sql_int(row["mob_id"])
            for row in await self.db.execute_query(
                "SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=? ORDER BY mob_id",
                (scroll_id,),
            )
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                {"resource": resource, "drops": drops},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return fingerprint, drops

    async def _prepare_draft_references(self, payload: GearDraftPayload, previous: DbRow | None = None) -> str:
        scroll = payload.get("learning_scroll")
        scroll_id = scroll.get("resource_id") if scroll is not None else None
        if scroll_id is None:
            return "{}"
        if previous is not None:
            previous_scroll = self.db._decode_draft(previous)["payload"].get("learning_scroll")
            previous_id = previous_scroll.get("resource_id") if previous_scroll is not None else None
            if previous_id == scroll_id:
                return sql_text(previous["reference_fingerprints_json"])
        fingerprint, drop_ids = await self.db._scroll_reference(scroll_id)
        # Selecting a physical scroll starts from its current sources. Further
        # source edits preserve this fingerprint and are compared at final save.
        payload["scroll_mob_ids"] = drop_ids
        return json.dumps({str(scroll_id): fingerprint}, sort_keys=True)

    async def _check_draft_references(self, row: DbRow, payload: GearDraftPayload) -> None:
        scroll = payload.get("learning_scroll")
        scroll_id = scroll.get("resource_id") if scroll is not None else None
        if scroll_id is None:
            return
        expected: object = json.loads(sql_text(row["reference_fingerprints_json"]))
        fingerprint, _ = await self.db._scroll_reference(scroll_id)
        if not isinstance(expected, dict) or expected.get(str(scroll_id)) != fingerprint:
            raise DraftConflictError(
                "Выбранный свиток или его источники изменены другим редактором. Откройте новый черновик с актуальными данными."
            )

    @staticmethod
    def _decode_draft(row: DbRow) -> GearDraft:
        payload = validate_draft_payload(json.loads(sql_text(row["payload_json"])))
        saved: GearSaveResult | None = None
        if row["saved_result_json"] is not None:
            value: object = json.loads(sql_text(row["saved_result_json"]))
            if not isinstance(value, dict) or not isinstance(value.get("draft_id"), str):
                raise DomainError("Повреждён результат сохранения черновика.")
            saved = GearSaveResult(
                draft_id=value["draft_id"],
                gear_id=positive_integer(value.get("gear_id"), "Снаряжение"),
                recipe_id=positive_integer(value["recipe_id"], "Рецепт")
                if value.get("recipe_id") is not None
                else None,
                scroll_resource_id=positive_integer(value["scroll_resource_id"], "Свиток")
                if value.get("scroll_resource_id") is not None
                else None,
            )
        status = row["status"]
        if status not in ("editing", "saved", "cancelled"):
            raise DomainError("Повреждён статус черновика.")
        draft: GearDraft = dict(
            draft_id=str(row["draft_id"]),
            owner_user_id=sql_int(row["owner_user_id"]),
            chat_id=sql_int(row["chat_id"]),
            message_id=sql_int(row["message_id"]),
            revision=sql_int(row["revision"]),
            status="editing",
            payload=payload,
            saved_result=saved,
        )
        if status == "saved":
            draft["status"] = "saved"
        elif status == "cancelled":
            draft["status"] = "cancelled"
        return draft

    async def _context_draft(
        self, draft_id: str, owner_user_id: int, chat_id: int, message_id: int | None = None
    ) -> DbRow:
        rows = await self.db.execute_query("SELECT * FROM gear_drafts WHERE draft_id=?", (draft_id,))
        if (
            not rows
            or rows[0]["owner_user_id"] != owner_user_id
            or rows[0]["chat_id"] != chat_id
            or (message_id is not None and rows[0]["message_id"] != message_id)
        ):
            raise DraftConflictError("Черновик недоступен или открыт из другого сообщения. Откройте его заново.")
        return rows[0]

    @staticmethod
    def _check_draft_revision(row: DbRow, expected_revision: int) -> None:
        if row["status"] != "editing" or row["revision"] != expected_revision:
            raise DraftConflictError("Черновик уже изменён или завершён. Откройте актуальный экран.")

    async def create_gear_draft(
        self,
        *,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        payload: GearDraftPayload | None = None,
        gear_id: int | None = None,
    ) -> GearDraft:
        positive_integer(owner_user_id, "Администратор")
        positive_integer(message_id, "Сообщение")
        async with self.db.transaction():
            original = await self.db.get_gear_draft_payload(gear_id) if gear_id is not None else None
            if gear_id is not None and original is None:
                raise DomainError("Снаряжение уже удалено.")
            value = validate_draft_payload(payload if payload is not None else (original or {}))
            target_id = original["gear_id"] if original is not None else None
            if value.get("gear_id") != target_id:
                raise DraftConflictError("Нельзя менять предмет, которому принадлежит черновик.")
            references = await self.db._prepare_draft_references(value)
            draft_id = secrets.token_hex(8)
            await self.db.execute_query(
                "INSERT INTO gear_drafts(draft_id,owner_user_id,chat_id,message_id,target_gear_id,base_fingerprint,base_owner_fingerprint,reference_fingerprints_json,published_reference_fingerprints_json,payload_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    draft_id,
                    owner_user_id,
                    chat_id,
                    message_id,
                    target_id,
                    self.db._payload_fingerprint(original) if original is not None else None,
                    await self.db._gear_owner_fingerprint(target_id) if target_id is not None else None,
                    references,
                    await self.db._prepare_draft_references(original.copy()) if original is not None else "{}",
                    json.dumps(value, ensure_ascii=False),
                ),
            )
            return self.db._decode_draft(await self.db._context_draft(draft_id, owner_user_id, chat_id))

    async def get_gear_draft(self, draft_id: str, *, owner_user_id: int, chat_id: int) -> GearDraft | None:
        try:
            row = await self.db._context_draft(draft_id, owner_user_id, chat_id)
        except DraftConflictError:
            return None
        return self.db._decode_draft(row)

    async def list_gear_drafts(self, *, owner_user_id: int, chat_id: int) -> list[GearDraft]:
        rows = await self.db.execute_query(
            "SELECT * FROM gear_drafts WHERE owner_user_id=? AND chat_id=? AND status='editing' ORDER BY updated_at DESC,draft_id",
            (owner_user_id, chat_id),
        )
        return [self.db._decode_draft(row) for row in rows]

    async def update_gear_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        payload: GearDraftPayload,
    ) -> GearDraft:
        value = validate_draft_payload(payload)
        async with self.db.transaction():
            row = await self.db._context_draft(draft_id, owner_user_id, chat_id, message_id)
            self.db._check_draft_revision(row, expected_revision)
            if value.get("gear_id") != row["target_gear_id"]:
                raise DraftConflictError("Нельзя менять предмет, которому принадлежит черновик.")
            references = await self.db._prepare_draft_references(value, row)
            await self.db.execute_query(
                "UPDATE gear_drafts SET payload_json=?,reference_fingerprints_json=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (json.dumps(value, ensure_ascii=False), references, draft_id),
            )
            return self.db._decode_draft(await self.db._context_draft(draft_id, owner_user_id, chat_id))

    async def bind_gear_draft_message(
        self,
        draft_id: str,
        *,
        old_message_id: int,
        new_message_id: int,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
    ) -> GearDraft:
        positive_integer(new_message_id, "Сообщение")
        async with self.db.transaction():
            row = await self.db._context_draft(draft_id, owner_user_id, chat_id, old_message_id)
            self.db._check_draft_revision(row, expected_revision)
            await self.db.execute_query(
                "UPDATE gear_drafts SET message_id=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (new_message_id, draft_id),
            )
            return self.db._decode_draft(await self.db._context_draft(draft_id, owner_user_id, chat_id))

    async def cancel_gear_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> GearDraft:
        async with self.db.transaction():
            row = await self.db._context_draft(draft_id, owner_user_id, chat_id, message_id)
            self.db._check_draft_revision(row, expected_revision)
            await self.db.execute_query(
                "UPDATE gear_drafts SET status='cancelled',revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (draft_id,),
            )
            return self.db._decode_draft(await self.db._context_draft(draft_id, owner_user_id, chat_id))

    async def delete_gear_draft_target(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
        delete_gear: bool,
        delete_scroll: bool = False,
    ) -> GearDraft:
        """Delete a reviewed draft target only if its aggregate still matches."""
        async with self.db.transaction():
            row = await self.db._context_draft(draft_id, owner_user_id, chat_id, message_id)
            if row["status"] == "cancelled":
                return self.db._decode_draft(row)
            self.db._check_draft_revision(row, expected_revision)
            if row["target_gear_id"] is None:
                raise DomainError("У нового черновика пока нет опубликованного предмета.")
            gear_id = sql_int(row["target_gear_id"])
            current = await self.db.get_gear_draft_payload(gear_id)
            if current is None or self.db._payload_fingerprint(current) != row["base_fingerprint"]:
                raise DraftConflictError(
                    "Предмет изменён другим редактором. Откройте актуальную карточку перед удалением."
                )
            if await self.db._gear_owner_fingerprint(gear_id) != row["base_owner_fingerprint"]:
                raise DraftConflictError(
                    "Список изучивших рецепт изменился. Откройте актуальную карточку перед удалением."
                )
            await self.db._check_published_draft_references(row, current)
            recipes = await self.db.execute_query(
                "SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear_id,)
            )
            if recipes:
                await self.db.delete_recipe_bundle(sql_int(recipes[0]["id"]), delete_scroll=delete_scroll)
            if delete_gear:
                await self.db.delete_gear(gear_id)
            await self.db.execute_query(
                "UPDATE gear_drafts SET status='cancelled',revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (draft_id,),
            )
            return self.db._decode_draft(await self.db._context_draft(draft_id, owner_user_id, chat_id))

    async def _create_named_draft_resource(
        self, name: str, emoji: str, resource_type: str, note: str = "", *, allow_duplicate: bool = False
    ) -> int:
        return await self.db.add_resource(name, emoji, resource_type, note, allow_duplicate=allow_duplicate)

    async def _replace_draft_drops(self, item_type: DropItemType, item_id: int, mob_ids: list[int]) -> None:
        await self.db.set_item_drop_sources(item_type, item_id, mob_ids)

    async def save_gear_draft(
        self,
        draft_id: str,
        *,
        expected_revision: int,
        owner_user_id: int,
        chat_id: int,
        message_id: int,
    ) -> GearSaveResult:
        async with self.db.transaction():
            row = await self.db._context_draft(draft_id, owner_user_id, chat_id, message_id)
            draft = self.db._decode_draft(row)
            if draft["status"] == "saved" and draft["saved_result"] is not None:
                return draft["saved_result"]
            self.db._check_draft_revision(row, expected_revision)
            value = validate_draft_payload(draft["payload"], complete=True)
            await self.db._check_draft_references(row, value)
            gear_id = value.get("gear_id")
            current: GearDraftPayload | None = None
            if gear_id != row["target_gear_id"]:
                raise DraftConflictError("Изменился предмет черновика.")
            if gear_id is not None:
                current = await self.db.get_gear_draft_payload(gear_id)
                if current is None or self.db._payload_fingerprint(current) != row["base_fingerprint"]:
                    raise DraftConflictError(
                        "Предмет изменён другим редактором. Создайте новый черновик, чтобы не потерять изменения."
                    )
            for mob_id in set(value["gear_mob_ids"] + value["scroll_mob_ids"]):
                if not await self.db.execute_query("SELECT 1 FROM mobs WHERE id=?", (mob_id,)):
                    raise DomainError(f"Источник {mob_id} уже удалён. Обновите список источников.")
            identity_changed = current is None or (
                value["name"].casefold(),
                value["rarity"],
                value["slot"],
                value["level"],
            ) != (current["name"].strip().casefold(), current["rarity"], current["slot"], current["level"])
            if identity_changed and await self.db.execute_query(
                "SELECT 1 FROM gear WHERE NORMALIZE_IDENTITY(name)=NORMALIZE_IDENTITY(?) AND rarity=? AND slot=? AND level=? AND id IS NOT ?",
                (value["name"], value["rarity"], value["slot"], value["level"], gear_id),
            ):
                raise DuplicateIdentityError(
                    "Такое снаряжение этого уровня уже существует. Откройте его редактирование."
                )
            if gear_id is None:
                gear_id = await self.db.add_gear(
                    value["name"],
                    value["rarity"],
                    value["slot"],
                    value["emoji"],
                    value["level"],
                    value["classes"],
                    value["note"],
                )
            else:
                await self.db.update_gear(
                    gear_id,
                    value["name"],
                    value["rarity"],
                    value["slot"],
                    value["emoji"],
                    value["level"],
                    value["classes"],
                    value["note"],
                )
            rows = await self.db.execute_query(
                "SELECT id FROM recipes WHERE result_type='gear' AND result_id=?", (gear_id,)
            )
            recipe_id = sql_int(rows[0]["id"]) if rows else None
            scroll_id: int | None = None
            if value["craftable"]:
                if recipe_id is None:
                    recipe_id = await self.db.create_recipe("gear", gear_id, value["quantity"])
                else:
                    await self.db.execute_query(
                        "UPDATE recipes SET quantity=? WHERE id=?", (value["quantity"], recipe_id)
                    )
                materials: list[tuple[int, int]] = []
                for material in value["materials"]:
                    resource_id = material.get("resource_id")
                    if resource_id is None:
                        resource_id = await self.db._create_named_draft_resource(
                            material["name"],
                            material.get("emoji", ""),
                            "craft",
                            allow_duplicate=material.get("allow_duplicate", False),
                        )
                    resource = await self.db.get_resource_by_id(resource_id)
                    if resource is None or resource["type"] == "scroll_recipe":
                        raise DomainError("Материал удалён или является изучаемым свитком.")
                    materials.append((resource_id, material["quantity"]))
                if len({item[0] for item in materials}) != len(materials):
                    raise DomainError("В рецепте повторяется материал.")
                await self.db.execute_query("DELETE FROM recipe_ingredients WHERE recipe_id=?", (recipe_id,))
                for resource_id, quantity in materials:
                    await self.db.add_ingredient(recipe_id, resource_id, quantity)
                scroll = value["learning_scroll"]
                if scroll is not None:
                    scroll_id = scroll.get("resource_id")
                    if scroll_id is None:
                        scroll_name = scroll.get("name", f"Рецепт ({value['name']})")
                        # The automatic label must obey the same limits as explicitly entered text.
                        validate_draft_payload({"learning_scroll": {"name": scroll_name}})
                        scroll_id = await self.db._create_named_draft_resource(
                            scroll_name,
                            scroll.get("emoji", "📜"),
                            "scroll_recipe",
                            scroll.get("note", ""),
                            allow_duplicate=scroll.get("allow_duplicate", False),
                        )
                await self.db.set_recipe_learning_scroll(recipe_id, scroll_id)
                if scroll_id is not None:
                    await self.db._replace_draft_drops("resource", scroll_id, value["scroll_mob_ids"])
            elif recipe_id is not None:
                raise DomainError("Рецепт уже существует. Для его удаления используйте явное удаление рецепта.")
            await self.db._replace_draft_drops("gear", gear_id, value["gear_mob_ids"])
            result = GearSaveResult(
                draft_id=draft_id, gear_id=gear_id, recipe_id=recipe_id, scroll_resource_id=scroll_id
            )
            await self.db.execute_query(
                "UPDATE gear_drafts SET status='saved',saved_result_json=?,revision=revision+1,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?",
                (json.dumps(result, ensure_ascii=False), draft_id),
            )
            return result

    async def _check_published_draft_references(self, row: DbRow, current: GearDraftPayload) -> None:
        scroll = current.get("learning_scroll")
        scroll_id = scroll.get("resource_id") if scroll is not None else None
        if scroll_id is None:
            return
        expected: object = json.loads(sql_text(row["published_reference_fingerprints_json"]))
        fingerprint, _ = await self.db._scroll_reference(scroll_id)
        if not isinstance(expected, dict) or expected.get(str(scroll_id)) != fingerprint:
            raise DraftConflictError(
                "Опубликованный свиток изменился или черновик создан старой версией. Откройте актуальную карточку перед удалением."
            )
