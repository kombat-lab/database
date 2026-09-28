import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from database import Database
from public_catalog import PublicCatalogHandlers
from public_presentation import PublicContext
from ui.callbacks import (
    CardViewCallback,
    EntityBackCallback,
    EntityNavigateCallback,
    GearViewCallback,
    MAX_SQLITE_ID,
    MobViewCallback,
    RecipeOwnerCallback,
    ResourceViewCallback,
    parse_return_param,
)
from ui.cards import build_gear_card, build_resource_card
from ui.links import EntityLinkMode
from ui.navigation import EntityNavigationHistory, EntityRef
from ui.rich import CardView, present_rich_card


class PublicRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        self.app = PublicCatalogHandlers(PublicContext(self.db))
        self.gear_id = await self.db.add_gear("Rare item", "rare", "шлем", "🛡")
        self.recipe_id = await self.db.create_recipe("gear", self.gear_id)
        self.scroll_id = await self.db.add_resource("Learning scroll", "📜", "scroll_recipe")
        self.material_id = await self.db.add_resource("Ore", "🪨", "craft")
        await self.db.set_recipe_learning_scroll(self.recipe_id, self.scroll_id)
        await self.db.add_ingredient(self.recipe_id, self.material_id, 7)
        await self.db.claim_recipe_owner(self.recipe_id, 101, None, expected_gear_id=self.gear_id)
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.message = types.Message(
            message_id=10,
            date=1,
            chat=types.Chat(id=101, type="private"),
            text="Card",
        ).as_(self.bot)
        self.user = types.User(id=101, is_bot=False, first_name="Player")

    async def asyncTearDown(self):
        await self.app.background_tasks.close()
        await self.db.close()
        await self.bot.session.close()

    def callback(self, data, user=None):
        return types.CallbackQuery(
            id="test",
            from_user=user or self.user,
            message=self.message,
            chat_instance="test",
            data=data,
        ).as_(self.bot)

    async def test_scroll_and_gear_show_same_materials_and_stable_owner(self):
        gear = await build_gear_card(self.db, self.gear_id, link_mode=EntityLinkMode.CALLBACK)
        scroll = await build_resource_card(self.db, self.scroll_id, link_mode=EntityLinkMode.CALLBACK)
        for card in (gear, scroll):
            for text in (card.rich_html, card.fallback_html):
                self.assertIn("Изучить рецепт один раз", text)
                self.assertIn("Материалы на один крафт", text)
                self.assertIn("Ore", text)
                self.assertIn("7 шт.", text)
                self.assertIn("tg://user?id=101", text)
                self.assertNotIn("@None", text)
            self.assertNotIn("tg-button", card.fallback_html)
            self.assertIn('type="callback_data"', card.rich_html)
        self.assertIn("Rare item", scroll.fallback_html)
        self.assertNotIn("Где крафтить", scroll.fallback_html)
        self.assertNotIn("Алхимия", scroll.fallback_html)
        materials = gear.fallback_html.split("Материалы на один крафт", 1)[1]
        self.assertNotIn("Learning scroll", materials)
        self.assertNotIn("1 шт.", materials)
        self.assertIn("За один крафт: 1 шт.", gear.fallback_html)

    async def test_learning_button_uses_relation_instead_of_rarity(self):
        data = await self.db.get_gear_card(self.gear_id)
        keyboard = await self.app.build_gear_card_keyboard(data, 101, 1, 0)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn(f"recipe_relinquish_{self.recipe_id}_{self.gear_id}_rare_0_1", callbacks)
        await self.db.relinquish_recipe_owner(self.recipe_id, 101)
        await self.db.set_recipe_learning_scroll(self.recipe_id, None)
        await self.db.update_gear(self.gear_id, rarity="epic")
        keyboard = await self.app.build_gear_card_keyboard(await self.db.get_gear_card(self.gear_id), 101, 1, 0)
        self.assertFalse(
            any((b.callback_data or "").startswith("recipe_") for row in keyboard.inline_keyboard for b in row)
        )

    async def test_owner_actions_use_stable_user_id_after_rename_or_without_username(self):
        claim = f"recipe_claim_{self.recipe_id}_{self.gear_id}_rare_0_1"
        relinquish = f"recipe_relinquish_{self.recipe_id}_{self.gear_id}_rare_0_1"
        stranger = types.User(id=202, is_bot=False, first_name="Other", username="OldName")
        renamed = types.User(id=101, is_bot=False, first_name="Player", username="NewName")
        anonymous = renamed.model_copy(update={"username": None})

        with (
            patch.object(self.app, "render_gear_card", new=AsyncMock(return_value=True)),
            patch.object(types.CallbackQuery, "answer", new=AsyncMock()),
        ):
            await self.app.update_recipe_owner(self.callback(relinquish, stranger))
            self.assertEqual((await self.db.get_gear_card(self.gear_id))["owner_user_ids"], [101])
            await self.app.update_recipe_owner(self.callback(relinquish, renamed))
            self.assertEqual((await self.db.get_gear_card(self.gear_id))["owner_user_ids"], [])
            await self.app.update_recipe_owner(self.callback(claim, anonymous))

        data = await self.db.get_gear_card(self.gear_id)
        self.assertEqual(data["owner_user_ids"], [101])
        keyboard = await self.app.build_gear_card_keyboard(data, anonymous.id, 1, 0)
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn(relinquish, callbacks)
        plain = (await build_gear_card(self.db, self.gear_id, "rare", data=data)).fallback_html
        self.assertIn("tg://user?id=101", plain)
        self.assertNotIn("@None", plain)

    async def test_interactive_learning_keeps_back_navigation(self):
        history = EntityNavigationHistory()
        key = (101, 101, 10)
        source = EntityRef("resource", self.scroll_id)
        target = EntityRef("gear", self.gear_id)
        history.visit(key, source, target)
        with (
            patch.object(self.app, "entity_navigation", history),
            patch.object(
                self.app,
                "present_rich_card",
                AsyncMock(return_value=self.message),
            ) as present,
            patch.object(types.CallbackQuery, "answer", AsyncMock()),
        ):
            await self.app.update_recipe_owner(
                self.callback(f"recipe_relinquish_{self.recipe_id}_{self.gear_id}_rare_x_1")
            )
        self.assertEqual(history.previous(key), source)
        callbacks = [b.callback_data for row in present.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertIn(f"entity_back:resource:{self.scroll_id}", callbacks)
        self.assertIn(f"recipe_claim_{self.recipe_id}_{self.gear_id}_rare_x_1", callbacks)

    async def test_fresh_deep_link_can_open_main_sections_without_reply_keyboard(self):
        storage = MemoryStorage()
        state = FSMContext(storage, StorageKey(bot_id=self.bot.id, chat_id=101, user_id=101))
        start = self.message.model_copy(update={"text": f"/start gear_{self.gear_id}", "from_user": self.user})
        try:
            with (
                patch.object(self.app, "upsert_rich_card", AsyncMock(return_value=self.message)),
                patch.object(
                    self.app.analytics,
                    "log_view_gear",
                    AsyncMock(),
                ),
                patch.object(types.Message, "delete", AsyncMock()),
                patch.object(types.Message, "answer", AsyncMock()) as answer,
                patch.object(
                    types.Message,
                    "edit_text",
                    AsyncMock(return_value=self.message),
                ) as edit,
                patch.object(types.CallbackQuery, "answer", AsyncMock()),
            ):
                await self.app.router.propagate_event(
                    "message", start, bot=self.bot, state=state, raw_state=None, event_from_user=self.user
                )
                answer.assert_not_awaited()
                await self.app.router.propagate_event(
                    "callback_query",
                    self.callback("back_to_main_menu"),
                    bot=self.bot,
                    state=state,
                    raw_state=None,
                    event_from_user=self.user,
                )
                menu = edit.await_args.kwargs["reply_markup"]
                destinations = {button.callback_data for row in menu.inline_keyboard for button in row}
                self.assertEqual(
                    destinations,
                    {"main_section_mobs", "main_section_resources", "main_section_gear", "main_section_search"},
                )
                for destination in destinations:
                    await self.app.router.propagate_event(
                        "callback_query",
                        self.callback(destination),
                        bot=self.bot,
                        state=state,
                        raw_state=None,
                        event_from_user=self.user,
                    )
                    self.assertTrue(edit.await_args.kwargs["reply_markup"].inline_keyboard)
                self.assertIsNone(await state.get_state())
        finally:
            await storage.close()

    async def test_craft_location_is_explicit_escaped_and_independent_of_resource_name(self):
        data = {
            "id": 777,
            "name": "Дубленая кожа",
            "emoji": "",
            "type": "alchemy",
            "note": "",
            "mobs": [],
            "used_in": [],
            "learning_recipes": [],
            "craft_location": "",
        }
        with patch.object(
            self.db, "get_recipe_for_resource", AsyncMock(return_value={"ingredients": [], "craft_location": ""})
        ):
            card = await build_resource_card(self.db, 777, data=data)
            self.assertNotIn("Где крафтить", card.fallback_html)
            self.assertNotIn("Мередит", card.fallback_html)
            data["craft_location"] = "New <workshop> & vendor"
            first = await build_resource_card(self.db, 777, data=data)
            data["name"] = "Renamed resource"
            renamed = await build_resource_card(self.db, 777, data=data)
        for card in (first, renamed):
            self.assertIn("New &lt;workshop&gt; &amp; vendor", card.rich_html)
            self.assertIn("New &lt;workshop&gt; &amp; vendor", card.fallback_html)

    async def test_output_amounts_survive_storage_and_appear_in_related_cards(self):
        product = await self.db.add_resource("Сплав", "🧪", "alchemy")
        await self.db.update_recipe_quantity(self.recipe_id, 2)
        await self.db.save_resource_recipe(product, 5, [{"resource_id": self.material_id, "quantity": 7}])

        self.assertEqual((await self.db.get_gear_card(self.gear_id))["craft_quantity"], 2)
        self.assertEqual((await self.db.get_recipe_for_resource(product))["quantity"], 5)
        for card, text in (
            (await build_gear_card(self.db, self.gear_id), "За один крафт: 2 шт."),
            (await build_resource_card(self.db, product), "За один крафт: 5 шт."),
            (await build_resource_card(self.db, self.scroll_id), "× 2 шт."),
        ):
            self.assertIn(text, card.rich_html)
            self.assertIn(text, card.fallback_html)


class InteractiveDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.connect()
        self.app = PublicCatalogHandlers(PublicContext(self.db))
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.user = types.User(id=101, is_bot=False, first_name="Player")
        self.message = types.Message(message_id=10, date=1, chat=types.Chat(id=101, type="private"), text="A").as_(
            self.bot
        )
        self.history = EntityNavigationHistory()
        self.key = (101, 101, 10)
        self.source = EntityRef("mob", 1)
        self.current = EntityRef("resource", 2)
        self.history.visit(self.key, self.source, self.current)
        self.error = TelegramNetworkError(
            method=EditMessageText(chat_id=101, message_id=10, text="B"), message="offline"
        )

    async def asyncTearDown(self):
        await self.app.background_tasks.close()
        await self.db.close()
        await self.bot.session.close()

    def callback(self, message=True):
        return types.CallbackQuery(
            id="test",
            from_user=self.user,
            chat_instance="test",
            message=self.message if message else None,
            inline_message_id=None if message else "inline-only",
        ).as_(self.bot)

    async def test_failed_forward_or_back_keeps_previous_history(self):
        with (
            patch.object(self.app, "entity_navigation", self.history),
            patch.object(
                self.app,
                "present_interactive_entity",
                AsyncMock(side_effect=self.error),
            ),
            patch.object(types.CallbackQuery, "answer", AsyncMock()),
            patch.object(self.app, "log_interactive_entity_view", AsyncMock()) as log,
        ):
            with self.assertRaises(TelegramNetworkError):
                await self.app.navigate_related_entity(
                    self.callback(),
                    EntityNavigateCallback(entity_type="gear", entity_id=3, source_type="resource", source_id=2),
                )
            self.assertEqual(self.history.current(self.key), self.current)
            self.assertEqual(self.history.previous(self.key), self.source)
            with self.assertRaises(TelegramNetworkError):
                await self.app.navigate_related_entity_back(
                    self.callback(), EntityBackCallback(entity_type="mob", entity_id=1)
                )
            self.assertEqual(self.history.current(self.key), self.current)
            log.assert_not_awaited()

    async def test_success_transfers_history_to_replacement_anchor(self):
        sent = self.message.model_copy(update={"message_id": 11})
        with (
            patch.object(self.app, "entity_navigation", self.history),
            patch.object(
                self.app,
                "present_interactive_entity",
                AsyncMock(return_value=sent),
            ),
            patch.object(types.CallbackQuery, "answer", AsyncMock()),
            patch.object(self.app, "log_interactive_entity_view", AsyncMock()),
        ):
            await self.app.navigate_related_entity(
                self.callback(),
                EntityNavigateCallback(entity_type="gear", entity_id=3, source_type="resource", source_id=2),
            )
        self.assertIsNone(self.history.current(self.key))
        self.assertEqual(self.history.current((101, 101, 11)), EntityRef("gear", 3))
        self.assertEqual(self.history.previous((101, 101, 11)), self.current)

    async def test_inline_callback_without_chat_never_sends_replacement(self):
        with (
            patch.object(self.app, "present_interactive_entity", AsyncMock()) as present,
            patch.object(types.CallbackQuery, "answer", AsyncMock()),
        ):
            await self.app.navigate_related_entity(
                self.callback(False),
                EntityNavigateCallback(entity_type="gear", entity_id=3, source_type="resource", source_id=2),
            )
            await self.app.navigate_related_entity_back(
                self.callback(False), EntityBackCallback(entity_type="mob", entity_id=1)
            )
        present.assert_not_awaited()

    async def test_composed_card_uses_shared_safe_delivery(self):
        card = CardView("<b>Card</b>", "Card")
        with patch("ui.rich.upsert_rich_card", AsyncMock(return_value=self.message)) as upsert:
            self.assertIs(
                await present_rich_card(bot=self.bot, chat_id=101, card=card, current_message=self.message),
                self.message,
            )
        self.assertEqual(upsert.await_args.kwargs["plain_text"], "Card")
        self.assertIs(upsert.await_args.kwargs["current_message"], self.message)


class ComponentParserTests(unittest.TestCase):
    def test_components_reject_values_outside_sqlite_range(self):
        overflow = MAX_SQLITE_ID + 1
        for parser, payload in (
            (MobViewCallback.parse, f"view_mobs_{overflow}_1_1"),
            (ResourceViewCallback.parse, f"view_resource_{overflow}_scroll_recipe_1"),
            (GearViewCallback.parse, f"view_gear_{overflow}_epic_0_1"),
            (CardViewCallback.parse, f"view_card_1_{overflow}"),
            (RecipeOwnerCallback.parse, f"recipe_claim_{overflow}_1_epic_x_1"),
        ):
            self.assertIsNone(parser(payload))
        self.assertFalse(EntityRef("gear", overflow).is_valid)

    def test_return_context_preserves_gear_slot_and_card(self):
        self.assertEqual(parse_return_param("gear_21_epic_4_3")["slot_index"], 4)
        self.assertEqual(parse_return_param("card_9_2")["kind"], "card")
