"""Customer incident regressions: no real WhatsApp, CRM or model calls."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_reliability as reliability
from test_reliability import bot, completion, tool
from conversation_memory import STOP_CONTACT, update_facts, remove_answered_questions

SEND = bot.send_whatsapp
HYDRATE = bot._hydrate_chat_history


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    setUp = reliability.ReliabilityTests.setUp
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown
    queue = reliability.ReliabilityTests.queue

    def request(self, text, identity='incoming-1', timestamp=None):
        return SimpleNamespace(json=AsyncMock(return_value={
            'typeWebhook': 'incomingMessageReceived', 'idMessage': identity,
            'timestamp': timestamp or int(time.time()), 'senderData': {'chatId': self.chat},
            'messageData': {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': text}},
        }))

    async def test_stop_is_silent_and_survives_restart(self):
        bot.followups[self.chat] = {'stage': 0}
        await bot.handle_webhook(self.request('Не пиши мне'))
        await bot.handle_webhook(self.request('Не пиши мне', 'second'))
        self.send.assert_not_awaited()
        self.ai.assert_not_awaited()
        self.assertNotIn(self.chat, bot.followups)
        bot.contact_opt_out.clear()
        await bot._load_conversations()
        self.assertIn(self.chat, bot.contact_opt_out)
        await bot.handle_webhook(self.request('Ок', 'third'))
        self.send.assert_not_awaited()

    async def test_old_stop_request_still_blocks(self):
        await bot.handle_webhook(self.request('Не пишите больше', timestamp=1))
        self.assertIn(self.chat, bot.contact_opt_out)

    async def test_stop_variants_and_no_false_positive_for_learning(self):
        for text in ('Не пиши мне', 'Не пишите', 'Не нужно мне писать', 'Больше не пишите нам',
                     'Не звоните', 'Не беспокойте меня', 'Перестаньте писать', 'Отпишите меня',
                     'Маған жазбаңыз', 'Мазаламаңыз', 'Stop', 'unsubscribe'):
            with self.subTest(text=text):
                self.assertRegex(text, STOP_CONTACT)
        for text in ('Не умеет писать', 'Не нужно онлайн, хотим Аркаду', 'Не хочу утром, можно вечером?',
                     'Хочу записаться', 'Не начинающий, 4 разряд', 'Мне никто не звонит', 'Тренер не пишет'):
            with self.subTest(text=text):
                self.assertIsNone(STOP_CONTACT.search(text))

    async def test_stop_cancels_pending_buffer(self):
        with patch.object(bot, 'BUFFER_DELAY', 30):
            await bot._buffer_incoming(self.chat, {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'Расписание'}})
            pending = bot.message_buffers[self.chat]['timer']
            await bot.handle_webhook(self.request('Не пиши мне'))
            await asyncio.sleep(0)
        self.assertTrue(pending.cancelled())
        self.assertNotIn(self.chat, bot.message_buffers)

    async def test_optout_rechecked_after_outgoing_cooldown(self):
        async def wait_slot(_):
            bot._stop_contact(self.chat)
            return True
        with patch.object(bot, '_wa_acquire_send_slot', wait_slot), patch('httpx.AsyncClient.post', AsyncMock()) as post:
            self.assertFalse(await SEND(self.chat, 'Ответ'))
            post.assert_not_awaited()

    async def test_optout_also_blocks_crm_and_voice_error(self):
        bot._stop_contact(self.chat)
        with patch.object(bot, '_wa_acquire_send_slot', AsyncMock()) as quota:
            self.assertFalse(await SEND(self.chat, 'Уведомление CRM', sanitize=False))
            quota.assert_not_awaited()

    async def test_internal_numbers_ignore_sales_but_allow_service_notifications(self):
        self.chat = bot.phone_to_chat_id(next(iter(bot.BRANCH_PHONES.values())))
        await bot.handle_webhook(self.request('Здравствуйте'))
        self.queue()
        await bot.process_dialog(self.chat)
        self.ai.assert_not_awaited()
        self.send.assert_not_awaited()
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))
        with patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)), \
             patch('httpx.AsyncClient.post', AsyncMock(return_value=SimpleNamespace(status_code=200, json=lambda: {'idMessage': 'service'}))):
            self.assertTrue(await SEND(self.chat, 'Новый лид', sanitize=False))

    async def test_extra_internal_numbers_normalize(self):
        with patch.dict('os.environ', {'INTERNAL_PHONES': '8 (700) 123-45-67, +7 701 000 0000'}):
            self.assertTrue(bot._is_internal_chat(self.chat))

    async def test_employee_recipient_is_persistently_excluded(self):
        await bot._dispatch_moyklass_webhook('moyklass-webhook-employee', 'lesson_changed',
            {'userId': 50, 'lessonId': 100}, 'Расписание изменилось', [self.chat.split('@')[0]], False)
        bot.internal_chats.clear()
        await bot._load_conversations()
        self.assertTrue(bot._is_internal_chat(self.chat))

    async def test_business_auto_reply_does_not_start_sales(self):
        for text in ('Благодарим за обращение. Вы обратились в нерабочее время. Мы ответим вам при первой же возможности.',
                     'Сіз жұмыс уақытынан тыс уақытта хабарластыңыз.'):
            self.queue(text)
            await bot.process_dialog(self.chat)
        self.ai.assert_not_awaited()
        self.send.assert_not_awaited()
        self.assertNotIn(self.chat, bot.followups)

    async def test_ack_does_not_restart_conversation(self):
        for text in ('Окей', 'Спасибо!', 'Жақсы.', '👍', 'Хорошо, спасибо'):
            self.queue(text)
            await bot.process_dialog(self.chat)
        self.send.assert_not_awaited()
        self.ai.assert_not_awaited()

    async def test_yes_to_trial_offer_and_substantive_question_still_get_answer(self):
        bot.chat_history[self.chat] = [{'role': 'assistant', 'content': 'Записать на пробный урок?'}]
        self.ai.side_effect = [completion('Как зовут ученика?'), completion('Время уточнит управляющий.')]
        self.queue('Да')
        await bot.process_dialog(self.chat)
        self.queue('Хорошо, а когда урок?')
        await bot.process_dialog(self.chat)
        self.assertEqual(self.ai.await_count, 2)
        self.assertEqual(self.send.await_count, 2)

    async def test_crm_greeting_and_ack_combined_are_silent(self):
        bot.record_crm_notification_in_history(self.chat, 'Ученик не пришёл на урок. Свяжитесь с тренером.')
        self.queue('Сәлеметсіз бе! Жақсы.')
        await bot.process_dialog(self.chat)
        self.ai.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_crm_context_survives_restart(self):
        bot.record_crm_notification_in_history(self.chat, 'Свяжитесь с тренером.')
        bot.chat_history.clear()
        bot.crm_notify_recent.clear()
        bot.known_users.clear()
        await bot._load_conversations()
        self.queue('Здравствуйте')
        await bot.process_dialog(self.chat)
        self.ai.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_screenshot_age_rank_branch_dont_repeat(self):
        self.ai.side_effect = [completion('Имеет ли ваш ребёнок опыт в шахматах или он начинающий?'),
                               completion('Какой формат обучения вам удобнее: Аркада, Камал или онлайн?')]
        bot.message_buffers[self.chat] = {'messages': ['Ребенок 11 лет.', '4 разряд шахмат']}
        await bot.process_dialog(self.chat)
        self.assertNotIn('начинающий', self.send.call_args.args[1])
        self.queue('Аркада')
        await bot.process_dialog(self.chat)
        reply = self.send.call_args.args[1]
        self.assertNotIn('Какой формат', reply)
        self.assertIn('Как зовут', reply)
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '11')
        self.assertIn('4 разряд', bot.conversation_facts[self.chat]['experience'])
        self.assertEqual(bot.conversation_facts[self.chat]['preference'], 'Аркада')
        prompt = str(self.ai.call_args.kwargs['messages'])
        self.assertIn('есть опыт', prompt)
        self.assertIn('Аркада', prompt)

    async def test_history_and_facts_survive_timeout_and_restart(self):
        bot.chat_history[self.chat] = [{'role': 'system', 'content': bot.SYSTEM_PROMPT},
            {'role': 'user', 'content': 'Ребёнок 11 лет, 4 разряд'},
            {'role': 'assistant', 'content': 'Какой филиал удобнее?'}]
        bot.conversation_facts[self.chat] = {'audience': 'ребёнок', 'age': '11', 'experience': '4 разряд'}
        bot.last_activity[self.chat] = time.time() - bot.SESSION_TIMEOUT - 1
        bot._save_conversation(self.chat)
        bot.chat_history.clear()
        bot.conversation_facts.clear()
        await bot._load_conversations()
        self.ai.side_effect = [completion('Как зовут ученика?')]
        self.queue('Аркада')
        await bot.process_dialog(self.chat)
        self.assertIn('4 разряд', str(self.ai.call_args.kwargs['messages']))
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '11')

    async def test_new_message_during_generation_discards_old_answer(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def model(**kwargs):
            if self.ai.await_count == 1:
                started.set()
                await release.wait()
                return completion('Имеет ли ребёнок опыт?')
            return completion('Какой филиал вам удобен?')
        self.ai.side_effect = model
        self.queue('Ребёнок 11 лет')
        task = asyncio.create_task(bot.process_dialog(self.chat))
        await started.wait()
        with patch.object(bot, 'BUFFER_DELAY', .01):
            await bot.handle_webhook(self.request('4 разряд шахмат'))
            await asyncio.sleep(.02)
            release.set()
            await task
            await asyncio.gather(*list(bot._background_tasks))
        self.send.assert_awaited_once()
        self.assertNotIn('Имеет ли', self.send.call_args.args[1])
        self.assertIn('4 разряд', bot.conversation_facts[self.chat]['experience'])

    async def test_stop_during_model_request_prevents_tool_and_answer(self):
        async def model(**kwargs):
            bot._stop_contact(self.chat)
            return completion(calls=[tool('register_client_request', {'preference': 'Аркада'})])
        self.ai.side_effect = model
        with patch.object(bot.crm, 'create_lead', AsyncMock()) as create:
            self.queue()
            await bot.process_dialog(self.chat)
            create.assert_not_awaited()
        self.send.assert_not_awaited()

    async def test_only_one_followup_even_after_restart_or_new_question(self):
        bot.chat_history[self.chat] = [{'role': 'user', 'content': 'Цена?'}]
        self.assertTrue(await bot._send_followup_message(self.chat))
        bot.followup_sent.clear()
        bot.followup_blocked.clear()
        await bot._load_conversations()
        bot._update_followup_tracking(self.chat, 'Какая цена?')
        self.assertFalse(await bot._send_followup_message(self.chat))
        self.send.assert_awaited_once()

    async def test_followup_waiting_on_rate_limit_cancels_on_new_input(self):
        async def slot(_):
            bot.incoming_versions[self.chat] += 1
            return True
        with patch.object(bot, 'send_whatsapp', SEND), patch.object(bot, '_wa_acquire_send_slot', slot), \
             patch('httpx.AsyncClient.post', AsyncMock()) as post:
            self.assertFalse(await bot._send_followup_message(self.chat))
            post.assert_not_awaited()
        self.assertNotIn(self.chat, bot.followup_sent)

    async def test_history_import_order_failed_outgoing_and_current_message(self):
        bot.seen_incoming_ids[self.chat].append('current')
        rows = [
            {'type': 'incoming', 'idMessage': 'current', 'textMessage': 'Аркада'},
            {'type': 'outgoing', 'statusMessage': 'failed', 'textMessage': 'Не доставлено'},
            {'type': 'outgoing', 'statusMessage': 'read', 'textMessage': 'Какой филиал?'},
            {'type': 'incoming', 'textMessage': 'Ребёнок 11 лет, 4 разряд'},
        ]
        for row in rows:
            row['chatId'] = self.chat
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)) as post:
            await HYDRATE(self.chat)
            await HYDRATE(self.chat)
            post.assert_awaited_once()
        self.assertEqual([m['content'] for m in bot.chat_history[self.chat] if m['role'] != 'system'],
                         ['Ребёнок 11 лет, 4 разряд', 'Какой филиал?'])
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '11')

    async def test_historical_optout_is_honored_without_sending(self):
        response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: [
            {'chatId': self.chat, 'type': 'incoming', 'textMessage': 'Не пиши мне'}])
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=response)):
            await HYDRATE(self.chat)
        self.assertIn(self.chat, bot.contact_opt_out)
        self.send.assert_not_awaited()

    async def test_branch_comparison_is_not_recorded_as_choice(self):
        for text in ('Аркада или Камал?', 'Не Аркада', 'Можно онлайн?', 'Сколько стоит онлайн?'):
            self.assertNotIn('preference', update_facts({}, text))
        self.assertEqual(update_facts({}, 'Аркада')['preference'], 'Аркада')


if __name__ == '__main__':
    unittest.main()
