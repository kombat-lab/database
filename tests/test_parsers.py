import os
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

from bot import (  # noqa: E402
    DEFAULT_ALCHEMY_CRAFT_LOCATION,
    MEREDITH_ALCHEMY_CRAFT_LOCATION,
    format_gear_card_plain,
    format_gear_card_rich,
    get_alchemy_craft_location,
    parse_resource_page_callback,
    parse_resource_view_callback,
    parse_return_param,
)


class CallbackParserTests(unittest.TestCase):
    def test_resource_type_with_underscore(self):
        self.assertEqual(
            parse_resource_page_callback("res_page_scroll_recipe_3", "res_page_"),
            ("scroll_recipe", 3),
        )
        self.assertEqual(
            parse_resource_view_callback("view_resource_42_scroll_recipe_7"),
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
