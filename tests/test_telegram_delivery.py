
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendRichMessage
from aiogram.types import InputRichMessage, Message
from aiogram.utils.formatting import Bold, Text, TextLink

import messaging
from messaging import cleanup_card_fragments, replace_rich_card, upsert_rich_card
from search_rendering import build_search_content, ranked_inline_items
from telegram_text import split_entities, split_formatted_text, split_html, utf16_length


class TelegramTextTests(unittest.TestCase):
    def test_nested_html_and_entities_survive_boundaries_without_text_loss(self):
        raw = ("🧙🏽‍♂️ & <stone>\n" * 700) + "last"
        content = Text(Bold("prefix ", TextLink(raw, url="https://example.com")), " suffix")
        original, entities = content.render()
        chunks = split_formatted_text(content, limit=79)
        self.assertEqual("".join(c.text for c in chunks), original)
        self.assertGreater(len(chunks), 10)
        for chunk in chunks:
            self.assertLessEqual(utf16_length(chunk.text), 79)
            self.assertTrue(chunk.text)
            for entity in chunk.entities:
                self.assertGreater(entity.length, 0)
                self.assertGreaterEqual(entity.offset, 0)
                self.assertLessEqual(entity.offset + entity.length, utf16_length(chunk.text))
            reparsed = split_html(chunk.as_html(), limit=79)
            self.assertEqual("".join(c.text for c in reparsed), chunk.text)

    def test_html_parser_handles_escaped_input_and_empty_tags(self):
        html = "<b><i>🪨 &lt;broken&gt;</i> &amp; more</b><i></i>\n<tg-spoiler>secret</tg-spoiler>"
        chunks = split_html(html, limit=9)
        self.assertEqual("".join(c.text for c in chunks), "🪨 <broken> & more\nsecret")
        self.assertTrue(any(e.type == "spoiler" for c in chunks for e in c.entities))

    def test_boundary_never_splits_astral_character(self):
        text = "a" * 4095 + "😀" + "b" * 4096
        chunks = split_entities(text)
        self.assertEqual([utf16_length(c.text) for c in chunks], [4095, 4096, 2])
        self.assertEqual("".join(c.text for c in chunks), text)

    def test_invalid_limits_and_malformed_html_fail_before_send(self):
        with self.assertRaises(ValueError):
            split_entities("x", limit=1)
        with self.assertRaises(ValueError):
            split_html("<b>unclosed")
        with self.assertRaises(ValueError):
            split_html("<invalid>tag</invalid>")

    def test_large_search_is_bounded_and_database_values_are_literal_text(self):
        results = {group: [
            {"id": i + 1, "name": "<b>result & data</b>" * 4, "emoji": "<broken>", "slot": "<slot>"}
            for i in range(50)
        ] for group in ("mobs", "resources", "gear", "cards")}
        content = build_search_content(results, "test_bot")
        chunks = split_formatted_text(content)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(utf16_length(c.text) <= 4096 for c in chunks))
        text = "".join(c.text for c in chunks)
        self.assertEqual(text.count("<broken>"), 200)
        self.assertIn("&lt;broken&gt;", chunks[0].as_html())

    def test_inline_order_keeps_all_types_reachable_and_exact_match_first(self):
        results = {
            "mobs": [{"id": i + 1, "name": f"Mob {i}", "emoji": ""} for i in range(50)],
            "resources": [{"id": 1, "name": "Mob", "emoji": ""}],
            "gear": [{"id": 1, "name": "Mob gear", "emoji": ""}],
            "cards": [{"id": 1, "name": "Mob card", "emoji": ""}],
        }
        entries = ranked_inline_items(results, "Mob")
        self.assertEqual(entries[0][0], "resource")
        self.assertEqual({kind for kind, _ in entries}, {"mob", "resource", "gear", "card"})
        self.assertEqual(len(entries), 53)


class CardDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        messaging._card_fragments.clear()

    def setup_card(self):
        bot, old = AsyncMock(), AsyncMock()
        bot.id = 701
        old.message_id = 10
        old.message_thread_id = 7
        rich = InputRichMessage(html="<b>Card</b>")
        return bot, old, rich, SendRichMessage(chat_id=1, rich_message=rich)

    async def test_ambiguous_network_error_preserves_old_card_without_second_send(self):
        bot, old, rich, method = self.setup_card()
        bot.send_rich_message.side_effect = TelegramNetworkError(method=method, message="timeout")
        with self.assertRaises(TelegramNetworkError):
            await replace_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text="Card",
                                    reply_markup=None, current_message=old)
        old.delete.assert_not_awaited()
        bot.send_message.assert_not_awaited()

    async def test_flood_control_is_not_retried_as_a_different_format(self):
        bot, old, rich, method = self.setup_card()
        bot.edit_message_text.side_effect = TelegramRetryAfter(method=method, message="slow down", retry_after=60)
        with self.assertRaises(TelegramRetryAfter):
            await upsert_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text="Card",
                                  current_message=old)
        old.delete.assert_not_awaited()
        bot.send_rich_message.assert_not_awaited()
        self.assertEqual(bot.edit_message_text.await_count, 1)

    async def test_failed_text_fallback_preserves_previous_navigation(self):
        bot, old, rich, method = self.setup_card()
        bot.send_rich_message.side_effect = TelegramBadRequest(method=method, message="unsupported format")
        bot.send_message.side_effect = TelegramNetworkError(method=method, message="offline")
        with self.assertRaises(TelegramNetworkError):
            await replace_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text="Card",
                                    reply_markup=None, current_message=old)
        old.delete.assert_not_awaited()

    async def test_long_fallback_keeps_all_text_and_topic_before_deleting_old(self):
        bot, old, rich, method = self.setup_card()
        events = []
        async def send(**kwargs):
            events.append(("send", kwargs))
            sent = AsyncMock(spec=Message)
            sent.message_id = 10 + len(events)
            sent.message_thread_id = 7
            return sent
        async def delete():
            events.append(("delete", {}))
        bot.send_rich_message.side_effect = TelegramBadRequest(method=method, message="unsupported format")
        bot.send_message.side_effect = send
        old.delete.side_effect = delete
        raw = "🪨" * 6000
        await replace_rich_card(bot=bot, chat_id=1, rich_message=rich, plain_text=f"<b>{raw}</b>",
                                reply_markup=None, current_message=old)
        self.assertEqual(events[-1][0], "delete")
        messages = [kwargs for event, kwargs in events if event == "send"]
        self.assertEqual("".join(m["text"] for m in messages), raw)
        self.assertTrue(all(m["message_thread_id"] == 7 for m in messages))
        self.assertTrue(all(utf16_length(m["text"]) <= 4096 for m in messages))


class MultipartCardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        messaging._card_fragments.clear()
        self.bot = AsyncMock()
        self.bot.id = 702
        self.rich = InputRichMessage(html="<b>Card</b>")
        self.method = SendRichMessage(chat_id=1, rich_message=self.rich)
        self.events = []
        self.sent = []
        self.old = self.message(10)
        self.bot.send_rich_message.side_effect = TelegramBadRequest(method=self.method, message="unsupported")
        self.bot.send_message.side_effect = self.send
        self.bot.delete_message.side_effect = self.delete_fragment

    def message(self, message_id):
        message = AsyncMock(spec=Message)
        message.message_id = message_id
        message.message_thread_id = 7
        async def remove():
            self.events.append(("delete", message_id))
        message.delete = AsyncMock(side_effect=remove)
        return message

    async def send(self, **kwargs):
        message = self.message(11 + len(self.sent))
        self.sent.append(message)
        self.events.append(("send", message.message_id))
        return message

    async def delete_fragment(self, *, chat_id, message_id):
        self.events.append(("delete", message_id))
        return True

    async def card_a(self):
        return await replace_rich_card(
            bot=self.bot, chat_id=1, rich_message=self.rich, plain_text="A" * 5000,
            reply_markup=None, current_message=self.old,
        )

    async def test_replace_removes_every_part_of_previous_card_after_new_send(self):
        anchor = await self.card_a()
        self.events.clear()
        await replace_rich_card(
            bot=self.bot, chat_id=1, rich_message=self.rich, plain_text="B",
            reply_markup=None, current_message=anchor,
        )
        self.assertEqual(self.events, [("send", 13), ("delete", 11), ("delete", 12)])
        self.assertEqual(messaging._card_fragments, {})

    async def test_successful_upsert_removes_extras_but_keeps_edited_anchor(self):
        anchor = await self.card_a()
        self.events.clear()
        async def edit(**kwargs):
            self.events.append(("edit", kwargs['message_id']))
            return anchor
        self.bot.edit_message_text.side_effect = edit
        result = await upsert_rich_card(
            bot=self.bot, chat_id=1, rich_message=self.rich, plain_text="B", current_message=anchor,
        )
        self.assertIs(result, anchor)
        self.assertEqual(self.events, [("edit", 12), ("delete", 11)])
        anchor.delete.assert_not_awaited()

    async def test_failed_replacement_preserves_complete_previous_card_and_metadata(self):
        anchor = await self.card_a()
        self.events.clear()
        failure = TelegramNetworkError(method=self.method, message="ambiguous")
        self.bot.send_rich_message.side_effect = failure
        with self.assertRaises(TelegramNetworkError):
            await replace_rich_card(
                bot=self.bot, chat_id=1, rich_message=self.rich, plain_text="B",
                reply_markup=None, current_message=anchor,
            )
        self.assertEqual(self.events, [])
        self.assertIn((702, 1, 12), messaging._card_fragments)
        anchor.delete.assert_not_awaited()
        await cleanup_card_fragments(self.bot, 1, anchor.message_id)
        self.assertEqual(self.events, [("delete", 11)])

    async def test_rate_limited_upsert_preserves_all_parts(self):
        anchor = await self.card_a()
        self.events.clear()
        self.bot.edit_message_text.side_effect = TelegramRetryAfter(
            method=self.method, message="rate limit", retry_after=1,
        )
        with self.assertRaises(TelegramRetryAfter):
            await upsert_rich_card(
                bot=self.bot, chat_id=1, rich_message=self.rich, plain_text="B", current_message=anchor,
            )
        self.assertEqual(self.events, [])
        self.assertIn((702, 1, 12), messaging._card_fragments)

    async def test_partial_failure_removes_only_confirmed_new_parts(self):
        anchor = await self.card_a()
        self.events.clear()
        confirmed = self.message(13)
        self.bot.send_message.side_effect = [
            confirmed, TelegramNetworkError(method=self.method, message="second send ambiguous"),
        ]
        with self.assertRaises(TelegramNetworkError):
            await replace_rich_card(
                bot=self.bot, chat_id=1, rich_message=self.rich, plain_text="B" * 5000,
                reply_markup=None, current_message=anchor,
            )
        self.assertEqual(self.events, [("delete", 13)])
        self.assertIn((702, 1, 12), messaging._card_fragments)
        anchor.delete.assert_not_awaited()

    async def test_cleanup_is_best_effort_and_scoped_by_bot_chat_and_anchor(self):
        anchor = await self.card_a()
        self.events.clear()
        other_bot = AsyncMock()
        other_bot.id = 999
        await cleanup_card_fragments(other_bot, 1, anchor.message_id)
        await cleanup_card_fragments(self.bot, 2, anchor.message_id)
        self.bot.delete_message.assert_not_awaited()
        self.bot.delete_message.side_effect = TelegramBadRequest(method=self.method, message="already deleted")
        with self.assertLogs("messaging", level="WARNING"):
            await cleanup_card_fragments(self.bot, 1, anchor.message_id)
        self.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=11)
        anchor.delete.assert_not_awaited()

    async def test_registry_bounds_metadata_by_card_count_ids_and_age(self):
        with patch.object(messaging, "_MAX_TRACKED_CARDS", 2), patch.object(
            messaging, "_MAX_TRACKED_FRAGMENT_IDS", 3
        ), patch.object(messaging.time, "monotonic", return_value=10):
            messaging._remember_fragments(self.bot, 1, 3, [1, 2])
            messaging._remember_fragments(self.bot, 1, 6, [4, 5])
            self.assertNotIn((702, 1, 3), messaging._card_fragments)
            messaging._remember_fragments(self.bot, 1, 8, [7])
            self.assertEqual(len(messaging._card_fragments), 2)
            self.assertLessEqual(sum(len(row.message_ids) for row in messaging._card_fragments.values()), 3)
        with patch.object(messaging.time, "monotonic", return_value=10 + messaging._FRAGMENT_TTL_SECONDS + 1):
            await cleanup_card_fragments(self.bot, 1, 8)
        self.assertEqual(messaging._card_fragments, {})
        self.bot.delete_message.assert_not_awaited()
