import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import InlineQuery, User

from tests.public_fixture import app
from inline_search import InlinePage


class InlineDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_shortened_first_page_query_cancels_previous_log(self):
        for offset in ("", "0"):
            with self.subTest(offset=offset):
                previous = asyncio.create_task(asyncio.Event().wait())
                app.inline_log_tasks[9876] = previous
                query = InlineQuery(
                    id="shortened", from_user=User(id=9876, is_bot=False, first_name="Tester"),
                    query="x", offset=offset,
                )
                with patch.object(InlineQuery, "answer", new=AsyncMock()) as answer, \
                     patch.object(app.db, "search", new=AsyncMock()) as search:
                    await app.inline_search_handler(query)
                await asyncio.gather(previous, return_exceptions=True)
                self.assertTrue(previous.cancelled())
                self.assertNotIn(9876, app.inline_log_tasks)
                search.assert_not_awaited()
                answer.assert_awaited_once()

    async def test_pagination_keeps_pending_first_page_log(self):
        previous = asyncio.create_task(asyncio.Event().wait())
        app.inline_log_tasks[9877] = previous
        query = InlineQuery(
            id="next-page", from_user=User(id=9877, is_bot=False, first_name="Tester"),
            query="valid query", offset="50",
        )
        try:
            with patch.object(InlineQuery, "answer", new=AsyncMock()), \
                 patch.object(app.inline_search, "page", new=AsyncMock(return_value=InlinePage((), ""))):
                await app.inline_search_handler(query)
            self.assertIs(app.inline_log_tasks[9877], previous)
            self.assertFalse(previous.done())
        finally:
            app.inline_log_tasks.pop(9877, None)
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
