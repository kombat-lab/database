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
from admin_handlers import (
    ENTITY_CONFIGS,
    build_mob_edit_keyboard,
    get_drop_filter_options,
    get_location_choice_keyboard,
    resolve_drop_filter,
)
from bot import (
    DEFAULT_ALCHEMY_CRAFT_LOCATION,
    MEREDITH_ALCHEMY_CRAFT_LOCATION,
    build_gear_card_keyboard,
    build_recipe_owner_callback,
    format_gear_card_plain,
    format_gear_card_rich,
    format_resource_card,
    format_resource_card_rich,
    get_alchemy_craft_location,
    get_location_list_title,
    parse_resource_page_callback,
    parse_resource_view_callback,
    parse_gear_view_callback,
    parse_recipe_owner_callback,
    parse_return_param,
    replace_rich_card,
)
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


class CallbackParserTests(unittest.TestCase):
    def test_gear_callbacks_preserve_slot_context(self):
        self.assertEqual(
            parse_gear_view_callback("nav_gear_47_epic_2_3"),
            (47, "epic", 2, 3),
        )
        self.assertEqual(
            parse_gear_view_callback("view_gear_47_epic_3"),
            (47, "epic", None, 3),
        )

        callback_data = build_recipe_owner_callback(
            "claim", 36, 47, "epic", 3, 2
        )
        self.assertEqual(callback_data, "recipe_claim_36_47_epic_2_3")
        self.assertEqual(
            parse_recipe_owner_callback(callback_data),
            ("claim", 36, 47, "epic", 2, 3),
        )
        self.assertEqual(
            parse_recipe_owner_callback("recipe_claim_36_47_epic_3"),
            ("claim", 36, 47, "epic", None, 3),
        )

    def test_resource_type_with_underscore(self):
        self.assertEqual(
            parse_resource_page_callback("res_page_scroll_recipe_3", "res_page_"),
            ("scroll_recipe", 3),
        )
        self.assertEqual(
            parse_resource_view_callback("view_resource_42_scroll_recipe_7"),
            (42, "scroll_recipe", 7),
        )
        self.assertEqual(
            parse_resource_view_callback("nav_resource_42_scroll_recipe_7"),
            (42, "scroll_recipe", 7),
        )

    def test_return_contexts(self):
        self.assertEqual(
            parse_return_param("resource_type_42_scroll_recipe_7"),
            {
                "kind": "resource_type",
                "item_id": 42,
                "page": 7,
                "context_type": "type",
                "context_id": "scroll_recipe",
            },
        )
        self.assertEqual(
            parse_return_param("resource_loc_11_4_2"),
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
                self.assertIsNone(parse_return_param(value))

        self.assertIsNone(parse_resource_page_callback("res_page_scroll_recipe_x", "res_page_"))
        self.assertIsNone(parse_resource_view_callback("view_resource_0_craft_1"))

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
    async def test_navigation_deletes_old_card_before_sending_new_one(self):
        events = []
        sent_message = object()
        bot = AsyncMock()
        current_message = AsyncMock()

        async def delete_old():
            events.append("delete")

        async def send_new(**kwargs):
            events.append("send")
            return sent_message

        current_message.delete.side_effect = delete_old
        bot.send_rich_message.side_effect = send_new

        result = await replace_rich_card(
            bot=bot,
            chat_id=123,
            rich_message=object(),
            plain_text="Карточка",
            reply_markup=object(),
            current_message=current_message,
        )

        self.assertIs(result, sent_message)
        self.assertEqual(events, ["delete", "send"])
        bot.edit_message_text.assert_not_awaited()


class OptionalNoteTests(unittest.TestCase):
    def test_skip_keyboard_uses_shared_callback(self):
        keyboard = build_optional_note_keyboard()
        button = keyboard.inline_keyboard[0][0]

        self.assertEqual(button.text, "⏭ Без примечания")
        self.assertEqual(button.callback_data, OPTIONAL_NOTE_SKIP_CALLBACK)

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

        with patch("bot.db.get_gear_card", new=AsyncMock(return_value=gear_data)):
            plain = await format_gear_card_plain(47, "epic", 1)
            rich = await format_gear_card_rich(47, "epic", 1)

        for card in (plain, rich.html):
            with self.subTest(card_type=type(card).__name__):
                self.assertIn("📜 Свиток падает с мобов:", card)
                self.assertIn("Муха-охотник", card)
                self.assertIn("⚔️ Выпадает с мобов:", card)
                self.assertIn("Страж", card)
                self.assertIn("Рецепт пока не заполнен.", card)

        self.assertIn(RICH_TABLE_OPEN, rich.html)


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
            patch("bot.db.get_resource_card", new=AsyncMock(return_value=resource_data)),
            patch("bot.db.get_recipe_for_resource", new=AsyncMock(return_value=None)),
        ):
            plain = await format_resource_card(115, "type", "craft", 1)
            rich = await format_resource_card_rich(115, "type", "craft", 1)

        for card in (plain, rich.html):
            with self.subTest(card_type=type(card).__name__):
                self.assertIn("🧩 Используется в рецептах:", card)
                self.assertIn("Кожаный шлем", card)
                self.assertIn("Дублёная кожа", card)
                self.assertIn("🟢 🪖", card)
                self.assertNotIn("⚔️", card)
                self.assertNotIn("⚗️ ⚗️", card)
                self.assertLess(card.index("Дублёная кожа"), card.index("Кожаный шлем"))

        self.assertIn("— 5 шт.", plain)
        self.assertIn("— 3 шт.", plain)
        self.assertIn("<tg-spoiler>", plain)
        self.assertIn("</tg-spoiler>", plain)
        self.assertNotIn("<table", plain)
        self.assertIn("<details>", rich.html)
        self.assertIn("<summary>🧩 Используется в рецептах:</summary>", rich.html)
        self.assertIn("</details>", rich.html)
        self.assertIn("<th>Результат</th><th>Нужно</th>", rich.html)
        self.assertIn("<td>5 шт.</td>", rich.html)
        self.assertIn("<td>3 шт.</td>", rich.html)
        self.assertIn(RICH_TABLE_OPEN, rich.html)
        self.assertNotIn("cellpadding=", rich.html)
        self.assertNotIn("tg-button", rich.html)
