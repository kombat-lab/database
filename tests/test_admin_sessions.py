import unittest
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Router, types
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from admin_sessions import AdminScreenMiddleware, tag_admin_keyboard
from admin_utils import GenericEditStates, register_generic_handlers, show_edit_menu


class AdminScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = Bot("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
        self.user = types.User(id=1, is_bot=False, first_name="Admin")
        self.message = types.Message(message_id=10, date=1, chat=types.Chat(id=1, type="private"), from_user=self.user, text="input").as_(self.bot)
        self.storage = MemoryStorage()
        self.state = FSMContext(self.storage, StorageKey(bot_id=self.bot.id, chat_id=1, user_id=1))
        self.router = Router()
        self.router.callback_query.outer_middleware(AdminScreenMiddleware())
        self.entity = {'id': 1, 'name': 'Ресурс A', 'note': '', 'emoji': '⚔️'}
        self.update = AsyncMock()
        self.config = {
            'name': 'resource', 'name_ru': 'Ресурс', 'get_page_func': AsyncMock(return_value=[]),
            'get_by_id_func': AsyncMock(side_effect=lambda ident: dict(self.entity, id=ident)),
            'update_func': self.update, 'delete_func': AsyncMock(), 'item_callback_prefix': 'resource_edit',
            'list_state': GenericEditStates.select_field, 'list_title': 'Ресурсы', 'add_button': False,
            'add_button_text': '', 'add_callback': '', 'edit_fields': [('name', 'Название'), ('note', 'Примечание'), ('emoji', 'Эмодзи')],
            'integer_fields': [], 'select_options': {}, 'display_mapping': {},
        }
        register_generic_handlers(self.router, lambda: {'resource': self.config})
        self.patches = [
            patch.object(Bot, 'edit_message_text', new=AsyncMock(return_value=self.message)),
            patch.object(Bot, 'send_rich_message', new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, 'edit_text', new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, 'answer', new=AsyncMock(return_value=self.message)),
            patch.object(types.Message, 'delete', new=AsyncMock()),
            patch.object(types.CallbackQuery, 'answer', new=AsyncMock()),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.storage.close()
        await self.bot.session.close()

    def callback(self, payload, *, message_id=10, user_id=1):
        return types.CallbackQuery(id='screen-test', from_user=self.user.model_copy(update={'id': user_id}), chat_instance='chat', data=payload, message=self.message.model_copy(update={'message_id': message_id})).as_(self.bot)

    async def route(self, event):
        kind = 'callback_query' if isinstance(event, types.CallbackQuery) else 'message'
        await self.router.propagate_event(kind, event, bot=self.bot, state=self.state, raw_state=await self.state.get_state())

    async def button(self, payload):
        token = (await self.state.get_data())['admin_screen']['token']
        return f'{payload}~{token}'

    async def open_item(self, ident=1):
        await show_edit_menu(self.callback('open'), self.state, ident, self.config, dict(self.entity, id=ident))

    async def test_old_item_button_cannot_edit_new_item_even_with_same_message(self):
        await self.open_item(1)
        old = await self.button('edit_field_name')
        await self.state.clear()
        await self.open_item(2)
        await self.route(self.callback(old))
        await self.route(self.message.model_copy(update={'text': 'wrong name'}))
        self.update.assert_not_awaited()
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)

    async def test_current_button_routes_after_token_is_removed(self):
        await self.open_item()
        await self.route(self.callback(await self.button('edit_field_name')))
        self.assertEqual(await self.state.get_state(), GenericEditStates.new_value.state)
        await self.route(self.message.model_copy(update={'text': 'Новое имя'}))
        self.update.assert_awaited_once_with(1, name='Новое имя')
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)

    async def test_wrong_user_message_and_context_are_rejected(self):
        await self.open_item()
        payload = await self.button('edit_field_name')
        await self.route(self.callback(payload, user_id=2))
        await self.route(self.callback(payload, message_id=11))
        await self.state.update_data(entity_id=2)
        await self.route(self.callback(payload))
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)
        self.update.assert_not_awaited()

    async def test_untagged_legacy_field_button_is_rejected(self):
        await self.open_item()
        await self.route(self.callback('edit_field_name'))
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)

    async def test_reply_to_old_prompt_is_rejected(self):
        await self.open_item()
        await self.route(self.callback(await self.button('edit_field_name')))
        old_prompt = self.message.model_copy(update={'message_id': 9})
        await self.route(self.message.model_copy(update={'text': 'wrong name', 'reply_to_message': old_prompt}))
        self.update.assert_not_awaited()

    async def test_untagged_skip_cannot_clear_note(self):
        await self.open_item()
        await self.route(self.callback(await self.button('edit_field_note')))
        await self.route(self.callback('optional_note_skip'))
        self.update.assert_not_awaited()
        await self.route(self.callback(await self.button('optional_note_skip')))
        self.update.assert_awaited_once_with(1, note='')

    async def test_delivery_failure_does_not_leave_write_state(self):
        await self.open_item()
        await self.route(self.callback(await self.button('edit_field_name')))
        with patch.object(Bot, 'send_rich_message', new=AsyncMock(side_effect=RuntimeError('delivery unavailable'))):
            with self.assertRaisesRegex(RuntimeError, 'delivery unavailable'):
                await self.route(self.message.model_copy(update={'text': 'Новое имя'}))
        await self.route(self.message.model_copy(update={'text': 'Новое имя'}))
        self.update.assert_awaited_once()
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)

    async def test_name_limit_prevents_write(self):
        await self.open_item()
        await self.route(self.callback(await self.button('edit_field_name')))
        await self.route(self.message.model_copy(update={'text': 'я' * 161}))
        self.update.assert_not_awaited()

    async def test_resource_dependencies_block_confirmation_and_deletion(self):
        self.config['delete_impact_func'] = AsyncMock(return_value='материал используется в 3 рецептах')
        await self.open_item()
        await self.route(self.callback(await self.button('delete_entity')))
        self.config['delete_impact_func'].assert_awaited_once_with(1)
        self.config['delete_func'].assert_not_awaited()
        self.assertNotIn('admin_delete_confirmation', await self.state.get_data())
        self.assertEqual(await self.state.get_state(), GenericEditStates.select_field.state)

    def test_callback_bound_is_utf8_bytes_and_wizard_keeps_own_version(self):
        keyboard = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text='Save', callback_data='gw:' + 'a' * 16 + ':1:save')]])
        self.assertEqual(tag_admin_keyboard(keyboard, 'token'), keyboard)
        long = types.InlineKeyboardMarkup(inline_keyboard=[[types.InlineKeyboardButton(text='Long', callback_data='я' * 30)]])
        with self.assertRaises(ValueError):
            tag_admin_keyboard(long, '12345678')
