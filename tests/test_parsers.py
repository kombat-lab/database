import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

from aiogram.types import Update

from admin_utils import (
    OPTIONAL_NOTE_SKIP_CALLBACK,
    build_edit_menu,
    build_optional_note_keyboard,
    normalize_optional_note,
)
from admin_handlers import ENTITY_CONFIGS
from admin_mobs import (
    build_mob_edit_keyboard,
    get_drop_filter_options,
    get_location_choice_keyboard,
    resolve_drop_filter,
)
from database import db
from bot import (
    build_gear_card_keyboard,
    get_location_list_title,
)
from ui.callbacks import (
    EntityBackCallback,
    EntityNavigateCallback,
    GearViewCallback,
    RecipeOwnerCallback,
    ResourceViewCallback,
    parse_resource_page,
    parse_return_context,
)
from ui.cards import (
    DEFAULT_ALCHEMY_CRAFT_LOCATION,
    MEREDITH_ALCHEMY_CRAFT_LOCATION,
    build_gear_card,
    build_mob_card,
    build_resource_card,
    get_alchemy_craft_location,
)
from ui.links import EntityLinkMode
from ui.navigation import EntityNavigationHistory, EntityRef
from ui.rich import CardView, SECTION_DIVIDER, present_rich_card
from utils import RICH_TABLE_OPEN


class BotApiCompatibilityTests(unittest.TestCase):
    def test_bot_api_10_3_rich_button_update_is_deserialized(self):
        update = Update.model_validate({
            "update_id": 1001,
            "callback_query": {
                "id": "callback-1",
                "from": {
                    "id": 123456789,
                    "is_bot": False,
                    "first_name": "Test User",
                },
                "message": {
                    "message_id": 42,
                    "date": 1787600000,
                    "chat": {
                        "id": 123456789,
                        "type": "private",
                        "first_name": "Test User",
                    },
                    "rich_message": {
                        "blocks": [{
                            "type": "buttons",
                            "buttons": [{
                                "text": "📋 Скопировать название",
                                "style": "primary",
                                "copy_text": {"text": "Клочок меха"},
                            }],
                            "align": "center",
                        }],
                    },
                },
                "chat_instance": "chat-instance-1",
                "data": "nav_resource_115_craft_2",
            },
        })

        button_block = update.callback_query.message.rich_message.blocks[0]
        self.assertEqual(button_block.type, "buttons")
        self.assertEqual(button_block.buttons[0].copy_text.text, "Клочок меха")


class EntityNavigationTests(unittest.TestCase):
    def test_callback_contains_target_and_source(self):
        callback = EntityNavigateCallback(
            entity_type="mob",
            entity_id=5,
            source_type="resource",
            source_id=115,
        )
        packed = callback.pack()
        self.assertEqual(packed, "entity:mob:5:resource:115")
        self.assertEqual(EntityNavigateCallback.unpack(packed), callback)
        self.assertEqual(
            EntityBackCallback(entity_type="resource", entity_id=115).pack(),
            "entity_back:resource:115",
        )

    def test_history_supports_multiple_steps_and_message_replacement(self):
        history = EntityNavigationHistory(max_sessions=4, max_depth=4)
        old_key = (1, 10, 100)
        new_key = (1, 10, 101)
        resource = EntityRef("resource", 115)
        mob = EntityRef("mob", 5)
        gear = EntityRef("gear", 30)

        history.visit(old_key, resource, mob, root_state="root keyboard")
        history.visit(old_key, mob, gear)
        self.assertEqual(history.previous(old_key), mob)
        self.assertEqual(history.back(old_key), mob)
        self.assertEqual(history.previous(old_key), resource)

        history.transfer(old_key, new_key)
        self.assertIsNone(history.previous(old_key))
        self.assertEqual(history.previous(new_key), resource)
        self.assertEqual(history.root_state(new_key), "root keyboard")

    def test_stale_source_resets_history(self):
        history = EntityNavigationHistory()
        key = (1, 10, 100)
        resource = EntityRef("resource", 115)
        mob = EntityRef("mob", 5)
        card = EntityRef("card", 7)

        history.visit(key, resource, mob)
        history.visit(key, card, resource)
        self.assertEqual(history.previous(key), card)


