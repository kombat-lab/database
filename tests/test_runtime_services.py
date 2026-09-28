import unittest
from aiogram import types

from analytics import AnalyticsService
from database import Database
from maintenance import preview_retention
from runtime_scope import RuntimeScope, RuntimeScopeMiddleware, current_scope, database_for


class RuntimeServicesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.first = Database(":memory:")
        self.second = Database(":memory:")
        await self.first.connect()
        await self.second.connect()

    async def asyncTearDown(self):
        await self.first.close()
        await self.second.close()

    async def test_database_bound_analytics_does_not_use_default_instance(self):
        await self.first.register_user_if_not_exists(101, "First")
        await self.second.register_user_if_not_exists(202, "Second")
        first, second = AnalyticsService(self.first), AnalyticsService(self.second)
        await first.log_start(101)
        await second.log_start(202)
        self.assertEqual([row["user_id"] for row in await first.get_users_page()], [101])
        self.assertEqual([row["user_id"] for row in await second.get_users_page()], [202])

    async def test_page_totals_cover_selected_users_with_stable_pagination(self):
        service = AnalyticsService(self.first)
        for user_id in range(1, 15):
            await self.first.register_user_if_not_exists(user_id)
            for _ in range(user_id):
                await service.log_start(user_id)
        first = await service.get_users_page(offset=0, limit=5)
        second = await service.get_users_page(offset=5, limit=5)
        self.assertEqual([row["user_id"] for row in first], [14, 13, 12, 11, 10])
        self.assertEqual([row["user_id"] for row in second], [9, 8, 7, 6, 5])
        self.assertTrue(all(row["event_count"] == row["user_id"] for row in first + second))

    async def test_scope_resets_after_exception_and_fails_fast_outside_request(self):
        event = types.User(id=1, is_bot=False, first_name="Synthetic")
        scope = RuntimeScopeMiddleware(RuntimeScope(self.first, frozenset({101})))

        async def handler(event, data):
            self.assertIs(database_for(), self.first)
            self.assertEqual(current_scope().admin_ids, frozenset({101}))
            raise RuntimeError("synthetic")

        with self.assertRaises(RuntimeError):
            await scope(handler, event, {})
        with self.assertRaisesRegex(RuntimeError, "RuntimeScope is not bound"):
            current_scope()

    async def test_retention_preview_never_deletes_history(self):
        await self.first.register_user_if_not_exists(101)
        await self.first.execute_query(
            "INSERT INTO analytics_events(user_id,event_type,timestamp) VALUES (101,'start','2000-01-01')"
        )
        preview = await preview_retention(self.first)
        self.assertEqual(preview.analytics_events, 1)
        self.assertEqual((await AnalyticsService(self.first).get_db_stats())["events"], 1)
