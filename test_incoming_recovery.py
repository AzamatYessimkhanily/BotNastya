"""Delayed/missing webhook incidents. All WhatsApp, CRM and AI requests are mocked."""
import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import test_reliability as reliability
from test_reliability import bot, completion
from conversation_memory import load_snapshots, recovery_epoch

HYDRATE = bot._hydrate_chat_history
FOLLOWUP_CHECK = bot._check_followup_history
SEND = bot.send_whatsapp


class IncomingRecoveryTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    setUp = reliability.ReliabilityTests.setUp
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown

    def row(self, identity='message-1', text='8 жаста, иә', timestamp=None, **fields):
        return dict({'type': 'incoming', 'typeMessage': 'textMessage', 'chatId': self.chat,
                     'idMessage': identity, 'timestamp': int(time.time()) if timestamp is None else timestamp,
                     'textMessage': text}, **fields)

    def response(self, rows):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)

    def request(self, row):
        return SimpleNamespace(json=AsyncMock(return_value={
            'typeWebhook': 'incomingMessageReceived', 'senderData': {'chatId': row['chatId']},
            'timestamp': row['timestamp'], 'idMessage': row['idMessage'],
            'messageData': bot._journal_message_data(row)}))

    async def drain(self):
        while bot._background_tasks:
            await asyncio.wait_for(asyncio.gather(*list(bot._background_tasks)), 3)

    async def stop_and_forget_memory(self):
        tasks = list(bot._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for name in ('pending_inputs', '_processing_inputs', 'incoming_pending', 'incoming_versions',
                     'seen_incoming_ids', 'message_buffers', 'chat_history', 'conversation_facts',
                     'latest_incoming_timestamps', 'pending_answers', '_dialog_locks', '_incoming_locks'):
            getattr(bot, name).clear()

    async def test_hour_late_questions_are_accepted_and_grouped_into_one_reply(self):
        now = int(time.time())
        rows = [self.row('duration', 'неше сағаттан сабақ?', now-3757),
                self.row('name', 'Тестовый ученик', now-3698),
                self.row('schedule', 'Расписание қандай?', now-3632)]
        self.ai.return_value = completion('Кестені нақтылау үшін BINOM мектебінің атауын айтыңызшы.')
        self.ai.side_effect = None
        with patch.object(bot, 'INCOMING_RECOVERY_SINCE', now-7200), \
             patch.object(bot, 'WA_INCOMING_MAX_AGE_SEC', 86400), patch.object(bot, 'BUFFER_DELAY', .01):
            for row in rows:
                await bot.handle_webhook(self.request(row))
            await self.drain()
            for row in rows:
                await bot.handle_webhook(self.request(row))
        self.send.assert_awaited_once()
        self.assertIn('Расписание қандай?', str(self.ai.call_args.kwargs['messages']))
        self.assertNotIn(self.chat, bot.pending_inputs)

    async def test_activation_floor_survives_restart_but_does_not_replay_old_chats(self):
        now = int(time.time())
        self.assertEqual(recovery_epoch(bot.CONVERSATION_STATE_FILE, now-7200), now-7200)
        with patch.object(bot, 'BOT_STARTED_AT', now), patch.object(bot, 'WA_INCOMING_MAX_AGE_SEC', 86400):
            await bot._load_conversations()
            self.assertFalse(bot._should_skip_stale_incoming({'timestamp': now-3600}))
            self.assertTrue(bot._should_skip_stale_incoming({'timestamp': now-90000}))
            self.assertTrue(bot._should_skip_stale_incoming({'timestamp': now-8000}))

    async def test_missing_webhook_for_new_chat_is_recovered_once(self):
        rows = [self.row('greeting', 'Здравствуйте'), self.row('voucher', 'Есть шахматы по ваучеру? 13 лет')]
        self.ai.side_effect = [completion('Подскажите, в каком филиале хотите заниматься?')]
        with patch('httpx.AsyncClient.get', AsyncMock(return_value=self.response(rows))), \
             patch.object(bot, 'BUFFER_DELAY', .01):
            await bot._recover_incoming_tick()
            await self.drain()
            await bot._recover_incoming_tick()
            await bot.handle_webhook(self.request(rows[1]))
        self.send.assert_awaited_once()
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '13')

    async def test_late_journal_entry_is_not_lost_to_a_timestamp_cursor(self):
        now = int(time.time())
        rows = [self.row('first', 'Здравствуйте', now-120)]
        later = self.row('late', 'Нужны занятия онлайн', now-60)
        self.ai.side_effect = [completion('Для кого рассматриваете занятия?'), completion('Какой возраст ученика?')]
        with patch.object(bot, 'INCOMING_RECOVERY_SINCE', now-3600), \
             patch('httpx.AsyncClient.get', AsyncMock(side_effect=[self.response(rows), self.response(rows+[later])])), \
             patch.object(bot, 'BUFFER_DELAY', .01):
            await bot._recover_incoming_tick()
            await self.drain()
            await bot._recover_incoming_tick()
            await self.drain()
        self.assertEqual(self.send.await_count, 2)

    async def test_recovery_ignores_pre_activation_groups_deleted_and_opted_out(self):
        now = int(time.time())
        blocked = '77009999999@c.us'
        bot.contact_opt_out.add(blocked)
        rows = [self.row('old', timestamp=now-60), self.row('group', chatId='123@g.us'),
                self.row('deleted', isDeleted=True), self.row('blocked', chatId=blocked),
                self.row('outgoing', type='outgoing')]
        with patch.object(bot, 'INCOMING_RECOVERY_SINCE', now), \
             patch('httpx.AsyncClient.get', AsyncMock(return_value=self.response(rows))):
            await bot._recover_incoming_tick()
        self.assertFalse(bot.pending_inputs)
        self.send.assert_not_awaited()

    async def test_durable_input_survives_restart_during_buffer(self):
        row = self.row(text='Ребёнку 8 лет, хотим шахматы')
        with patch.object(bot, 'BUFFER_DELAY', 30):
            await bot.handle_webhook(self.request(row))
            await asyncio.sleep(0)
            snapshot = dict(load_snapshots(bot.CONVERSATION_STATE_FILE))[self.chat]
            self.assertEqual(snapshot['pending_inputs'][0]['id'], row['idMessage'])
            await self.stop_and_forget_memory()
        self.ai.side_effect = [completion('В каком филиале хотите заниматься?')]
        with patch.object(bot, 'BUFFER_DELAY', .01):
            await bot._load_conversations()
            await self.drain()
            await bot.handle_webhook(self.request(row))
        self.send.assert_awaited_once()
        self.assertFalse(dict(load_snapshots(bot.CONVERSATION_STATE_FILE))[self.chat]['pending_inputs'])
        self.assertEqual(bot.latest_incoming_timestamps[self.chat], row['timestamp'])

    async def test_restart_during_generation_replays_without_duplicate_history(self):
        started = asyncio.Event()
        async def blocked_model(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.ai.side_effect = blocked_model
        row = self.row(text='Ребёнку 8 лет, хотим шахматы')
        with patch.object(bot, 'BUFFER_DELAY', .01):
            await bot.handle_webhook(self.request(row))
            await asyncio.wait_for(started.wait(), 2)
            await self.stop_and_forget_memory()
            self.ai.side_effect = [completion('Какой филиал удобен?')]
            await bot._load_conversations()
            await self.drain()
        self.send.assert_awaited_once()
        users = [m for m in bot.chat_history[self.chat] if m['role'] == 'user']
        self.assertEqual(len(users), 1)
        self.assertFalse(bot.pending_inputs)

    async def test_existing_history_recovers_age_and_quoted_text_without_replacement(self):
        bot.chat_history[self.chat] = [{'role': 'assistant', 'content': 'Балаңыз неше жаста?'}]
        bot.latest_incoming_timestamps[self.chat] = int(time.time())
        rows = [self.row('age'), self.row('question', '', typeMessage='quotedMessage',
                                        extendedTextMessage={'text': 'неше сағаттан сабақ?'})]
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=self.response(rows))):
            await HYDRATE(self.chat)
            await HYDRATE(self.chat)
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '8')
        self.assertEqual([m['content'] for m in bot.chat_history[self.chat]].count('8 жаста, иә'), 1)
        self.assertIn('неше сағаттан сабақ?', str(bot.chat_history[self.chat]))
        self.assertEqual(bot.chat_history[self.chat][0]['role'], 'assistant')

    async def test_history_failure_is_retried_and_old_age_does_not_replace_correction(self):
        bot.chat_history[self.chat] = [{'role': 'user', 'content': 'Теперь 9 лет'}]
        bot.conversation_facts[self.chat] = {'age': '9'}
        with patch('httpx.AsyncClient.post', AsyncMock(side_effect=[httpx.ConnectTimeout('secret'),
                                                                 self.response([self.row()])])) as post:
            await HYDRATE(self.chat)
            await HYDRATE(self.chat)
        self.assertEqual(post.await_count, 2)
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '9')

    async def test_context_import_does_not_swallow_newer_question(self):
        now = int(time.time())
        bot.latest_incoming_timestamps[self.chat] = now-5
        row = self.row('newer', timestamp=now)
        with patch('httpx.AsyncClient.post', AsyncMock(return_value=self.response([row]))):
            await HYDRATE(self.chat)
        self.assertNotIn('newer', bot.seen_incoming_ids[self.chat])

    async def test_followup_recovers_answer_instead_of_reminding(self):
        now = int(time.time())
        bot.latest_incoming_timestamps[self.chat] = now-7200
        self.ai.side_effect = [completion('Қай BINOM мектебінде оқиды?')]
        with patch.object(bot, 'INCOMING_RECOVERY_SINCE', now-8000), \
             patch.object(bot, 'WA_INCOMING_MAX_AGE_SEC', 86400), \
             patch.object(bot, '_check_followup_history', FOLLOWUP_CHECK), \
             patch('httpx.AsyncClient.post', AsyncMock(return_value=self.response([self.row(timestamp=now-7100)]))), \
             patch.object(bot, 'BUFFER_DELAY', .01):
            self.assertFalse(await bot._send_followup_message(self.chat))
            await self.drain()
        self.send.assert_awaited_once()
        self.assertNotIn(self.chat, bot.followup_sent)
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '8')

    async def test_followup_is_deferred_when_history_cannot_be_checked(self):
        with patch.object(bot, '_check_followup_history', FOLLOWUP_CHECK), \
             patch('httpx.AsyncClient.post', AsyncMock(side_effect=httpx.ConnectTimeout('offline'))):
            self.assertFalse(await bot._send_followup_message(self.chat))
        self.send.assert_not_awaited()
        self.assertNotIn(self.chat, bot.followup_sent)

    async def test_followup_rechecks_history_after_send_cooldown(self):
        bot.latest_incoming_timestamps[self.chat] = int(time.time())
        row = self.row('arrived-during-wait', text='Расписание?')
        with patch.object(bot, 'send_whatsapp', SEND), \
             patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)), \
             patch('httpx.AsyncClient.post', AsyncMock(return_value=self.response([row]))) as post:
            self.assertFalse(await bot._send_followup_message(self.chat))
        self.assertTrue(all('/getChatHistory/' in c.args[0] for c in post.call_args_list))
        self.assertNotIn(self.chat, bot.followup_sent)

    async def test_recovery_failure_does_not_disable_next_poll_or_leak_token(self):
        with patch('httpx.AsyncClient.get', AsyncMock(side_effect=[httpx.ConnectTimeout('secret-token'),
                                                                self.response([])])), \
             self.assertLogs(bot.logger, level='WARNING') as logs:
            await bot._recover_incoming_tick()
            await bot._recover_incoming_tick()
        self.assertNotIn('secret-token', '\n'.join(logs.output))

    async def test_old_unanswered_message_cancels_legacy_reminder(self):
        now = int(time.time())
        bot.latest_incoming_timestamps[self.chat] = now-7200
        with patch.object(bot, 'INCOMING_RECOVERY_SINCE', now), \
             patch.object(bot, '_check_followup_history', FOLLOWUP_CHECK), \
             patch('httpx.AsyncClient.post', AsyncMock(return_value=self.response([self.row(timestamp=now-7100)]))):
            self.assertFalse(await bot._send_followup_message(self.chat))
        self.send.assert_not_awaited()
        self.assertIn(self.chat, bot.followup_blocked)

    async def test_disk_failure_does_not_make_retry_look_like_a_duplicate(self):
        row = self.row()
        with patch.object(bot, '_save_conversation', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                await bot.handle_webhook(self.request(row))
        self.assertNotIn(row['idMessage'], bot.seen_incoming_ids[self.chat])
        self.assertFalse(bot.pending_inputs)
        with patch.object(bot, 'BUFFER_DELAY', 30):
            await bot.handle_webhook(self.request(row))
            await asyncio.sleep(0)
        self.assertEqual(len(bot.pending_inputs[self.chat]), 1)

    async def test_more_than_400_known_inputs_are_not_replayed_after_restart(self):
        for i in range(501):
            bot.seen_incoming_ids[self.chat].append(str(i))
        bot._save_conversation(self.chat)
        bot.seen_incoming_ids.clear()
        await bot._load_conversations()
        with patch('httpx.AsyncClient.get', AsyncMock(return_value=self.response([self.row('0')]))):
            await bot._recover_incoming_tick()
        self.assertFalse(bot.pending_inputs)
        self.send.assert_not_awaited()

    async def test_expired_durable_input_is_not_sent_days_after_restart(self):
        now = int(time.time())
        bot.pending_inputs[self.chat] = [{'id': 'expired', 'timestamp': now-90000,
                                         'data': bot._journal_message_data(self.row())}]
        bot._save_conversation(self.chat)
        bot.pending_inputs.clear()
        with patch.object(bot, 'WA_INCOMING_MAX_AGE_SEC', 86400):
            await bot._load_conversations()
        self.assertFalse(bot.pending_inputs)
        self.assertFalse(bot._background_tasks)

    async def test_history_requests_share_rate_limit_after_completion(self):
        starts, finishes = [], []
        async def post(*args, **kwargs):
            starts.append(time.monotonic())
            await asyncio.sleep(.01)
            finishes.append(time.monotonic())
            return self.response([])
        with patch.object(bot, 'GREEN_HISTORY_MIN_INTERVAL_SEC', .02), \
             patch('httpx.AsyncClient.post', AsyncMock(side_effect=post)):
            await asyncio.gather(bot._get_chat_history(self.chat), bot._get_chat_history('77009999999@c.us'))
        self.assertEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1]-finishes[0], .019)


if __name__ == '__main__':
    unittest.main()
