import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

from aiogram import Bot, types
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import EditMessageText

import admin_gear as editor
from database import Database


class GearEditorTests(unittest.IsolatedAsyncioTestCase):
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
        self.stack.enter_context(patch.object(editor, 'db', self.db))
        self.deliver = self.stack.enter_context(patch.object(Bot, 'edit_message_text', new=AsyncMock(return_value=self.message)))
        self.answer = self.stack.enter_context(patch.object(types.Message, 'answer', new=AsyncMock(return_value=self.message)))
        self.stack.enter_context(patch.object(types.Message, 'edit_text', new=AsyncMock(return_value=self.message)))
        self.callback_answer = self.stack.enter_context(patch.object(types.CallbackQuery, 'answer', new=AsyncMock()))

    async def asyncTearDown(self):
        self.stack.close()
        await self.storage.close()
        await self.db.close()
        await self.bot.session.close()

    def callback(self, data, message_id=10, user=None):
        message = self.message.model_copy(update={'message_id': message_id}).as_(self.bot)
        return types.CallbackQuery(id='test', from_user=user or self.user, chat_instance='test',
                                   message=message, data=data).as_(self.bot)

    async def draft(self):
        data = await self.state.get_data()
        return await self.db.get_gear_draft(data['gear_draft_id'], owner_user_id=1, chat_id=1)

    async def route(self, event):
        kind = 'callback_query' if isinstance(event, types.CallbackQuery) else 'message'
        return await editor.gear_router.propagate_event(kind, event, bot=self.bot, state=self.state,
                                                        raw_state=await self.state.get_state())

    async def action(self, action):
        draft = await self.draft()
        await self.route(self.callback(editor.draft_callback(draft, action), message_id=draft['message_id']))

    async def text(self, value):
        await self.route(self.message.model_copy(update={'message_id': 90, 'text': value}).as_(self.bot))

    async def start(self, payload=None):
        await editor.start_gear_editor(self.callback('gear_add_start'), self.state, payload=payload)

    async def complete_profile(self):
        await self.action('input:name')
        await self.text('Клинок проверки')
        await self.action('set:rarity:2')
        await self.action('set:slot:11')
        await self.action('input:emoji')
        await self.text('⚔️')

    async def test_complete_flow_publishes_one_recipe_learning_scroll_and_sources(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        location_id = await self.db.execute_insert('INSERT INTO locations(name,emoji) VALUES (?,?)', ('Лес', '🌲'))
        mob_id = await self.db.execute_insert('INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES (?,?,?,?,?,?,?)',
                                            ('Волк', '🐺', 10, 1, 2, 3, location_id))
        await self.start()
        await self.complete_profile()
        await self.action('craft')
        await self.action(f'choose:material:{resource_id}')
        await self.text('7')
        await self.action('learning_toggle')
        await self.action(f'choose:scroll_mob:{mob_id}')
        await self.action('section:preview')
        old_draft = await self.draft()
        old_save = editor.draft_callback(old_draft, 'save')
        self.assertEqual(await self.db.execute_query('SELECT * FROM gear'), [])
        self.assertEqual(await self.db.execute_query('SELECT * FROM recipes'), [])
        await self.route(self.callback(old_save))
        await self.route(self.callback(old_save))
        saved = await self.db.get_gear_draft(old_draft['draft_id'], owner_user_id=1, chat_id=1)
        result = saved['saved_result']
        self.assertEqual(saved['status'], 'saved')
        self.assertEqual(len(await self.db.execute_query('SELECT * FROM gear')), 1)
        self.assertEqual(len(await self.db.execute_query('SELECT * FROM recipes')), 1)
        materials = await self.db.execute_query('SELECT * FROM recipe_ingredients')
        self.assertEqual([(item['resource_id'], item['quantity']) for item in materials], [(resource_id, 7)])
        scroll = await self.db.get_resource_by_id(result['scroll_resource_id'])
        self.assertEqual(scroll['type'], 'scroll_recipe')
        drops = await self.db.execute_query('SELECT * FROM drops')
        self.assertEqual([(row['mob_id'], row['item_type'], row['item_id']) for row in drops],
                         [(mob_id, 'resource', scroll['id'])])
        self.assertEqual((await self.draft())['payload']['gear_id'], result['gear_id'])

    async def test_back_and_pause_resume_keep_data_and_reject_old_screen(self):
        await self.start()
        await self.complete_profile()
        before = await self.draft()
        stale = editor.draft_callback(before, 'input:name')
        await self.action('section:materials')
        await self.action('section:home')
        self.assertEqual((await self.draft())['payload']['name'], 'Клинок проверки')
        await self.action('pause')
        self.assertIsNone(await self.state.get_state())
        await self.route(self.callback(stale))
        self.assertIsNone(await self.state.get_state())
        await self.route(self.callback('gear_drafts', message_id=20))
        current = await self.db.get_gear_draft(before['draft_id'], owner_user_id=1, chat_id=1)
        await self.route(self.callback(f"gr:{current['draft_id']}:{current['revision']}", message_id=20))
        resumed = await self.draft()
        self.assertEqual(resumed['message_id'], 20)
        self.assertEqual(resumed['payload']['name'], 'Клинок проверки')
        await self.route(self.callback(editor.draft_callback(resumed, 'input:name'), message_id=10))
        self.assertEqual(await self.state.get_state(), editor.GearEditorStates.editing.state)

    async def test_invalid_quantity_retains_prompt_without_publish(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        await self.start()
        await self.action('craft')
        await self.action(f'choose:material:{resource_id}')
        before = await self.draft()
        await self.text('0')
        self.assertEqual((await self.draft())['revision'], before['revision'])
        self.assertEqual(await self.state.get_state(), editor.GearEditorStates.input.state)
        self.assertEqual(await self.db.execute_query('SELECT * FROM recipes'), [])

    async def test_owner_action_is_unavailable_until_learning_requirement_is_saved(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        await self.start()
        await self.complete_profile()
        await self.action('craft')
        await self.action(f'choose:material:{resource_id}')
        await self.text('2')
        await self.action('section:preview')
        await self.action('save')
        await self.action('section:learning')
        buttons = self.deliver.await_args.kwargs['reply_markup'].inline_keyboard
        self.assertFalse(any(button.callback_data.endswith(':owners') for row in buttons for button in row))
        await self.action('owners')
        self.assertEqual(await self.state.get_state(), editor.GearEditorStates.editing.state)
        self.assertFalse((await self.db.get_gear_card((await self.draft())['payload']['gear_id']))['can_learn'])

    async def test_existing_material_match_offers_selection_or_explicit_variant(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        await self.start()
        await self.complete_profile()
        await self.action('craft')
        await self.action('input:new_material_name')
        await self.text('Сталь')
        self.assertEqual(await self.state.get_state(), editor.GearEditorStates.editing.state)
        buttons = self.deliver.await_args.kwargs['reply_markup'].inline_keyboard
        callbacks = [button.callback_data for row in buttons for button in row]
        self.assertTrue(any(f'choose:material:{resource_id}' in callback for callback in callbacks))
        self.assertTrue(any('material_variant' in callback for callback in callbacks))
        await self.action('material_variant')
        await self.text('🪨')
        await self.text('2')
        self.assertTrue((await self.draft())['payload']['materials'][0]['allow_duplicate'])
        self.assertEqual(len(await self.db.get_resource_name_matches('Сталь', 'craft')), 1)
        await self.action('section:preview')
        await self.action('save')
        self.assertEqual(len(await self.db.get_resource_name_matches('Сталь', 'craft')), 2)

    async def test_new_material_is_not_published_until_complete_save(self):
        await self.start()
        await self.complete_profile()
        await self.action('craft')
        await self.action('input:new_material_name')
        await self.text('Новый металл')
        await self.text('🧱')
        await self.text('4')
        self.assertEqual(await self.db.execute_query('SELECT * FROM resources'), [])
        await self.action('save')
        resources = await self.db.execute_query('SELECT * FROM resources')
        self.assertEqual([(row['name'], row['type']) for row in resources], [('Новый металл', 'craft')])

    async def test_duplicate_material_opens_existing_quantity(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        await self.start({'craftable': True, 'materials': [{'resource_id': resource_id, 'quantity': 2}]})
        await self.action(f'choose:material:{resource_id}')
        await self.text('9')
        self.assertEqual((await self.draft())['payload']['materials'], [{'resource_id': resource_id, 'quantity': 9}])

    async def test_search_filters_scrolls_out_of_materials_and_paginates(self):
        for index in range(19):
            await self.db.add_resource(f'Сталь {index:02}', '🧱', 'craft')
        scroll_id = await self.db.add_resource('Сталь свиток', '📜', 'scroll_recipe')
        await self.start({'craftable': True})
        await self.action('pick:material:0')
        keyboard = self.deliver.await_args.kwargs['reply_markup']
        selects = [item.callback_data for row in keyboard.inline_keyboard for item in row if ':choose:material:' in (item.callback_data or '')]
        self.assertEqual(len(selects), 8)
        self.assertFalse(any(data.endswith(f':{scroll_id}') for data in selects))
        await self.action('search:material')
        await self.text('Сталь 17')
        keyboard = self.deliver.await_args.kwargs['reply_markup']
        selects = [item.text for row in keyboard.inline_keyboard for item in row if ':choose:material:' in (item.callback_data or '')]
        self.assertEqual(selects, ['🧱 Сталь 17'])

    async def test_telegram_failure_after_revision_keeps_durable_draft(self):
        await self.start()
        await self.action('input:name')
        self.deliver.side_effect = TelegramNetworkError(method=EditMessageText(text='x'), message='offline')
        with self.assertRaises(TelegramNetworkError):
            await self.text('Сохранённое имя')
        draft = await self.draft()
        self.assertEqual(draft['payload']['name'], 'Сохранённое имя')
        self.assertEqual((await self.state.get_data())['gear_draft_revision'], draft['revision'])
        self.assertEqual(await self.db.execute_query('SELECT * FROM gear'), [])

    async def test_cancel_requires_current_confirmation_and_never_publishes(self):
        await self.start()
        await self.complete_profile()
        await self.action('cancel_prompt')
        draft = await self.draft()
        await self.action('cancel')
        cancelled = await self.db.get_gear_draft(draft['draft_id'], owner_user_id=1, chat_id=1)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(await self.db.execute_query('SELECT * FROM gear'), [])
        self.assertIsNone(await self.state.get_state())


    async def test_existing_scroll_keeps_its_known_sources_when_selected(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        scroll_id = await self.db.add_resource('Изучаемый свиток', '📜', 'scroll_recipe')
        location_id = await self.db.execute_insert('INSERT INTO locations(name,emoji) VALUES (?,?)', ('Лес', '🌲'))
        mob_id = await self.db.execute_insert('INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES (?,?,?,?,?,?,?)',
                                            ('Волк', '🐺', 10, 1, 2, 3, location_id))
        await self.db.execute_query("INSERT INTO drops(mob_id,item_type,item_id) VALUES (?,'resource',?)", (mob_id,scroll_id))
        await self.start({'name':'Клинок', 'rarity':'epic','slot':'основная рука','emoji':'⚔️',
                          'craftable':True,'materials':[{'resource_id':resource_id,'quantity':2}]})
        await self.action(f'choose:scroll:{scroll_id}')
        self.assertEqual((await self.draft())['payload']['scroll_mob_ids'], [mob_id])
        await self.action('save')
        drops = await self.db.execute_query("SELECT mob_id FROM drops WHERE item_type='resource' AND item_id=?", (scroll_id,))
        self.assertEqual([row['mob_id'] for row in drops], [mob_id])

    async def test_full_preview_paginates_without_omitting_materials_or_description(self):
        from telegram_text import split_html
        materials = []
        for index in range(30):
            material_id = await self.db.add_resource(f'Материал {index:02} ' + 'д' * 120, '🧱', 'craft')
            materials.append({'resource_id':material_id,'quantity':index+1})
        await self.start({'name':'Клинок','rarity':'epic','slot':'основная рука','emoji':'⚔️',
                          'note':'Описание ' + 'т' * 1950, 'craftable':True,'materials':materials,'learning_scroll':{}})
        await self.action('section:preview')
        texts = []
        while True:
            text = self.deliver.await_args.kwargs['text']
            self.assertEqual(len(split_html(text)), 1)
            texts.append(text)
            keyboard = self.deliver.await_args.kwargs['reply_markup']
            next_button = next((button for row in keyboard.inline_keyboard for button in row if button.text == 'Следующая страница ▶️'), None)
            if next_button is None:
                break
            await self.route(self.callback(next_button.callback_data))
        self.assertGreater(len(texts), 1)
        joined=''.join(texts)
        self.assertIn('Описание ',joined)
        for index in range(30):
            self.assertIn(f'Материал {index:02}',joined)
        self.assertIn('Изучается один раз',joined)

    async def test_drafts_pagination_reaches_oldest_draft(self):
        for index in range(25):
            await self.db.create_gear_draft(owner_user_id=1,chat_id=1,message_id=10,payload={'name':f'Черновик {index}'})
        seen=set()
        await self.route(self.callback('gear_drafts'))
        while True:
            keyboard=types.Message.edit_text.await_args.kwargs['reply_markup']
            seen.update(button.callback_data.split(':')[1] for row in keyboard.inline_keyboard for button in row if (button.callback_data or '').startswith('gr:'))
            next_button=next((button for row in keyboard.inline_keyboard for button in row if button.text=='Вперёд ▶️'),None)
            if next_button is None:
                break
            await self.route(self.callback(next_button.callback_data))
        self.assertEqual(len(seen),25)

    async def test_explicit_formula_delete_keeps_gear_and_scroll(self):
        resource_id=await self.db.add_resource('Сталь','🧱','craft')
        await self.start({'name':'Клинок','rarity':'epic','slot':'основная рука','emoji':'⚔️',
                          'craftable':True,'materials':[{'resource_id':resource_id,'quantity':2}],'learning_scroll':{}})
        original=await self.draft()
        await self.action('save')
        saved=await self.db.get_gear_draft(original['draft_id'],owner_user_id=1,chat_id=1)
        result=saved['saved_result']
        await self.action('target_delete_prompt:recipe')
        self.assertIsNotNone(await self.db.get_recipe_details(result['recipe_id']))
        await self.action('target_delete_confirm:recipe:keep')
        self.assertIsNotNone(await self.db.get_gear_by_id(result['gear_id']))
        self.assertIsNotNone(await self.db.get_resource_by_id(result['scroll_resource_id']))
        self.assertIsNone(await self.db.get_recipe_details(result['recipe_id']))
        self.assertFalse((await self.draft())['payload']['craftable'])

    async def test_other_user_cannot_operate_current_draft(self):
        await self.start({'name':'Клинок'})
        draft=await self.draft()
        stranger=types.User(id=2,is_bot=False,first_name='Other')
        await self.route(self.callback(editor.draft_callback(draft,'input:name'),user=stranger))
        self.assertEqual((await self.draft())['revision'],draft['revision'])
        self.assertEqual(await self.state.get_state(),editor.GearEditorStates.editing.state)

    async def add_mob(self, name, location_name):
        location = await self.db.execute_query('SELECT id FROM locations WHERE name=?', (location_name,))
        location_id = location[0]['id'] if location else await self.db.execute_insert(
            'INSERT INTO locations(name,emoji) VALUES (?,?)', (location_name, '🌲'))
        return await self.db.execute_insert(
            'INSERT INTO mobs(name,emoji,hp,dust_min,dust_max,exp,location_id) VALUES (?,?,?,?,?,?,?)',
            (name, '🐺', 10, 1, 2, 3, location_id))

    def source_buttons(self, kind):
        return [button for row in self.deliver.await_args.kwargs['reply_markup'].inline_keyboard
                for button in row if f':choose:{kind}:' in (button.callback_data or '')]

    async def click_source(self, kind, mob_id):
        item = next(button for button in self.source_buttons(kind)
                    if button.callback_data.endswith(f':{mob_id}'))
        await self.route(self.callback(item.callback_data))

    async def test_drop_picker_searches_locations_and_keeps_gear_and_scroll_choices_independent(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        forest_ids = [await self.add_mob(f'Волк {index:02}', 'Тёмный лес') for index in range(10)]
        swamp_ids = [await self.add_mob(f'Жаба {index:02}', 'Болото') for index in range(2)]
        await self.start({'name': 'Клинок', 'rarity': 'epic', 'slot': 'основная рука', 'emoji': '⚔️',
                          'craftable': True, 'materials': [{'resource_id': resource_id, 'quantity': 2}],
                          'learning_scroll': {}})
        await self.action('section:sources')
        await self.action('pick:gear_mob:0')
        self.assertEqual(len(self.source_buttons('gear_mob')), 8)
        await self.action('search:gear_mob')
        self.assertIn('локации', self.deliver.await_args.kwargs['text'])
        await self.text('тЁмный ЛЕС')
        self.assertTrue(all('Тёмный лес' in button.text for button in self.source_buttons('gear_mob')))
        await self.click_source('gear_mob', forest_ids[0])
        self.assertTrue(self.source_buttons('gear_mob')[0].text.startswith('☑️'))
        await self.action('pick:gear_mob:1')
        self.assertEqual(len(self.source_buttons('gear_mob')), 2)
        old_button = self.source_buttons('gear_mob')[-1].callback_data
        await self.route(self.callback(old_button))
        await self.route(self.callback(old_button))
        self.assertEqual((await self.draft())['payload']['gear_mob_ids'], [forest_ids[0], forest_ids[-1]])
        self.assertEqual((await self.state.get_data())['gear_draft_gear_mob_page'], 1)
        await self.action('selected_sources:gear_mob')
        self.assertEqual(len(self.source_buttons('gear_mob')), 2)
        await self.click_source('gear_mob', forest_ids[0])
        self.assertEqual(len(self.source_buttons('gear_mob')), 1)
        await self.action('section:sources')
        await self.action('pick:scroll_mob:0')
        self.assertEqual(len(self.source_buttons('scroll_mob')), 8)
        self.assertTrue(all(button.text.startswith('⬜') for button in self.source_buttons('scroll_mob')))
        await self.action('search:scroll_mob')
        await self.text('болото')
        self.assertEqual(len(self.source_buttons('scroll_mob')), 2)
        for mob_id in swamp_ids:
            await self.click_source('scroll_mob', mob_id)
        await self.action('selected_sources:scroll_mob')
        self.assertEqual(len(self.source_buttons('scroll_mob')), 2)
        await self.action('section:sources')
        await self.action('pick:gear_mob:0')
        self.assertEqual(len(self.source_buttons('gear_mob')), 1)
        self.assertIn('Тёмный лес', self.source_buttons('gear_mob')[0].text)
        self.assertTrue((await self.state.get_data())['gear_draft_gear_mob_selected_only'])
        await self.action('clearsearch:gear_mob')
        self.assertEqual((await self.state.get_data())['gear_draft_gear_mob_query'], '')
        self.assertEqual(len(self.source_buttons('gear_mob')), 1)
        self.assertEqual(await self.db.execute_query('SELECT * FROM drops'), [])
        before = await self.draft()
        await self.action('save')
        saved = await self.db.get_gear_draft(before['draft_id'], owner_user_id=1, chat_id=1)
        result = saved['saved_result']
        drops = await self.db.execute_query('SELECT mob_id,item_type,item_id FROM drops')
        self.assertEqual({(item['mob_id'], item['item_type'], item['item_id']) for item in drops},
                         {(forest_ids[-1], 'gear', result['gear_id']),
                          *( (mob_id, 'resource', result['scroll_resource_id']) for mob_id in swamp_ids )})

    async def test_selected_sources_page_clamps_after_last_item_is_unselected(self):
        mob_ids = [await self.add_mob(f'Волк {index:02}', 'Лес') for index in range(9)]
        await self.start({'gear_mob_ids': mob_ids})
        await self.action('pick:gear_mob:0')
        await self.action('selected_sources:gear_mob')
        await self.action('pick:gear_mob:1')
        self.assertEqual(len(self.source_buttons('gear_mob')), 1)
        await self.click_source('gear_mob', mob_ids[-1])
        self.assertEqual((await self.state.get_data())['gear_draft_gear_mob_page'], 0)
        self.assertEqual(len(self.source_buttons('gear_mob')), 8)
        self.assertEqual((await self.draft())['payload']['gear_mob_ids'], mob_ids[:-1])

    async def test_material_search_does_not_filter_drop_sources_and_back_keeps_selection(self):
        resource_id = await self.db.add_resource('Сталь', '🧱', 'craft')
        mob_id = await self.add_mob('Волк', 'Лес')
        await self.start({'craftable': True})
        await self.action('search:material')
        await self.text('Сталь')
        await self.action('pick:gear_mob:0')
        self.assertEqual(len(self.source_buttons('gear_mob')), 1)
        await self.click_source('gear_mob', mob_id)
        await self.action('search:gear_mob')
        back = self.deliver.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        await self.route(self.callback(back.callback_data))
        self.assertTrue(self.source_buttons('gear_mob')[0].text.startswith('☑️'))
        self.assertEqual((await self.draft())['payload']['gear_mob_ids'], [mob_id])
        await self.action('pick:material:0')
        selects = [button.callback_data for row in self.deliver.await_args.kwargs['reply_markup'].inline_keyboard
                   for button in row if ':choose:material:' in (button.callback_data or '')]
        self.assertEqual(len(selects), 1)
        self.assertTrue(selects[0].endswith(f':{resource_id}'))

    async def test_removed_mob_is_rejected_without_changing_draft_or_selection(self):
        mob_id = await self.add_mob('Волк', 'Лес')
        await self.start()
        await self.action('pick:gear_mob:0')
        button = self.source_buttons('gear_mob')[0]
        before = await self.draft()
        await self.db.execute_query('DELETE FROM mobs WHERE id=?', (mob_id,))
        await self.route(self.callback(button.callback_data))
        self.assertEqual((await self.draft())['revision'], before['revision'])
        self.assertEqual((await self.draft())['payload'].get('gear_mob_ids', []), [])
        self.assertIn('Моб удалён', self.callback_answer.await_args.args[0])

    async def test_deleted_selected_sources_can_be_removed_and_cleared_before_save(self):
        first_id = await self.add_mob('Первый волк', 'Лес')
        second_id = await self.add_mob('Второй волк', 'Лес')
        await self.start({'name': 'Клинок', 'rarity': 'epic', 'slot': 'основная рука', 'emoji': '⚔️'})
        await self.action('pick:gear_mob:0')
        await self.click_source('gear_mob', first_id)
        await self.click_source('gear_mob', second_id)
        selected_button = next(button for button in self.source_buttons('gear_mob')
                               if button.callback_data.endswith(f':{first_id}'))
        await self.db.execute_query('DELETE FROM mobs WHERE id IN (?,?)', (first_id, second_id))
        await self.route(self.callback(selected_button.callback_data))
        self.assertEqual((await self.draft())['payload']['gear_mob_ids'], [second_id])
        self.assertEqual(self.source_buttons('gear_mob'), [])
        clear_button = next(button for row in self.deliver.await_args.kwargs['reply_markup'].inline_keyboard
                            for button in row if button.text == 'Снять все отметки')
        await self.route(self.callback(clear_button.callback_data))
        cleared = await self.draft()
        self.assertEqual(cleared['payload']['gear_mob_ids'], [])
        await self.route(self.callback(clear_button.callback_data))
        self.assertEqual((await self.draft())['revision'], cleared['revision'])
        await self.action('save')
        saved = await self.db.get_gear_draft(cleared['draft_id'], owner_user_id=1, chat_id=1)
        self.assertEqual(saved['status'], 'saved')
        self.assertIsNotNone(await self.db.get_gear_by_id(saved['saved_result']['gear_id']))
        self.assertEqual(await self.db.execute_query('SELECT * FROM drops'), [])
