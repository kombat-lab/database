import os
import unittest

os.environ.setdefault("BOT_TOKEN", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")

from bot import (  # noqa: E402
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