class CallbackParserTests(unittest.TestCase):
    def test_gear_callbacks_preserve_slot_context(self):
        self.assertEqual(
            GearViewCallback.parse("nav_gear_47_epic_2_3"),
            GearViewCallback(47, "epic", 2, 3),
        )
        self.assertEqual(
            GearViewCallback.parse("view_gear_47_epic_3"),
            GearViewCallback(47, "epic", None, 3),
        )

        callback_data = RecipeOwnerCallback(
            "claim", 36, 47, "epic", 2, 3
        ).pack()
        self.assertEqual(callback_data, "recipe_claim_36_47_epic_2_3")
        self.assertEqual(
            RecipeOwnerCallback.parse(callback_data),
            RecipeOwnerCallback("claim", 36, 47, "epic", 2, 3),
        )
        self.assertEqual(
            RecipeOwnerCallback.parse("recipe_claim_36_47_epic_3"),
            RecipeOwnerCallback("claim", 36, 47, "epic", None, 3),
        )

    def test_resource_type_with_underscore(self):
        self.assertEqual(
            parse_resource_page("res_page_scroll_recipe_3", "res_page_"),
            ("scroll_recipe", 3),
        )
        self.assertEqual(
            ResourceViewCallback.parse("view_resource_42_scroll_recipe_7"),
            ResourceViewCallback(42, "scroll_recipe", 7),
        )
        self.assertEqual(
            ResourceViewCallback.parse("nav_resource_42_scroll_recipe_7"),
            ResourceViewCallback(42, "scroll_recipe", 7),
        )

    def test_return_contexts(self):
        self.assertEqual(
            parse_return_context("resource_type_42_scroll_recipe_7"),
            {
                "kind": "resource_type",
                "item_id": 42,
                "page": 7,
                "context_type": "type",
                "context_id": "scroll_recipe",
            },
        )
        self.assertEqual(
            parse_return_context("resource_loc_11_4_2"),
            {
                "kind": "resource_loc",
                "item_id": 11,
                "page": 2,
                "context_type": "location",
                "context_id": 4,
            },
        )

    def test_invalid_values_are_rejected(self):
        invalid_values = (
            None,
            "",
            "mob_x_1_1",
            "mob_1_1_0",
            "resource_type_1_unknown_1",
            "resource_loc_1_-2_1",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                self.assertIsNone(parse_return_context(value))

        self.assertIsNone(parse_resource_page("res_page_scroll_recipe_x", "res_page_"))
        self.assertIsNone(ResourceViewCallback.parse("view_resource_0_craft_1"))

    def test_location_list_titles_are_in_russian(self):
        location = {"id": 5, "name": "Поляна", "emoji": "🏕"}

        self.assertEqual(
            get_location_list_title(location, "mobs", 1),
            "🏕 Поляна - Мобы\nСтраница 1",
        )
        self.assertEqual(
            get_location_list_title(location, "resources", 2),
            "🏕 Поляна - Ресурсы\nСтраница 2",
        )


class RichCardNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_edits_existing_card_in_place(self):
        sent_message = object()
        bot = AsyncMock()
        current_message = AsyncMock()
        current_message.message_id = 77
        bot.edit_message_text.return_value = sent_message

        result = await present_rich_card(
            bot=bot,
            chat_id=123,
            card=CardView("Карточка", "Карточка"),
            reply_markup=object(),
            current_message=current_message,
        )

        self.assertIs(result, sent_message)
        bot.edit_message_text.assert_awaited_once()
        bot.send_rich_message.assert_not_awaited()
        current_message.delete.assert_not_awaited()


class OptionalNoteTests(unittest.TestCase):
    def test_skip_keyboard_uses_shared_callback(self):
        keyboard = build_optional_note_keyboard()
        button = keyboard.inline_keyboard[0][0]

        self.assertEqual(button.text, "⏭ Без примечания")
        self.assertEqual(button.callback_data, OPTIONAL_NOTE_SKIP_CALLBACK)
        self.assertTrue(keyboard.force_reply)

    def test_legacy_dash_and_whitespace_are_normalized(self):
        self.assertEqual(normalize_optional_note(" - "), "")
        self.assertEqual(normalize_optional_note("  заметка  "), "заметка")


class AdminLocationSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_location_buttons_show_names_in_alphabetical_order(self):
        locations = [
            {"id": 2, "name": "Ядовитая топь", "emoji": "🧪"},
            {"id": 1, "name": "Алькасар", "emoji": "🏛"},
        ]
        with patch(
            "admin_handlers.db.get_locations",
            new=AsyncMock(return_value=locations),
        ):
            keyboard = await get_location_choice_keyboard("mob_add_location_")

        buttons = [row[0] for row in keyboard.inline_keyboard]
        self.assertEqual([button.text for button in buttons], ["🏛 Алькасар", "🧪 Ядовитая топь"])
        self.assertEqual(
            [button.callback_data for button in buttons],
            ["mob_add_location_1", "mob_add_location_2"],
        )

    async def test_mob_edit_menu_displays_location_name_instead_of_id(self):
        keyboard = build_mob_edit_keyboard({
            "id": 10,
            "name": "Страж",
            "emoji": "🛡",
            "hp": 100,
            "dust_min": 1,
            "dust_max": 2,
            "exp": 3,
            "location_id": 17,
            "location_name": "Алькасар",
            "location_emoji": "🏛",
        })

        labels = [row[0].text for row in keyboard.inline_keyboard]
        self.assertIn("Локация: 🏛 Алькасар", labels)
        self.assertNotIn("ID локации: 17", labels)


class AdminDropFilterTests(unittest.TestCase):
    def test_filter_value_and_label_come_from_one_configuration(self):
        gear_options = get_drop_filter_options("gear")

        self.assertEqual(resolve_drop_filter("gear", 0), gear_options[0][0])
        self.assertTrue(gear_options[0][1])

    def test_unknown_drop_category_is_rejected(self):
        with self.assertRaises(ValueError):
            get_drop_filter_options("unknown")


class GearAdminPresentationTests(unittest.TestCase):
    def test_edit_menu_uses_shared_labels_and_class_formatter(self):
        fallback, rich, _ = build_edit_menu(
            47,
            ENTITY_CONFIGS["gear"],
            {
                "name": "Тлеющий шлем",
                "rarity": "epic",
                "slot": "шлем",
                "level": 1,
                "classes": "",
                "note": "",
                "emoji": "🔥🪖",
            },
        )

        for text in (fallback, rich):
            self.assertIn("🔵 Сверхредкое", text)
            self.assertIn("🪖 Шлем", text)
            self.assertIn("Все классы", text)

        self.assertIn(RICH_TABLE_OPEN, rich)


class AlchemyCraftLocationTests(unittest.TestCase):
    def test_meredith_resources_use_trading_outpost(self):
        resource_names = (
            "Дубленая кожа",
            "Костяной куб",
            "Пепельный материал",
            "Прочная бечевка",
            "Субстанция",
            "Ядро земель",
        )

        for resource_name in resource_names:
            with self.subTest(resource_name=resource_name):
                self.assertEqual(
                    get_alchemy_craft_location(resource_name),
                    MEREDITH_ALCHEMY_CRAFT_LOCATION,
                )

    def test_other_resources_use_alcazar(self):
        self.assertEqual(
            get_alchemy_craft_location("Укрепляющий состав"),
            DEFAULT_ALCHEMY_CRAFT_LOCATION,
        )
        self.assertIn("Алькасар", DEFAULT_ALCHEMY_CRAFT_LOCATION)

    def test_matching_ignores_case_and_outer_spaces(self):
        self.assertEqual(
            get_alchemy_craft_location("  субстанция  "),
            MEREDITH_ALCHEMY_CRAFT_LOCATION,
        )


class MobCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_drop_sections_have_dividers_only_between_nonempty_blocks(self):
        mob = {
            "id": 24,
            "name": "Бабочка-туманница",
            "emoji": "💎🦋",
            "hp": 100,
            "exp": 20,
            "dust_min": 1,
            "dust_max": 3,
            "loc_emoji": "🌫",
            "loc_name": "Туманный лес",
            "resource_drops": [{
                "id": 1,
                "name": "Блестящая пыльца",
                "emoji": "✨",
            }],
            "gear_drops": [{
                "id": 2,
                "name": "Книга теней",
                "emoji": "📖",
                "rarity": "uncommon",
            }],
            "card_drops": [{
                "id": 3,
                "name": "Карта Бабочки-туманницы",
                "emoji": "💎🦋",
                "slot": "gloves",
            }],
        }
        with patch(
            "database.db.get_mob_full_card",
            new=AsyncMock(return_value=mob),
        ):
            card = await build_mob_card(db, 24)

        rendered = card.rich_html
        self.assertEqual(rendered.count(SECTION_DIVIDER), 2)
        self.assertLess(rendered.index("📦 Падает:"), rendered.index(SECTION_DIVIDER))
        self.assertLess(rendered.index(SECTION_DIVIDER), rendered.index("⚔️ Снаряжение:"))
        self.assertLess(rendered.index("⚔️ Снаряжение:"), rendered.rindex(SECTION_DIVIDER))
        self.assertLess(rendered.rindex(SECTION_DIVIDER), rendered.index("🃏 Карты:"))

        self.assertNotIn(SECTION_DIVIDER, card.fallback_html)
        for rendered in (card.rich_html, card.fallback_html):
            self.assertLess(rendered.index("📦 Падает:"), rendered.index("⚔️ Снаряжение:"))
            self.assertLess(rendered.index("⚔️ Снаряжение:"), rendered.index("🃏 Карты:"))

        mob["gear_drops"] = []
        mob["card_drops"] = []
        with patch(
            "database.db.get_mob_full_card",
            new=AsyncMock(return_value=mob),
        ):
            single_section = await build_mob_card(db, 24)
        self.assertNotIn(SECTION_DIVIDER, single_section.rich_html)
        self.assertNotIn(SECTION_DIVIDER, single_section.fallback_html)


class GearCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyboard_uses_one_navigation_context_after_owner_update(self):
        gear_data = {
            "id": 47,
            "rarity": "epic",
            "slot": "шлем",
            "recipe_id": 36,
            "owners": ["tester"],
        }
        with patch(
            "bot.db.get_prev_next_gear",
            new=AsyncMock(return_value={"prev_id": 46, "next_id": 48}),
        ) as neighbours:
            keyboard = await build_gear_card_keyboard(
                gear_data,
                "tester",
                page=3,
                slot_index=0,
            )

        neighbours.assert_awaited_once_with(47, "epic", "шлем")
        callbacks = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]
        self.assertIn("nav_gear_46_epic_0_3", callbacks)
        self.assertIn("nav_gear_48_epic_0_3", callbacks)
        self.assertIn("recipe_relinquish_36_47_epic_0_3", callbacks)
        self.assertIn("page_gear_epic_0_3", callbacks)

    async def test_scroll_and_direct_drop_sources_are_rendered_separately(self):
        gear_data = {
            "id": 47,
            "name": "Тлеющий шлем",
            "rarity": "epic",
            "slot": "шлем",
            "emoji": "🔥🪖",
            "level": 1,
            "classes": "",
            "note": "",
            "recipe_id": 36,
            "craftable": True,
            "ingredients": [],
            "owners": [],
            "scroll_mobs": [{"id": 43, "name": "Муха-охотник", "emoji": "🪰"}],
            "mobs": [{"id": 44, "name": "Страж", "emoji": "🛡️"}],
        }

        with patch("database.db.get_gear_card", new=AsyncMock(return_value=gear_data)):
            card = await build_gear_card(db, 47, "epic", 1)

        for rendered in (card.fallback_html, card.rich_html):
            with self.subTest(card_type=type(rendered).__name__):
                self.assertIn("📜 Свиток падает с мобов:", rendered)
                self.assertIn("Муха-охотник", rendered)
                self.assertIn("⚔️ Выпадает с мобов:", rendered)
                self.assertIn("Страж", rendered)
                self.assertIn("Рецепт пока не заполнен.", rendered)

        self.assertIn(RICH_TABLE_OPEN, card.rich_html)


