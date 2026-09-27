import sqlite3
import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage

import admin_handlers as admin
import admin_item_sources as sources
import admin_utils
import ui.rich
from database import Database


class CatalogCreationFixture:
    async def asyncSetUp(self):
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.db = Database(getattr(self, "database_path", ":memory:"))
        await self.db.connect()
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=self.bot.id, chat_id=101, user_id=101))
        self.user = types.User(id=101, is_bot=False, first_name="Admin")
        self.next_id = 10
        self.latest = types.Message(message_id=10, date=1, chat=types.Chat(id=101, type="private"), text="Admin").as_(self.bot)
        self.answer = AsyncMock(side_effect=self.answer_message)
        self.edit = AsyncMock(side_effect=self.edit_message)
        for mocked in (
            patch.object(admin, "db", self.db), patch.object(admin, "ADMIN_IDS", [101]),
            patch.object(sources, "db", self.db),
            patch.dict(admin.ENTITY_CONFIGS, {
                kind: dict(admin.ENTITY_CONFIGS[kind], get_by_id_func=getattr(self.db, f"get_{kind}_by_id"))
                for kind in ("resource", "card")
            }),
            patch.object(types.Message, "answer", self.answer), patch.object(types.Message, "edit_text", self.edit),
            patch.object(types.CallbackQuery, "answer", AsyncMock()),
            patch.object(ui.rich, "present_rich_card", AsyncMock(side_effect=self.render_rich)),
        ):
            mocked.start()
            self.addCleanup(mocked.stop)

    async def asyncTearDown(self):
        await self.db.close()
        await self.storage.close()
        await self.bot.session.close()

    async def answer_message(self, text, **kwargs):
        self.next_id += 1
        self.latest = types.Message(
            message_id=self.next_id, date=1, chat=types.Chat(id=101, type="private"),
            text=text, reply_markup=kwargs.get("reply_markup"),
        ).as_(self.bot)
        return self.latest

    async def edit_message(self, text, **kwargs):
        self.latest = self.latest.model_copy(update={"text": text, "reply_markup": kwargs.get("reply_markup")}).as_(self.bot)
        return self.latest

    async def render_rich(self, *, card, reply_markup=None, **kwargs):
        return await self.edit_message(card.fallback_html, reply_markup=reply_markup)

    def callback(self, data, message=None):
        return types.CallbackQuery(id="test", data=data, message=message or self.latest, from_user=self.user, chat_instance="test").as_(self.bot)

    def action(self, prefix):
        return next(button.callback_data for row in self.latest.reply_markup.inline_keyboard for button in row if (button.callback_data or "").startswith(prefix))

    async def route(self, event):
        kind = "callback_query" if isinstance(event, types.CallbackQuery) else "message"
        return await admin.admin_router.propagate_event(
            kind, event, bot=self.bot, state=self.state, raw_state=await self.state.get_state(), event_from_user=self.user,
        )

    async def text(self, text, reply_to=None):
        message = types.Message(message_id=900, date=1, chat=types.Chat(id=101, type="private"), from_user=self.user,
                                text=text, reply_to_message=reply_to).as_(self.bot)
        return await self.route(message)

    async def start(self, kind, name="Synthetic"):
        await admin.begin_catalog_creation(self.callback("start"), self.state, kind)
        await self.text(name)
        await self.text("🪨")

    async def resource_note(self, name="Synthetic"):
        await self.start("resource", name)
        await self.route(self.callback(self.action("res_type_craft")))

    async def card_note(self, name="Synthetic"):
        await self.start("card", name)
        await self.route(self.callback(self.action("card_slot_")))
        for _ in range(4):
            await self.text("-")


