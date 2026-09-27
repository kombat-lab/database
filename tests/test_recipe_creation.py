import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import EditMessageText, SendMessage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

import admin_handlers as admin
import admin_recipe_create as creation
import admin_recipes as recipes
import ui.rich
from database import Database
from fsm_storage import SQLiteFSMStorage


class RecipeCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(':memory:')
        await self.db.connect()
        self.bot = Bot('123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi')
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=self.bot.id, chat_id=1, user_id=1))
        self.user = types.User(id=1, is_bot=False, first_name='Admin')
        self.message = types.Message(message_id=10, date=1, chat=types.Chat(id=1, type='private'),
                                     from_user=self.user, text='editor').as_(self.bot)
        self.stack = ExitStack()
        for module in (creation, recipes):
            self.stack.enter_context(patch.object(module, 'db', self.db))
        self.stack.enter_context(patch.object(admin, 'ADMIN_IDS', [1]))
        self.stack.enter_context(patch.object(types.Message, 'answer', new=AsyncMock(return_value=self.message)))
        self.stack.enter_context(patch.object(types.Message, 'edit_text', new=AsyncMock(return_value=self.message)))
        self.stack.enter_context(patch.object(types.CallbackQuery, 'answer', new=AsyncMock()))
        self.stack.enter_context(patch.object(ui.rich, 'present_rich_card', new=AsyncMock(return_value=self.message)))

    async def asyncTearDown(self):
        self.stack.close()
        await self.storage.close()
        await self.db.close()
        await self.bot.session.close()

    def callback(self, data, message_id=10):
        return types.CallbackQuery(id='test', from_user=self.user, chat_instance='test', data=data,
                                   message=self.message.model_copy(update={'message_id':message_id}).as_(self.bot)).as_(self.bot)

    async def route(self, event):
        kind = 'callback_query' if isinstance(event, types.CallbackQuery) else 'message'
        return await admin.admin_router.propagate_event(kind, event, bot=self.bot, state=self.state,
                                                        raw_state=await self.state.get_state(), event_from_user=self.user)

    async def signed(self, data):
        token = (await self.state.get_data())['admin_screen']['token']
        return f'{data}~{token}'

    async def action(self, data):
        await self.route(self.callback(await self.signed(data)))

    async def text(self, text):
        await self.route(self.message.model_copy(update={'message_id':90,'text':text}).as_(self.bot))

    async def start(self):
        await self.route(self.callback('admin_manage_recipes'))
        await self.action('recipe_type_resource')
        await self.action('recipe_add_resource')

    async def test_complete_alchemy_recipe_stays_unpublished_until_explicit_save(self):
        result_id = await self.db.add_resource('Эликсир', '🧪', 'alchemy')
        ingredient_id = await self.db.add_resource('Трава', '🌿', 'craft')
        await self.start()
        await self.action(f'rc:select:{result_id}')
        self.assertEqual(await self.db.execute_query('SELECT * FROM recipes'), [])
        await self.text('3')
        await self.action(f'rc:select:{ingredient_id}')
        await self.text('5')
        self.assertEqual(await self.db.execute_query('SELECT * FROM recipes'), [])
        await self.action('rc:location')
        await self.text('Деревенская лаборатория')
        old_save = self.callback(await self.signed('rc:save'))
        await self.route(old_save)
        await self.route(old_save)
        recipe_rows = await self.db.execute_query('SELECT * FROM recipes')
        self.assertEqual(len(recipe_rows), 1)
        recipe = await self.db.get_recipe_details(recipe_rows[0]['id'])
        self.assertEqual(recipe['quantity'], 3)
        self.assertEqual(recipe['craft_location'], 'Деревенская лаборатория')
        self.assertEqual([(row['resource_id'],row['quantity']) for row in recipe['ingredients']], [(ingredient_id,5)])

    async def test_result_picker_filters_scrolls_and_currencies_and_has_search_pages(self):
        for index in range(19):
            await self.db.add_resource(f'Эликсир {index:02}', '🧪', 'alchemy')
        scroll_id = await self.db.add_resource('Рецепт снаряжения', '📜', 'scroll_recipe')
        currency_id = await self.db.add_resource('Монеты', '💰', 'currency')
        await self.start()
        keyboard = types.Message.edit_text.await_args.kwargs['reply_markup']
        selections = [button for row in keyboard.inline_keyboard for button in row if (button.callback_data or '').startswith('rc:select:')]
        self.assertEqual(len(selections), 8)
        self.assertFalse(any(button.callback_data.split('~')[0] in (f'rc:select:{scroll_id}',f'rc:select:{currency_id}') for button in selections))
        await self.action('rc:search')
        await self.text('Эликсир 18')
        keyboard = types.Message.answer.await_args.kwargs['reply_markup']
        selections = [button.text for row in keyboard.inline_keyboard for button in row if (button.callback_data or '').startswith('rc:select:')]
        self.assertEqual(selections, ['🧪 Эликсир 18'])

    async def test_self_ingredient_and_old_message_are_rejected(self):
        result_id = await self.db.add_resource('Эликсир', '🧪', 'alchemy')
        material_id = await self.db.add_resource('Трава', '🌿', 'craft')
        await self.start()
        await self.action(f'rc:select:{result_id}')
        await self.text('1')
        old = self.callback(await self.signed(f'rc:select:{material_id}'),message_id=99)
        await self.route(old)
        self.assertEqual(await self.state.get_state(),creation.RecipeCreationStates.choosing.state)
        await self.action(f'rc:select:{result_id}')
        self.assertEqual(await self.state.get_state(),creation.RecipeCreationStates.choosing.state)
        self.assertEqual((await self.state.get_data())['recipe_create_materials'],[])

    async def test_cancel_after_materials_does_not_publish(self):
        result_id = await self.db.add_resource('Эликсир', '🧪', 'alchemy')
        material_id = await self.db.add_resource('Трава', '🌿', 'craft')
        await self.start()
        await self.action(f'rc:select:{result_id}')
        await self.text('1')
        await self.action(f'rc:select:{material_id}')
        await self.text('2')
        await self.text('/kombat')
        self.assertIsNone(await self.state.get_state())
        self.assertEqual(await self.db.execute_query('SELECT * FROM recipes'),[])

    async def test_search_overflow_does_not_change_query_or_screen(self):
        await self.start()
        await self.action('rc:search')
        before = await self.state.get_data()
        await self.text('я' * 4096)
        self.assertEqual((await self.state.get_data())['recipe_create_query'], before['recipe_create_query'])
        self.assertEqual(await self.state.get_state(),creation.RecipeCreationStates.search.state)

    async def use_durable_state(self):
        await self.storage.close()
        self.storage = SQLiteFSMStorage(self.db)
        self.state = FSMContext(self.storage, self.state.key)

    async def compose_preview(self):
        result_id = await self.db.add_resource('Эликсир', '🧪', 'alchemy')
        first = await self.db.add_resource('Трава', '🌿', 'craft')
        second = await self.db.add_resource('Вода', '💧', 'craft')
        await self.start()
        await self.action(f'rc:select:{result_id}')
        await self.text('1')
        for material_id in (first, second):
            await self.action('rc:page:0')
            await self.action(f'rc:select:{material_id}')
            await self.text('2')
        return first, second

    async def test_material_removal_retry_never_removes_a_different_resource(self):
        await self.use_durable_state()
        first, second = await self.compose_preview()
        before = await self.state.get_data()
        old_remove = self.callback(await self.signed(f'rc:remove_id:{first}'))

        async def failed_delivery(*args, **kwargs):
            self.assertEqual(self.db._transaction_depth, 0)
            raise TelegramNetworkError(EditMessageText(chat_id=1, message_id=10, text='preview'), 'offline')

        with patch.object(types.Message, 'edit_text', new=AsyncMock(side_effect=failed_delivery)):
            with self.assertRaises(TelegramNetworkError):
                await self.route(old_remove)
        after_failure = await self.state.get_data()
        self.assertEqual(after_failure['recipe_create_materials'], before['recipe_create_materials'])
        self.assertEqual(after_failure['admin_screen'], before['admin_screen'])
        self.assertIn('admin_pending_screen', after_failure)
        await self.route(old_remove)
        self.assertEqual((await self.state.get_data())['recipe_create_materials'], [{'resource_id': second, 'quantity': 2}])
        await self.route(old_remove)
        await self.action('rc:remove:0')
        self.assertEqual((await self.state.get_data())['recipe_create_materials'], [{'resource_id': second, 'quantity': 2}])
        self.assertEqual(await self.db.execute_query('SELECT * FROM recipes'), [])

    async def test_material_prompt_commit_failure_preserves_payload_and_rejects_new_screen(self):
        await self.use_durable_state()
        first, _ = await self.compose_preview()
        await self.action('rc:page:0')
        before = await self.state.get_data()
        previous_state = await self.state.get_state()
        old_select = self.callback(await self.signed(f'rc:select:{first}'))
        original_set_state = self.state.set_state
        sent = []

        async def delivered(*args, **kwargs):
            self.assertEqual(self.db._transaction_depth, 0)
            sent.append(kwargs['reply_markup'])
            return self.message

        async def partial_commit(next_state):
            await original_set_state(next_state)
            raise RuntimeError('state commit failed')

        with patch.object(types.Message, 'edit_text', new=AsyncMock(side_effect=delivered)), patch.object(self.state, 'set_state', side_effect=partial_commit):
            with self.assertRaisesRegex(RuntimeError, 'state commit failed'):
                await self.route(old_select)
        restored = FSMContext(SQLiteFSMStorage(self.db), self.state.key)
        after = await restored.get_data()
        self.assertEqual({key: value for key, value in after.items() if key != 'admin_pending_screen'}, before)
        self.assertIn('admin_pending_screen', after)
        self.assertEqual(await restored.get_state(), previous_state)
        new_back = sent[-1].inline_keyboard[0][0].callback_data
        await self.route(self.callback(new_back))
        self.assertEqual(await self.state.get_data(), after)
        await self.text('99')
        self.assertEqual(await self.state.get_data(), after)
        await self.route(old_select)
        self.assertNotIn('admin_pending_screen', await self.state.get_data())
        await self.text('3')
        materials = (await self.state.get_data())['recipe_create_materials']
        self.assertEqual(next(item['quantity'] for item in materials if item['resource_id'] == first), 3)

    async def test_output_quantity_delivery_failure_does_not_publish_candidate(self):
        await self.use_durable_state()
        result_id = await self.db.add_resource('Эликсир', '🧪', 'alchemy')
        await self.start()
        await self.action(f'rc:select:{result_id}')
        before = await self.state.get_data()
        failure = TelegramNetworkError(SendMessage(chat_id=1, text='materials'), 'offline')
        with patch.object(types.Message, 'answer', new=AsyncMock(side_effect=failure)):
            with self.assertRaises(TelegramNetworkError):
                await self.text('7')
        after = await self.state.get_data()
        self.assertEqual({key: value for key, value in after.items() if key != 'admin_pending_screen'}, before)
        self.assertEqual(await self.state.get_state(), creation.RecipeCreationStates.output_quantity.state)
        await self.text('99')
        self.assertEqual((await self.state.get_data())['recipe_create_quantity'], 1)
