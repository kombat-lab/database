import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from aiogram.types import InlineQuery, User
from catalog_reads import load_snapshot, search_all
from database import Database
from inline_search import InlinePage, InlineSearchService
from public_catalog import PublicCatalogHandlers
from public_presentation import PublicContext
from ui.cards import build_card_card, build_gear_card, build_mob_card, build_resource_card


class InlineCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()

    async def test_exact_match_and_all_pages_remain_reachable(self):
        for index in range(120):
            await self.db.add_resource(f"Ore variant {index:03}", "")
        exact = await self.db.add_resource("Ore", "")
        location = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Mine','⛏')")
        await self.db.add_mob("Ore beast", "🐾", 10, 1, 2, 3, location)
        await self.db.add_gear("Ore helm", "rare", "шлем", "🛡")
        await self.db.add_card("Ore card", "🃏", "шлем")
        pages = [await search_all(self.db, "ore", offset=offset, limit=50) for offset in (0, 50, 100)]
        self.assertEqual(pages[0][0].item["id"], exact)
        identities = [(entry.kind, entry.item["id"]) for page in pages for entry in page]
        self.assertEqual(len(identities), 124)
        self.assertEqual(len(set(identities)), 124)
        self.assertEqual({kind for kind, _ in identities}, {"mob", "resource", "gear", "card"})
        self.assertEqual(await search_all(self.db, "%_"), [])

    async def test_fifty_resources_use_bounded_queries_and_revision_invalidates_cache(self):
        for index in range(50):
            await self.db.add_resource(f"Ore {index:02}", "")
        service = InlineSearchService(self.db)
        with patch.object(self.db, "execute_query", wraps=self.db.execute_query) as query:
            first = await service.page("Ore", 0, "bot")
        self.assertEqual(len(first.results), 50)
        self.assertLessEqual(query.await_count, 8, query.await_count)
        with patch.object(self.db, "execute_query", wraps=self.db.execute_query) as query:
            self.assertIs(await service.page("Ore", 0, "bot"), first)
        self.assertEqual(query.await_count, 1)
        await self.db.update_resource(1, note="Changed note")
        updated = await service.page("Ore", 0, "bot")
        self.assertIsNot(updated, first)
        self.assertIn("Changed note", updated.results[0].input_message_content.rich_message.html)
        self.assertEqual(updated.next_offset, "")

    async def test_old_render_cannot_evict_newer_revision_cache(self):
        resource = await self.db.add_resource("Ore", "")
        service = InlineSearchService(self.db)
        started, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def controlled_builder(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return await build_resource_card(*args, **kwargs)

        with patch("inline_search.build_resource_card", side_effect=controlled_builder):
            old = asyncio.create_task(service.page("Ore", 0, "bot"))
            await started.wait()
            await self.db.update_resource(resource, note="Newest")
            newest = await service.page("Ore", 0, "bot")
            release.set()
            stale = await old
        self.assertNotIn("Newest", stale.results[0].input_message_content.rich_message.html)
        self.assertIn("Newest", newest.results[0].input_message_content.rich_message.html)
        self.assertIs(await service.page("Ore", 0, "bot"), newest)

    async def test_cache_invalidates_for_linked_source_ingredient_and_owner_changes(self):
        material = await self.db.add_resource("Ore", "🪨")
        scroll = await self.db.add_resource("Scroll", "📜", "scroll_recipe")
        gear = await self.db.add_gear("Ore helm", "rare", "шлем", "🛡")
        recipe = await self.db.create_recipe("gear", gear)
        await self.db.add_ingredient(recipe, material, 2)
        await self.db.set_recipe_learning_scroll(recipe, scroll)
        location = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Cave','🪨')")
        mob = await self.db.execute_insert(
            "INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES ('Beast','🐾',10,1,3,5,?)",
            (location,),
        )
        service = InlineSearchService(self.db)
        first = await service.page("Ore helm", 0, "bot")
        await self.db.set_item_drop_sources("resource", scroll, [mob])
        sources = await service.page("Ore helm", 0, "bot")
        self.assertIsNot(first, sources)
        self.assertIn("Beast", sources.results[0].input_message_content.rich_message.html)
        await self.db.claim_recipe_owner(recipe, 101, None, expected_gear_id=gear)
        owners = await service.page("Ore helm", 0, "bot")
        self.assertIn("tg://user?id=101", owners.results[0].input_message_content.rich_message.html)
        await self.db.update_resource(material, name="Renamed ore")
        materials = await service.page("Ore helm", 0, "bot")
        self.assertIn("Renamed ore", materials.results[0].input_message_content.rich_message.html)

    async def test_cache_respects_size_and_page_bounds(self):
        await self.db.add_resource("Ore", "")
        tiny = InlineSearchService(self.db, max_bytes=1)
        first = await tiny.page("Ore", 0, "bot")
        second = await tiny.page("Ore", 0, "bot")
        self.assertIsNot(first, second)
        bounded = InlineSearchService(self.db, max_pages=1)
        await bounded.page("Ore", 0, "bot")
        await bounded.page("Or", 0, "bot")
        self.assertLessEqual(len(bounded._cache), 1)
        self.assertLessEqual(bounded._weight, bounded.max_bytes)

    async def test_batch_cards_equal_single_entity_readers(self):
        location = await self.db.execute_insert("INSERT INTO locations(name,emoji) VALUES ('Cave','C')")
        mob = await self.db.execute_insert(
            "INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES ('Ore beast','M',10,1,3,5,?)",
            (location,),
        )
        material = await self.db.add_resource("Ore", "🪨")
        product = await self.db.add_resource("Ore alloy", "⚙️", "alchemy")
        scroll = await self.db.add_resource("Ore scroll", "📜", "scroll_recipe")
        gear = await self.db.add_gear("Ore helm", "rare", "шлем", "🛡")
        card = await self.db.add_card("Ore card", "🃏", "шлем", "bonus")
        formula = await self.db.create_recipe("resource", product, 3)
        await self.db.add_ingredient(formula, material, 2)
        recipe = await self.db.create_recipe("gear", gear, 2)
        await self.db.add_ingredient(recipe, material, 7)
        await self.db.set_recipe_learning_scroll(recipe, scroll)
        await self.db.claim_recipe_owner(recipe, 101, None, expected_gear_id=gear)
        for kind, entity_id in [("gear", gear), ("resource", scroll), ("resource", material), ("card", card)]:
            await self.db.set_item_drop_sources(kind, entity_id, [mob])
        entries = await search_all(self.db, "Ore")
        async with self.db.transaction():
            snapshot = await load_snapshot(self.db, entries)
        builders = {
            "mob": build_mob_card,
            "resource": build_resource_card,
            "gear": build_gear_card,
            "card": build_card_card,
        }
        for entry in entries:
            with self.subTest(kind=entry.kind, id=entry.item["id"]):
                builder = builders[entry.kind]
                expected = await builder(self.db, entry.item["id"], bot_username="bot")
                with patch.object(self.db, "execute_query", side_effect=AssertionError("N+1 SQL")):
                    actual = await builder(snapshot, entry.item["id"], bot_username="bot")
                self.assertEqual(actual, expected)

    async def test_contexts_do_not_share_storage_or_router(self):
        second = Database(":memory:")
        await second.connect()
        try:
            await self.db.add_resource("First ore", "")
            await second.add_resource("Second ore", "")
            one = PublicCatalogHandlers(PublicContext(self.db))
            two = PublicCatalogHandlers(PublicContext(second))
            self.assertIsNot(one.router, two.router)
            self.assertIsNot(one.entity_navigation, two.entity_navigation)
            self.assertEqual((await one.inline_search.page("ore", 0, "bot")).results[0].title, "First ore")
            self.assertEqual((await two.inline_search.page("ore", 0, "bot")).results[0].title, "Second ore")
        finally:
            await second.close()

    async def test_new_query_cancels_old_rendering_but_keeps_final_analytics(self):
        handler = PublicCatalogHandlers(PublicContext(self.db))
        started = asyncio.Event()
        logged = asyncio.Event()

        async def render(query, *args):
            if query == "old":
                started.set()
                await asyncio.Event().wait()
            return InlinePage((), "")

        async def log(user_id, query):
            self.assertEqual(query, "new")
            logged.set()

        user = User(id=101, is_bot=False, first_name="Player")

        def query(text, offset=""):
            return InlineQuery(id=text, from_user=user, query=text, offset=offset)

        with (
            patch.object(handler.inline_search, "page", side_effect=render),
            patch.object(handler.analytics, "log_inline_search", side_effect=log),
            patch.object(InlineQuery, "answer", new=AsyncMock()) as answer,
        ):
            old = asyncio.create_task(handler.inline_search_handler(query("old")))
            await started.wait()
            await handler.inline_search_handler(query("new"))
            await asyncio.gather(old, return_exceptions=True)
            self.assertTrue(old.cancelled())
            await asyncio.wait_for(logged.wait(), 2)
            answer.assert_awaited_once()
        await handler.background_tasks.close()

    async def test_short_first_page_query_cancels_pending_analytics_log(self):
        for offset in ("", "0"):
            with self.subTest(offset=offset):
                handler = PublicCatalogHandlers(PublicContext(self.db))
                previous = asyncio.create_task(asyncio.Event().wait())
                handler.inline_log_tasks[9876] = previous
                query = InlineQuery(
                    id=f"shortened-{offset}",
                    from_user=User(id=9876, is_bot=False, first_name="Tester"),
                    query="x",
                    offset=offset,
                )
                try:
                    with (
                        patch.object(InlineQuery, "answer", new=AsyncMock()) as answer,
                        patch.object(self.db, "search", new=AsyncMock()) as search,
                    ):
                        await handler.inline_search_handler(query)
                    await asyncio.gather(previous, return_exceptions=True)
                    self.assertTrue(previous.cancelled())
                    self.assertNotIn(9876, handler.inline_log_tasks)
                    search.assert_not_awaited()
                    answer.assert_awaited_once()
                finally:
                    previous.cancel()
                    await asyncio.gather(previous, return_exceptions=True)
                    await handler.background_tasks.close()

    async def test_pagination_keeps_pending_first_page_analytics_log(self):
        handler = PublicCatalogHandlers(PublicContext(self.db))
        previous = asyncio.create_task(asyncio.Event().wait())
        handler.inline_log_tasks[9877] = previous
        query = InlineQuery(
            id="next-page",
            from_user=User(id=9877, is_bot=False, first_name="Tester"),
            query="valid query",
            offset="50",
        )
        try:
            with (
                patch.object(InlineQuery, "answer", new=AsyncMock()),
                patch.object(handler.inline_search, "page", new=AsyncMock(return_value=InlinePage((), ""))),
            ):
                await handler.inline_search_handler(query)
            self.assertIs(handler.inline_log_tasks[9877], previous)
            self.assertFalse(previous.done())
        finally:
            handler.inline_log_tasks.pop(9877, None)
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
            await handler.background_tasks.close()