class CatalogCreationSessionTests(CatalogCreationFixture, unittest.IsolatedAsyncioTestCase):
    async def test_old_type_slot_and_skip_buttons_cannot_change_new_creation(self):
        for kind, prefix in (("resource", "res_type_"), ("card", "card_slot_")):
            with self.subTest(kind=kind):
                await self.start(kind, "Draft A")
                old_message, old_button = self.latest, self.action(prefix)
                await self.start(kind, "Draft B")
                before = await self.state.get_data()
                await self.route(self.callback(old_button, old_message))
                self.assertEqual(await self.state.get_data(), before)
                await self.route(self.callback(prefix + ("craft" if kind == "resource" else "шлем")))
                self.assertEqual(await self.state.get_data(), before)
        for create in (self.resource_note, self.card_note):
            await create("Draft A")
            old_message, old_button = self.latest, self.action(admin.OPTIONAL_NOTE_SKIP_CALLBACK)
            await create("Draft B")
            before = await self.state.get_data()
            await self.route(self.callback(old_button, old_message))
            self.assertEqual(await self.state.get_data(), before)
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM resources"))[0]["n"], 0)
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM cards"))[0]["n"], 0)

    async def test_explicit_reply_to_old_prompt_is_rejected(self):
        await admin.begin_catalog_creation(self.callback("start"), self.state, "resource")
        old_prompt = self.latest
        await self.text("First name")
        before = await self.state.get_data()
        await self.text("🪨", reply_to=old_prompt)
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.emoji.state)

    async def test_back_preserves_values_and_cancel_discards_unpublished_creation(self):
        await self.resource_note("Original")
        await self.route(self.callback(self.action("catalog_create_back")))
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.type.state)
        self.assertEqual((await self.state.get_data())["res_name"], "Original")
        await self.route(self.callback(self.action("catalog_create_back")))
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.emoji.state)
        await self.route(self.callback(self.action("admin_cancel_edit")))
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.state.get_data(), {})
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM resources"))[0]["n"], 0)

    async def test_failed_next_prompt_preserves_previous_input_step(self):
        await admin.begin_catalog_creation(self.callback("start"), self.state, "resource")
        old_screen = (await self.state.get_data())["admin_screen"]
        old_prompt = self.latest
        failure = TelegramNetworkError(method=SendMessage(chat_id=101, text="next"), message="offline")
        with patch.object(types.Message, "answer", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.text("Accepted name")
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.name.state)
        self.assertEqual((await self.state.get_data())["admin_screen"], old_screen)
        await self.text("Unbound retry")
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.name.state)
        await self.text("Accepted name", reply_to=old_prompt)
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.emoji.state)

    async def test_database_failure_keeps_draft_and_successful_retry_inserts_once(self):
        await self.resource_note()
        await self.text("Note")
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM resources"))[0]["n"], 0)
        before = await self.state.get_data()
        save_button = self.action("isd:done")
        with patch.object(self.db, "create_resource_with_sources", AsyncMock(side_effect=sqlite3.OperationalError("synthetic write failure"))):
            await self.route(self.callback(save_button))
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)
        await self.route(self.callback(save_button))
        self.assertEqual(await self.state.get_state(), admin_utils.GenericEditStates.select_field.state)
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM resources"))[0]["n"], 1)

    async def test_delivery_failure_after_card_commit_cannot_repeat_insert(self):
        await self.card_note()
        await self.text("Note")
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM cards"))[0]["n"], 0)
        save = self.callback(self.action("isd:done"))
        failure = TelegramNetworkError(method=SendMessage(chat_id=101, text="saved"), message="offline")
        with patch.object(ui.rich, "present_rich_card", AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.route(save)
        self.assertNotEqual(await self.state.get_state(), sources.ItemSourcesStates.select.state)
        await self.route(save)
        await self.text("Retry note")
        self.assertEqual((await self.db.execute_query("SELECT COUNT(*) AS n FROM cards"))[0]["n"], 1)

    async def test_limits_and_unknown_slot_keep_current_step(self):
        await admin.begin_catalog_creation(self.callback("start"), self.state, "resource")
        await self.text("a" * (admin.MAX_RESOURCE_NAME_LENGTH + 1))
        self.assertEqual(await self.state.get_state(), admin.ResourceAddStates.name.state)
        self.assertNotIn("res_name", await self.state.get_data())
        await self.start("card")
        data = await self.state.get_data()
        token = data["admin_screen"]["token"]
        await self.route(self.callback("card_slot_invalid~" + token))
        self.assertEqual(await self.state.get_state(), admin.CardAddStates.slot.state)
        await self.route(self.callback(self.action("card_slot_")))
        await self.text("a" * (admin.MAX_CARD_BONUS_LENGTH + 1))
        self.assertEqual(await self.state.get_state(), admin.CardAddStates.bonus1.state)
