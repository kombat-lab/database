"""Exercise item-side drop editing through the real admin router and SQLite."""

import unittest
from unittest.mock import AsyncMock, patch

from aiogram import types
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, EditMessageText

import admin_handlers as admin
import admin_item_sources as sources
import admin_utils
import ui.rich
from tests.test_catalog_creation_sessions import CatalogCreationFixture


class ItemSourcesAdminTests(CatalogCreationFixture, unittest.IsolatedAsyncioTestCase):
    async def add_mob(self, name, location="Лес"):
        rows = await self.db.execute_query("SELECT id FROM locations WHERE name=?", (location,))
        location_id = int(rows[0]["id"]) if rows else await self.db.execute_insert(
            "INSERT INTO locations(name,emoji) VALUES (?,?)", (location, "📍"),
        )
        return await self.db.execute_insert(
            "INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES (?,?,?,?,?,?,?)",
            (name, "🐺", 10, 1, 2, 3, location_id),
        )

    async def open_creation_sources(self, kind, name="New object", *, skip_note=False):
        if kind == "resource":
            await self.resource_note(name)
        else:
            await self.card_note(name)
        if skip_note:
            await self.route(self.callback(self.action(admin.OPTIONAL_NOTE_SKIP_CALLBACK)))
        else:
            await self.text("Описание предмета")
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)

    async def open_existing(self, kind, item_id):
        entity = await getattr(self.db, f"get_{kind}_by_id")(item_id)
        await admin_utils.show_edit_menu(self.callback("open"), self.state, item_id, admin.ENTITY_CONFIGS[kind], entity)
        await self.route(self.callback(self.action("item_sources_open")))
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)

    def visible_mob_ids(self):
        return {
            int(button.callback_data.split("~")[0].removeprefix("isd:toggle:"))
            for row in self.latest.reply_markup.inline_keyboard for button in row
            if (button.callback_data or "").startswith("isd:toggle:")
        }

    async def select_mob(self, mob_id):
        await self.route(self.callback(self.action(f"isd:toggle:{mob_id}~")))

    async def drop_ids(self, kind, item_id):
        rows = await self.db.execute_query(
            "SELECT mob_id FROM drops WHERE item_type=? AND item_id=? ORDER BY mob_id", (kind, item_id),
        )
        return [int(row["mob_id"]) for row in rows]

    async def test_resources_and_cards_publish_only_after_confirming_sources(self):
        first = await self.add_mob("Волк")
        second = await self.add_mob("Кабан")
        for kind, table in (("resource", "resources"), ("card", "cards")):
            for with_sources in (False, True):
                with self.subTest(kind=kind, with_sources=with_sources):
                    name = f"{kind} {with_sources}"
                    await self.open_creation_sources(kind, name, skip_note=not with_sources)
                    if with_sources:
                        await self.select_mob(first)
                        await self.select_mob(second)
                    self.assertEqual(await self.db.execute_query(f"SELECT id FROM {table} WHERE name=?", (name,)), [])
                    save = self.callback(self.action("isd:done"))
                    await self.route(save)
                    await self.route(save)
                    rows = await self.db.execute_query(f"SELECT id,note FROM {table} WHERE name=?", (name,))
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["note"], "Описание предмета" if with_sources else "")
                    self.assertEqual(await self.drop_ids(kind, rows[0]["id"]), [first, second] if with_sources else [])
                    self.assertEqual(await self.state.get_state(), admin_utils.GenericEditStates.select_field.state)
                    self.assertIn("item_sources_open", self.action("item_sources_open"))

    async def test_back_preserves_creation_and_selected_sources_until_cancel(self):
        mob = await self.add_mob("Волк")
        for kind, table, note_state in (
            ("resource", "resources", admin.ResourceAddStates.note),
            ("card", "cards", admin.CardAddStates.note),
        ):
            with self.subTest(kind=kind):
                await self.open_creation_sources(kind)
                await self.select_mob(mob)
                await self.route(self.callback(self.action("isd:back")))
                self.assertEqual(await self.state.get_state(), note_state.state)
                self.assertEqual(await self.db.execute_query(f"SELECT id FROM {table}"), [])
                await self.text("Updated description")
                await self.route(self.callback(self.action("isd:selected")))
                self.assertEqual(self.visible_mob_ids(), {mob})
                await self.route(self.callback(self.action("admin_cancel_edit")))
                self.assertIsNone(await self.state.get_state())
                self.assertEqual(await self.state.get_data(), {})
                self.assertEqual(await self.db.execute_query(f"SELECT id FROM {table}"), [])

    async def test_existing_sources_can_be_replaced_or_removed_and_cancel_preserves_database(self):
        first = await self.add_mob("Волк")
        second = await self.add_mob("Кабан")
        for kind in ("resource", "card"):
            with self.subTest(kind=kind):
                if kind == "resource":
                    item_id = await self.db.add_resource("Сталь", "🧱")
                else:
                    item_id = await self.db.add_card("Карта", "🃏", "шлем")
                await self.db.add_drop(first, kind, item_id)
                await self.open_existing(kind, item_id)
                await self.select_mob(first)
                await self.select_mob(second)
                await self.route(self.callback(self.action("isd:back")))
                self.assertEqual(await self.drop_ids(kind, item_id), [first])
                self.assertEqual(await self.state.get_state(), admin_utils.GenericEditStates.select_field.state)
                await self.route(self.callback(self.action("item_sources_open")))
                await self.select_mob(first)
                await self.select_mob(second)
                await self.route(self.callback(self.action("isd:done")))
                self.assertEqual(await self.drop_ids(kind, item_id), [second])
                await self.route(self.callback(self.action("item_sources_open")))
                await self.select_mob(second)
                await self.route(self.callback(self.action("isd:done")))
                self.assertEqual(await self.drop_ids(kind, item_id), [])

    async def test_existing_sources_detect_other_editor_instead_of_overwriting(self):
        first = await self.add_mob("Волк")
        second = await self.add_mob("Кабан")
        item_id = await self.db.add_resource("Сталь", "🧱")
        await self.db.add_drop(first, "resource", item_id)
        await self.open_existing("resource", item_id)
        await self.select_mob(first)
        await self.db.add_drop(second, "resource", item_id)
        await self.route(self.callback(self.action("isd:done")))
        self.assertEqual(await self.drop_ids("resource", item_id), [first, second])
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)
        alerts = [call.args[0] for call in types.CallbackQuery.answer.await_args_list if call.args]
        self.assertTrue(any("изменены" in str(text) or "другим" in str(text) for text in alerts))

    async def test_old_picker_buttons_cannot_change_a_different_creation(self):
        mob = await self.add_mob("Волк")
        await self.open_creation_sources("resource", "Old resource")
        old_toggle = self.callback(self.action(f"isd:toggle:{mob}~"))
        old_save = self.callback(self.action("isd:done"))
        await self.open_creation_sources("card", "New card")
        before = await self.state.get_data()
        await self.route(old_toggle)
        await self.route(old_save)
        await self.route(self.callback(f"isd:toggle:{mob}"))
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.db.execute_query("SELECT id FROM resources"), [])
        self.assertEqual(await self.db.execute_query("SELECT id FROM cards"), [])
        await self.route(self.callback(self.action("isd:done")))
        card_id = (await self.db.execute_query("SELECT id FROM cards"))[0]["id"]
        self.assertEqual(await self.drop_ids("card", card_id), [])

    async def test_picker_searches_mob_and_location_and_keeps_cross_page_selection(self):
        forest_mobs = [await self.add_mob(f"Лесной зверь {index:02}") for index in range(10)]
        cave_mob = await self.add_mob("Слепая мышь", "Пещера")
        await self.open_creation_sources("resource")
        self.assertEqual(len(self.visible_mob_ids()), 8)
        first = min(self.visible_mob_ids())
        await self.select_mob(first)
        await self.route(self.callback(self.action("isd:page:")))
        self.assertTrue(self.visible_mob_ids() - {first})
        second = max(self.visible_mob_ids())
        await self.select_mob(second)
        await self.route(self.callback(self.action("isd:search")))
        await self.text("пЕщЕрА")
        self.assertEqual(self.visible_mob_ids(), {cave_mob})
        if second != cave_mob:
            await self.select_mob(cave_mob)
        await self.route(self.callback(self.action("isd:search")))
        await self.text("Лесной зверь 00")
        self.assertEqual(self.visible_mob_ids(), {forest_mobs[0]})
        await self.route(self.callback(self.action("isd:clear")))
        await self.route(self.callback(self.action("isd:selected")))
        expected = {first, second, cave_mob}
        self.assertEqual(self.visible_mob_ids(), expected)
        await self.route(self.callback(self.action("isd:done")))
        item_id = (await self.db.execute_query("SELECT id FROM resources"))[0]["id"]
        self.assertEqual(await self.drop_ids("resource", item_id), sorted(expected))

    async def test_deleted_selected_mob_rolls_back_item_and_all_links(self):
        first = await self.add_mob("Волк")
        second = await self.add_mob("Кабан")
        for kind, table in (("resource", "resources"), ("card", "cards")):
            with self.subTest(kind=kind):
                if kind == "card":
                    second = await self.add_mob("Кабан повторно")
                await self.open_creation_sources(kind)
                await self.select_mob(first)
                await self.select_mob(second)
                await self.db.execute_query("DELETE FROM mobs WHERE id=?", (second,))
                await self.route(self.callback(self.action("isd:done")))
                self.assertEqual(await self.db.execute_query(f"SELECT id FROM {table}"), [])
                self.assertEqual(await self.db.execute_query("SELECT mob_id FROM drops"), [])
                self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)

    async def test_failed_picker_delivery_keeps_note_step_retryable(self):
        await self.resource_note()
        old_screen = (await self.state.get_data())["admin_screen"]
        failure = TelegramNetworkError(method=EditMessageText(text="sources", chat_id=101, message_id=10), message="offline")
        with patch.object(types.Message, "answer", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.text("Описание")
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.note.state)
        self.assertEqual((await self.state.get_data())["admin_screen"], old_screen)
        self.assertEqual(await self.db.execute_query("SELECT id FROM resources"), [])
        await self.text("Описание")
        await self.route(self.callback(self.action("isd:done")))
        self.assertEqual(len(await self.db.execute_query("SELECT id FROM resources")), 1)

    async def test_failed_toggle_delivery_keeps_visible_selection_and_retry_selects_once(self):
        mob = await self.add_mob("Волк")
        await self.open_creation_sources("resource")
        before = await self.state.get_data()
        toggle = self.callback(self.action(f"isd:toggle:{mob}~"))
        failure = TelegramNetworkError(
            method=EditMessageText(text="selected", chat_id=101, message_id=self.latest.message_id),
            message="offline",
        )
        with patch.object(types.Message, "edit_text", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.route(toggle)
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual((await sources.current_selection(self.state)).selected, [])
        await self.route(toggle)
        self.assertEqual((await sources.current_selection(self.state)).selected, [mob])
        await self.route(self.callback(self.action("isd:done")))
        item_id = (await self.db.execute_query("SELECT id FROM resources"))[0]["id"]
        self.assertEqual(await self.drop_ids("resource", item_id), [mob])

    async def test_failed_back_delivery_keeps_picker_and_unsaved_selection_usable(self):
        mob = await self.add_mob("Волк")
        item_id = await self.db.add_resource("Сталь", "🧱")
        await self.open_existing("resource", item_id)
        await self.select_mob(mob)
        before = await self.state.get_data()
        save = self.callback(self.action("isd:done"))
        back = self.callback(self.action("isd:back"))
        failure = TelegramNetworkError(
            method=EditMessageText(text="item", chat_id=101, message_id=self.latest.message_id),
            message="offline",
        )
        with patch.object(ui.rich, "present_rich_card", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.route(back)
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)
        self.assertEqual(await self.drop_ids("resource", item_id), [])
        await self.route(save)
        self.assertEqual(await self.drop_ids("resource", item_id), [mob])
        self.assertEqual(await self.state.get_state(), admin_utils.GenericEditStates.select_field.state)

    async def test_failed_back_acknowledgement_keeps_successfully_shown_editor(self):
        mob = await self.add_mob("Волк")
        item_id = await self.db.add_resource("Сталь", "🧱")
        await self.open_existing("resource", item_id)
        await self.select_mob(mob)
        old_screen = (await self.state.get_data())["admin_screen"]
        old_save = self.callback(self.action("isd:done"))
        back = self.callback(self.action("isd:back"))
        failure = TelegramNetworkError(method=AnswerCallbackQuery(callback_query_id=back.id), message="offline")
        with patch.object(types.CallbackQuery, "answer", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.route(back)
        self.assertEqual(await self.state.get_state(), admin_utils.GenericEditStates.select_field.state)
        self.assertNotEqual((await self.state.get_data())["admin_screen"], old_screen)
        self.assertEqual(await self.drop_ids("resource", item_id), [])
        await self.route(old_save)
        self.assertEqual(await self.drop_ids("resource", item_id), [])
        await self.route(self.callback(self.action("item_sources_open")))
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)
        self.assertEqual((await sources.current_selection(self.state)).selected, [])
