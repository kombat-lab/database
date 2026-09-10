import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText

import bot as app


class CardFragmentNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.message = types.Message(
            message_id=30, date=1, chat=types.Chat(id=101, type="private"), text="last part",
        ).as_(self.bot)
        self.callback = types.CallbackQuery(
            id="next", data="cards_page_1", chat_instance="test", message=self.message,
            from_user=types.User(id=101, is_bot=False, first_name="Player"),
        ).as_(self.bot)
        self.failure = TelegramNetworkError(
            method=EditMessageText(chat_id=101, message_id=30, text="next"), message="offline",
        )

    async def asyncTearDown(self):
        await self.bot.session.close()

    async def test_return_to_text_cleans_fragments_after_success(self):
        events = []
        async def edit(*args, **kwargs):
            events.append("edit")
        async def cleanup(*args, **kwargs):
            events.append("cleanup")
        with patch.object(types.Message, "edit_text", new=AsyncMock(side_effect=edit)), patch.object(
            app, "cleanup_card_fragments", new=AsyncMock(side_effect=cleanup),
        ) as cleaner:
            await app.replace_callback_message_text(self.callback, "list")
        self.assertEqual(events, ["edit", "cleanup"])
        cleaner.assert_awaited_once_with(self.bot, 101, 30)

    async def test_failed_text_transition_keeps_entire_previous_card(self):
        with patch.object(types.Message, "edit_text", new=AsyncMock(side_effect=self.failure)), patch.object(
            app, "cleanup_card_fragments", new=AsyncMock(),
        ) as cleaner:
            with self.assertRaises(TelegramNetworkError):
                await app.replace_callback_message_text(self.callback, "list")
        cleaner.assert_not_awaited()

    async def test_card_and_resource_lists_use_fragment_cleanup(self):
        with patch.object(types.Message, "edit_text", new=AsyncMock()), patch.object(
            app.db, "get_all_cards_sorted_by_slot", new=AsyncMock(return_value=[]),
        ), patch.object(app.db, "get_resources_by_type", new=AsyncMock(return_value=[])), patch.object(
            app, "cleanup_card_fragments", new=AsyncMock(),
        ) as cleaner:
            await app.show_cards_list(self.callback, 1)
            await app.show_resources_by_type(self.callback, "craft", 1)
        self.assertEqual(cleaner.await_count, 2)

    async def test_main_menu_cleans_fragments_only_after_send(self):
        events = []
        async def send(*args, **kwargs):
            events.append("send")
        async def cleanup(*args, **kwargs):
            events.append("cleanup")
        async def delete(*args, **kwargs):
            events.append("delete")
        with patch.object(types.Message, "answer", new=AsyncMock(side_effect=send)), patch.object(
            types.Message, "delete", new=AsyncMock(side_effect=delete),
        ), patch.object(types.CallbackQuery, "answer", new=AsyncMock()), patch.object(
            app, "cleanup_card_fragments", new=AsyncMock(side_effect=cleanup),
        ):
            await app.back_to_main_menu(self.callback, AsyncMock())
        self.assertEqual(events, ["send", "cleanup", "delete"])

    async def test_failed_main_menu_keeps_all_messages(self):
        with patch.object(types.Message, "answer", new=AsyncMock(side_effect=self.failure)), patch.object(
            types.Message, "delete", new=AsyncMock(),
        ) as delete, patch.object(app, "cleanup_card_fragments", new=AsyncMock()) as cleaner:
            with self.assertRaises(TelegramNetworkError):
                await app.back_to_main_menu(self.callback, AsyncMock())
        cleaner.assert_not_awaited()
        delete.assert_not_awaited()