class ResourceCardTests(unittest.IsolatedAsyncioTestCase):
    async def test_usage_recipes_are_rendered_in_plain_and_rich_cards(self):
        resource_data = {
            "id": 115,
            "name": "Кожаный лоскут",
            "emoji": "🪹",
            "type": "craft",
            "note": "",
            "mobs": [],
            "used_in": [
                {
                    "recipe_id": 10,
                    "result_type": "gear",
                    "result_id": 20,
                    "result_name": "Кожаный шлем",
                    "result_emoji": "🪖",
                    "result_rarity": "rare",
                    "quantity": 5,
                },
                {
                    "recipe_id": 11,
                    "result_type": "resource",
                    "result_id": 30,
                    "result_name": "Дублёная кожа",
                    "result_emoji": "⚗️🧶",
                    "result_rarity": None,
                    "quantity": 3,
                },
            ],
        }

        with (
            patch("database.db.get_resource_card", new=AsyncMock(return_value=resource_data)),
            patch("database.db.get_recipe_for_resource", new=AsyncMock(return_value=None)),
        ):
            card = await build_resource_card(db, 115, "type", "craft", 1)

        for rendered in (card.fallback_html, card.rich_html):
            with self.subTest(card_type=type(rendered).__name__):
                self.assertIn("🧩 Используется в рецептах:", rendered)
                self.assertIn("Кожаный шлем", rendered)
                self.assertIn("Дублёная кожа", rendered)
                self.assertIn("🟢 🪖", rendered)
                self.assertNotIn("⚔️", rendered)
                self.assertNotIn("⚗️ ⚗️", rendered)
                self.assertLess(rendered.index("Дублёная кожа"), rendered.index("Кожаный шлем"))

        self.assertIn("— 5 шт.", card.fallback_html)
        self.assertIn("— 3 шт.", card.fallback_html)
        self.assertIn("<tg-spoiler>", card.fallback_html)
        self.assertIn("</tg-spoiler>", card.fallback_html)
        self.assertNotIn("<table", card.fallback_html)
        self.assertIn("<details>", card.rich_html)
        self.assertIn("<summary>🧩 Используется в рецептах:</summary>", card.rich_html)
        self.assertIn("</details>", card.rich_html)
        self.assertIn("<th>Результат</th><th>Нужно</th>", card.rich_html)
        self.assertIn("<td>5 шт.</td>", card.rich_html)
        self.assertIn("<td>3 шт.</td>", card.rich_html)
        self.assertIn(RICH_TABLE_OPEN, card.rich_html)
        self.assertNotIn("cellpadding=", card.rich_html)
        self.assertNotIn("tg-button", card.rich_html)

    async def test_callback_link_mode_keeps_deep_link_fallback(self):
        resource_data = {
            "id": 115,
            "name": "Кожаный лоскут",
            "emoji": "🪹",
            "type": "craft",
            "note": "",
            "mobs": [{
                "id": 5,
                "name": "Волк",
                "emoji": "🐺",
                "location_id": 1,
                "location_name": "Лес",
                "location_emoji": "🌲",
            }],
            "used_in": [],
        }
        with (
            patch("database.db.get_resource_card", new=AsyncMock(return_value=resource_data)),
            patch("database.db.get_recipe_for_resource", new=AsyncMock(return_value=None)),
        ):
            card = await build_resource_card(
                db,
                115,
                bot_username="fog_database_bot",
                link_mode=EntityLinkMode.CALLBACK,
            )

        self.assertIn('style="link"', card.rich_html)
        self.assertIn(
            'data="entity:mob:5:resource:115"',
            card.rich_html,
        )
        self.assertNotIn("tg-button", card.fallback_html)
        self.assertIn("https://t.me/fog_database_bot?start=mob_5", card.fallback_html)
