import unittest
from unittest.mock import AsyncMock, patch

from aiogram import types
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText

import admin_handlers as admin
import admin_mobs as mobs
from admin_utils import GenericEditStates
from database import Database
from runtime_scope import RuntimeScope
from runtime_scope import use_runtime_scope
from tests.test_catalog_creation_sessions import CatalogCreationFixture


class AdminReliabilityTests(CatalogCreationFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        location = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Лес','🌲')")
        self.mob_id = await self.db.add_mob("Волк", "🐺", 10, 1, 2, 3, location)
        self.resource_id = await self.db.add_resource("Сталь", "🧱", "craft")

    def failure(self):
        return TelegramNetworkError(
            method=EditMessageText(chat_id=101, message_id=self.latest.message_id, text="updated"), message="offline"
        )

    async def test_drop_list_command_retry_after_delivery_failure_keeps_added_drop(self):
        await self.state.update_data(mob_id=self.mob_id, drop_filter_value="craft")
        await self.state.set_state(mobs.MobStates.drop_list_page)
        with use_runtime_scope(self.scope):
            keyboard = await mobs.get_drop_list_keyboard(self.mob_id, "resource", "craft", 1)
        await mobs.present_mob(self.callback("fixture"), self.state, "Дроп", keyboard)
        command = self.callback(self.action("drop_set_resource_"))
        with patch.object(types.Message, "edit_text", AsyncMock(side_effect=self.failure())):
            with self.assertRaises(TelegramNetworkError):
                await self.route(command)
        self.assertTrue(await self.db.get_drop_status(self.mob_id, "resource", self.resource_id))
        await self.route(command)
        self.assertTrue(await self.db.get_drop_status(self.mob_id, "resource", self.resource_id))

    async def test_drop_search_command_retry_after_delivery_failure_keeps_removed_drop(self):
        await self.db.add_drop(self.mob_id, "resource", self.resource_id)
        await self.state.update_data(mob_id=self.mob_id, drop_search_query="Сталь")
        await self.state.set_state(mobs.MobStates.drop_search)
        items = await self.db.search_drop_items(self.mob_id, "Сталь")
        await mobs.present_mob(self.callback("fixture"), self.state, "Дроп", mobs.build_drop_search_keyboard(items))
        command = self.callback(self.action("drop_search_set_resource_"))
        with patch.object(types.Message, "edit_text", AsyncMock(side_effect=self.failure())):
            with self.assertRaises(TelegramNetworkError):
                await self.route(command)
        self.assertFalse(await self.db.get_drop_status(self.mob_id, "resource", self.resource_id))
        await self.route(command)
        self.assertFalse(await self.db.get_drop_status(self.mob_id, "resource", self.resource_id))

    async def test_failed_delivery_after_mob_rename_cannot_save_next_message(self):
        await self.state.update_data(mob_id=self.mob_id, edit_field="name")
        await self.state.set_state(mobs.MobStates.edit_new_value)
        await mobs.present_mob(self.callback("fixture"), self.state, "Имя")
        with patch.object(types.Message, "answer", AsyncMock(side_effect=self.failure())):
            with self.assertRaises(TelegramNetworkError):
                await self.text("Новое имя")
        self.assertEqual(await self.state.get_state(), mobs.MobStates.edit_field.state)
        await self.text("Спасибо")
        self.assertEqual((await self.db.get_mob_by_id(self.mob_id))["name"], "Новое имя")

    async def test_duplicate_resource_requires_explicit_variant_confirmation_and_is_idempotent(self):
        await self.resource_note("Сталь")
        await self.text("Новый вариант")
        await self.route(self.callback(self.action("isd:done")))
        self.assertEqual(await self.state.get_state(), admin.CatalogDuplicateStates.choose.state)
        self.assertEqual(len(await self.db.get_resource_name_matches("Сталь", "craft")), 1)
        command = self.callback(self.action("catalog_duplicate_confirm"))
        await self.route(command)
        await self.route(command)
        self.assertEqual(len(await self.db.get_resource_name_matches("Сталь", "craft")), 2)

    async def test_duplicate_prompt_delivery_failure_keeps_previous_save_retryable(self):
        import admin_item_sources as sources

        await self.resource_note("Сталь")
        await self.text("Отдельный вариант")
        old_screen = (await self.state.get_data())["admin_screen"]
        save = self.callback(self.action("isd:done"))
        with patch.object(types.Message, "edit_text", AsyncMock(side_effect=self.failure())):
            with self.assertRaises(TelegramNetworkError):
                await self.route(save)
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)
        self.assertEqual((await self.state.get_data())["admin_screen"], old_screen)
        await self.route(save)
        self.assertEqual(await self.state.get_state(), admin.CatalogDuplicateStates.choose.state)
        self.assertEqual(len(await self.db.get_resource_name_matches("Сталь", "craft")), 1)
        await self.route(self.callback(self.action("catalog_duplicate_confirm")))
        self.assertEqual(len(await self.db.get_resource_name_matches("Сталь", "craft")), 2)

    async def test_crash_after_commit_before_state_clear_replays_original_publication(self):
        await self.resource_note("Один раз")
        await self.text("Примечание")
        command = self.callback(self.action("isd:done"))
        with patch.object(self.state, "clear", AsyncMock(side_effect=RuntimeError("simulated crash"))):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                await self.route(command)
        self.assertEqual(len(await self.db.get_resource_name_matches("Один раз", "craft")), 1)
        await self.route(command)
        self.assertEqual(len(await self.db.get_resource_name_matches("Один раз", "craft")), 1)
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)

    async def test_existing_duplicate_can_be_opened_without_overwriting_or_inserting(self):
        await self.resource_note("Сталь")
        await self.text("Не должно сохраниться")
        await self.route(self.callback(self.action("isd:done")))
        await self.route(self.callback(self.action("catalog_duplicate_open_")))
        self.assertEqual(len(await self.db.get_resource_name_matches("Сталь", "craft")), 1)
        self.assertEqual((await self.db.get_resource_by_id(self.resource_id))["note"], "")
        self.assertEqual((await self.state.get_data())["entity_id"], self.resource_id)

    async def test_card_duplicate_requires_explicit_confirmation(self):
        from game_constants import GEAR_SLOTS

        await self.db.add_card("Карта", "🃏", GEAR_SLOTS[0])
        await self.card_note("Карта")
        await self.text("-")
        await self.route(self.callback(self.action("isd:done")))
        self.assertEqual(await self.state.get_state(), admin.CatalogDuplicateStates.choose.state)
        await self.route(self.callback(self.action("catalog_duplicate_confirm")))
        self.assertEqual(len(await self.db.get_card_name_matches("Карта", GEAR_SLOTS[0])), 2)

    async def test_factory_database_and_authorization_are_isolated(self):
        other = Database(":memory:")
        await other.connect()
        try:
            await other.add_resource("Только в другой базе", "🧱")
            router = admin.create_admin_router(RuntimeScope(other, frozenset({101})))
            await router.propagate_event(
                "callback_query",
                self.callback("admin_manage_resources"),
                bot=self.bot,
                state=self.state,
                raw_state=None,
                event_from_user=self.user,
            )
            labels = [button.text for row in self.latest.reply_markup.inline_keyboard for button in row]
            self.assertTrue(any("Только в другой базе" in label for label in labels))
            self.assertFalse(any("Сталь" in label for label in labels))
            denied = admin.create_admin_router(RuntimeScope(self.db, frozenset({999})))
            with patch.object(types.CallbackQuery, "answer", AsyncMock()) as answer:
                await denied.propagate_event(
                    "callback_query",
                    self.callback("admin_manage_resources"),
                    bot=self.bot,
                    state=self.state,
                    raw_state=None,
                    event_from_user=self.user,
                )
                answer.assert_awaited_once_with("⛔ Нет доступа.", show_alert=True)
        finally:
            await other.close()


