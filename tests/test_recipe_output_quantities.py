import unittest
from database import Database
from ui.cards import build_gear_card, build_resource_card


class RecipeOutputQuantityTests(unittest.IsolatedAsyncioTestCase):
    async def test_amounts_survive_storage_and_appear_in_all_related_cards(self):
        db = Database(':memory:')
        await db.connect()
        try:
            material = await db.add_resource('Руда', '🪨')
            scroll = await db.add_resource('Рецепт клинка', '📜', 'scroll_recipe')
            gear = await db.add_gear('Клинок', 'rare', 'основная рука', '🗡️')
            recipe = await db.create_recipe('gear', gear, 2)
            await db.add_ingredient(recipe, material, 3)
            await db.set_recipe_learning_scroll(recipe, scroll)
            product = await db.add_resource('Сплав', '🧪', 'alchemy')
            await db.save_resource_recipe(product, 5, [{'resource_id': material, 'quantity': 7}])
            self.assertEqual((await db.get_gear_card(gear))['craft_quantity'], 2)
            self.assertEqual((await db.get_recipe_for_resource(product))['quantity'], 5)
            for card, text in ((await build_gear_card(db, gear), 'За один крафт: 2 шт.'),
                               (await build_resource_card(db, product), 'За один крафт: 5 шт.'),
                               (await build_resource_card(db, scroll), '× 2 шт.')):
                self.assertIn(text, card.rich_html)
                self.assertIn(text, card.fallback_html)
        finally:
            await db.close()