class DurableAdminTransitionTests(CatalogCreationFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        from aiogram.fsm.context import FSMContext
        from fsm_storage import SQLiteFSMStorage

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database_path = str(Path(self.temp.name) / "durable-admin.db")
        await super().asyncSetUp()
        self.storage = SQLiteFSMStorage(self.db)
        self.state = FSMContext(self.storage, self.state.key)
        location = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Лес','🌲')")
        self.mob_id = await self.db.add_mob("Волк", "🐺", 10, 1, 2, 3, location)
        self.resource_id = await self.db.add_resource("Сталь", "🧱", "craft")

    async def test_fsm_failure_rolls_back_mob_rename_and_persists_previous_input_after_restart(self):
        from aiogram.fsm.context import FSMContext
        from fsm_storage import SQLiteFSMStorage

        await self.state.update_data(mob_id=self.mob_id, edit_field="name")
        await self.state.set_state(mobs.MobStates.edit_new_value)
        await mobs.present_mob(self.callback("fixture"), self.state, "Имя")
        with patch.object(self.state, "set_state", AsyncMock(side_effect=RuntimeError("disk failure"))):
            await self.text("Не должно записаться")
        reopened = Database(self.database_path)
        await reopened.connect()
        try:
            restored = FSMContext(SQLiteFSMStorage(reopened), self.state.key)
            self.assertEqual((await reopened.get_mob_by_id(self.mob_id))["name"], "Волк")
            self.assertEqual(await restored.get_state(), mobs.MobStates.edit_new_value.state)
            self.assertEqual((await restored.get_data())["edit_field"], "name")
        finally:
            await reopened.close()

    async def test_generic_edit_fsm_failure_rolls_back_resource_write(self):
        from admin_sessions import present_admin_text

        await self.state.update_data(editing_entity="resource", entity_id=self.resource_id, edit_field="name")
        await self.state.set_state(GenericEditStates.new_value)
        await present_admin_text(
            self.callback("fixture"),
            self.state,
            "Имя",
            context={"entity_id": self.resource_id, "editing_entity": "resource", "edit_field": "name"},
        )
        with patch.object(self.state, "set_state", AsyncMock(side_effect=RuntimeError("disk failure"))):
            await self.text("Не должно записаться")
        self.assertEqual((await self.db.get_resource_by_id(self.resource_id))["name"], "Сталь")
        self.assertEqual(await self.state.get_state(), GenericEditStates.new_value.state)

    async def test_sources_fsm_failure_rolls_back_drop_replacement(self):
        import admin_item_sources as sources

        with use_runtime_scope(self.scope):
            await sources.start_item_sources(self.callback("fixture"), self.state, "resource", self.resource_id)
        await self.route(self.callback(self.action(f"isd:toggle:{self.mob_id}")))
        with patch.object(self.state, "set_state", AsyncMock(side_effect=RuntimeError("disk failure"))):
            await self.route(self.callback(self.action("isd:done")))
        self.assertFalse(await self.db.get_drop_status(self.mob_id, "resource", self.resource_id))
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)

    async def test_committed_mob_edit_survives_delivery_failure_and_storage_restart(self):
        from aiogram.fsm.context import FSMContext
        from fsm_storage import SQLiteFSMStorage

        await self.state.update_data(mob_id=self.mob_id, edit_field="name")
        await self.state.set_state(mobs.MobStates.edit_new_value)
        await mobs.present_mob(self.callback("fixture"), self.state, "Имя")
        failure = TelegramNetworkError(
            method=EditMessageText(chat_id=101, message_id=self.latest.message_id, text="updated"), message="offline"
        )
        with patch.object(types.Message, "answer", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.text("Записанное имя")
        reopened = Database(self.database_path)
        await reopened.connect()
        try:
            restored = FSMContext(SQLiteFSMStorage(reopened), self.state.key)
            self.assertEqual((await reopened.get_mob_by_id(self.mob_id))["name"], "Записанное имя")
            self.assertEqual(await restored.get_state(), mobs.MobStates.edit_field.state)
            self.assertIsNone((await restored.get_data())["edit_field"])
        finally:
            await reopened.close()

    async def test_selection_and_screen_token_rollback_together_after_post_delivery_storage_failure(self):
        import admin_item_sources as sources
        import admin_sessions
        from aiogram.fsm.context import FSMContext
        from fsm_storage import SQLiteFSMStorage

        for failure_stage in ("selection", "screen_binding"):
            with self.subTest(failure_stage=failure_stage):
                name = f"Не публиковать {failure_stage}"
                await self.resource_note(name)
                await self.text("-")
                before = await self.state.get_data()
                toggle = self.callback(self.action(f"isd:toggle:{self.mob_id}"))
                original_store = sources.store_selection
                original_binding = admin_sessions.remember_admin_screen

                async def broken_store(*args, **kwargs):
                    await original_store(*args, **kwargs)
                    raise RuntimeError("selection write failed")

                async def broken_binding(*args, **kwargs):
                    await original_binding(*args, **kwargs)
                    raise RuntimeError("screen binding failed")

                target, attribute, operation = (
                    (sources, "store_selection", broken_store)
                    if failure_stage == "selection"
                    else (admin_sessions, "remember_admin_screen", broken_binding)
                )
                with patch.object(target, attribute, operation):
                    with self.assertRaises(RuntimeError):
                        await self.route(toggle)
                # Telegram accepted the checked item, but the new screen must
                # remain unauthorized if either persisted half failed.
                displayed_done = self.callback(self.action("isd:done"))
                self.assertTrue(
                    any(
                        button.text.startswith("☑️")
                        for row in self.latest.reply_markup.inline_keyboard
                        for button in row
                    )
                )
                self.assertEqual(
                    {
                        key: value
                        for key, value in (await self.state.get_data()).items()
                        if key != "admin_pending_screen"
                    },
                    before,
                )
                self.assertIn("admin_pending_screen", await self.state.get_data())
                await self.route(displayed_done)
                self.assertEqual(await self.db.get_resource_name_matches(name, "craft"), [])
                reopened = Database(self.database_path)
                await reopened.connect()
                try:
                    restored = FSMContext(SQLiteFSMStorage(reopened), self.state.key)
                    self.assertEqual(
                        {
                            key: value
                            for key, value in (await restored.get_data()).items()
                            if key != "admin_pending_screen"
                        },
                        before,
                    )
                    self.assertIn("admin_pending_screen", await restored.get_data())
                    self.assertEqual(await restored.get_state(), sources.ItemSourcesStates.select.state)
                finally:
                    await reopened.close()
                # Replaying the original intended toggle redraws a consistent
                # selection and allows publishing exactly that selection.
                await self.route(toggle)
                await self.route(self.callback(self.action("isd:done")))
                item_id = (await self.db.get_resource_name_matches(name, "craft"))[0]["id"]
                self.assertTrue(await self.db.get_drop_status(self.mob_id, "resource", item_id))

    async def test_mob_creation_rejects_bare_text_after_accepted_prompt_and_failed_state_commit(self):
        await self.state.set_data({"name": "Новый моб", "emoji": "🐺", "hp": 10})
        await self.state.set_state(mobs.MobStates.add_hp)
        await mobs.present_mob(self.callback("fixture"), self.state, "Введите HP:")
        old_prompt = self.latest
        with patch.object(self.state, "set_state", AsyncMock(side_effect=RuntimeError("FSM unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "FSM unavailable"):
                await self.text("100")
        self.assertIn("dust_min", self.latest.text)
        await self.text("3")
        self.assertEqual((await self.state.get_data())["hp"], 10)
        self.assertEqual(await self.state.get_state(), mobs.MobStates.add_hp.state)
        reopened = Database(self.database_path)
        await reopened.connect()
        try:
            from aiogram.fsm.context import FSMContext
            from fsm_storage import SQLiteFSMStorage

            restored = FSMContext(SQLiteFSMStorage(reopened), self.state.key)
            self.assertIn("admin_pending_screen", await restored.get_data())
            self.assertEqual((await restored.get_data())["hp"], 10)
        finally:
            await reopened.close()
        await self.text("100", reply_to=old_prompt)
        self.assertEqual((await self.state.get_data())["hp"], 100)
        self.assertEqual(await self.state.get_state(), mobs.MobStates.add_dust_min.state)
        self.assertNotIn("admin_pending_screen", await self.state.get_data())
        await self.text("3")
        self.assertEqual((await self.state.get_data())["hp"], 100)
        self.assertEqual((await self.state.get_data())["dust_min"], 3)

    async def test_mob_creation_ambiguous_delivery_keeps_inputs_blocked_until_confirmed_recovery(self):
        await self.state.set_data({"name": "Новый моб", "emoji": "🐺", "hp": 10})
        await self.state.set_state(mobs.MobStates.add_hp)
        await mobs.present_mob(self.callback("fixture"), self.state, "Введите HP:")
        old_prompt = self.latest

        async def accepted_then_failed(text, **kwargs):
            await self.answer_message(text, **kwargs)
            raise TelegramNetworkError(
                method=EditMessageText(chat_id=101, message_id=self.latest.message_id, text=text),
                message="response lost",
            )

        with patch.object(types.Message, "answer", AsyncMock(side_effect=accepted_then_failed)):
            with self.assertRaises(TelegramNetworkError):
                await self.text("100")
        self.assertIn("dust_min", self.latest.text)
        await self.text("3")
        self.assertEqual((await self.state.get_data())["hp"], 10)
        self.assertEqual(await self.state.get_state(), mobs.MobStates.add_hp.state)
        await self.text("100", reply_to=old_prompt)
        self.assertEqual((await self.state.get_data())["hp"], 100)
        self.assertEqual(await self.state.get_state(), mobs.MobStates.add_dust_min.state)
        self.assertNotIn("admin_pending_screen", await self.state.get_data())

    async def test_publication_retry_after_database_restart_returns_original_result(self):
        from aiogram.fsm.context import FSMContext
        from fsm_storage import SQLiteFSMStorage

        await self.resource_note("Единственная публикация")
        await self.text("Примечание")
        command = self.callback(self.action("isd:done"))
        with patch.object(self.state, "clear", AsyncMock(side_effect=RuntimeError("simulated crash"))):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                await self.route(command)
        reopened = Database(self.database_path)
        await reopened.connect()
        try:
            restored = FSMContext(SQLiteFSMStorage(reopened), self.state.key)
            router = admin.create_admin_router(RuntimeScope(reopened, frozenset({101})))
            await router.propagate_event(
                "callback_query",
                command,
                bot=self.bot,
                state=restored,
                raw_state=await restored.get_state(),
                event_from_user=self.user,
            )
            self.assertEqual(len(await reopened.get_resource_name_matches("Единственная публикация", "craft")), 1)
            self.assertEqual(await restored.get_state(), GenericEditStates.select_field.state)
        finally:
            await reopened.close()
