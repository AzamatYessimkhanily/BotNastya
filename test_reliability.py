"""Offline regression tests. No production API calls or WhatsApp messages."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# Use fake credentials even when run on the production host.
for key in ('OPENAI_API_KEY', 'GREEN_API_ID', 'GREEN_API_TOKEN', 'MOYKLASS_API_KEY'):
    os.environ[key] = 'offline-test'
import bot
import attendance_monitor as attendance
from runtime_state import write_json_atomic


def completion(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        role='assistant', content=content, tool_calls=calls))])


def tool(name, args, call_id='call1'):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def enterContext(self, context):
        # unittest.TestCase.enterContext was only added in Python 3.11;
        # the production host uses Python 3.9.
        value = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        return value

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ('CRM_SENT_STATE_FILE', 'FOLLOWUP_BLOCKED_FILE', 'FAILED_LEADS_FILE'):
            self.enterContext(patch.object(bot, name, str(Path(self.tmp.name) / name)))
        self.enterContext(patch.object(attendance, 'ATTENDANCE_STATE_FILE', str(Path(self.tmp.name) / 'attendance.json')))
        # Unexpected network use fails the test, including indirect API paths.
        self.enterContext(patch('httpx.AsyncClient.send', AsyncMock(side_effect=AssertionError('Unexpected HTTP'))))
        self.ai = self.enterContext(patch.object(bot.openai_client.chat.completions, 'create', AsyncMock(side_effect=AssertionError('Unexpected AI'))))
        self.send = self.enterContext(patch.object(bot, 'send_whatsapp', AsyncMock(return_value=True)))
        self.lookup = self.enterContext(patch.object(bot.crm, 'find_user_smart', AsyncMock(return_value=None)))
        for name in ('chat_history', 'message_buffers', 'known_users', 'client_dossiers', 'last_activity',
                     'followups', 'handoff_completed', 'session_manager_notified', 'seen_incoming_ids',
                     'crm_notify_recent', '_dialog_locks', '_incoming_locks', '_wa_last_chat_ts'):
            getattr(bot, name).clear()
        for name in ('_crm_sent_keys', '_crm_inflight_keys', 'followup_blocked', 'session_registered_leads', '_wa_send_times'):
            getattr(bot, name).clear()
        attendance._missed_sent.clear()
        attendance._debt_sent.clear()
        bot._wa_send_lock = None
        bot._wa_last_send_ts = 0
        bot._wa_paused_until = 0
        self.chat = '77001234567@c.us'

    async def asyncTearDown(self):
        tasks = list(bot._background_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def queue(self, text='Хочу записаться в Аркаду'):
        bot.message_buffers[self.chat] = {'messages': [text]}

    async def dispatch(self):
        return await bot._dispatch_moyklass_webhook('test', 'payment_new', {'userId': 42, 'paymentId': 5},
                                                  'Оплата получена.', ['77001234567'], False)

    async def test_parallel_crm_duplicates_send_once(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            started.set()
            await release.wait()
            return True
        self.send.side_effect = slow
        first = asyncio.create_task(self.dispatch())
        await started.wait()
        second = await self.dispatch()
        self.assertEqual(second['delivered'], 0)
        release.set()
        self.assertEqual((await first)['delivered'], 1)
        self.assertEqual(self.send.await_count, 1)
        self.assertFalse(bot._crm_inflight_keys)

    async def test_failed_crm_delivery_can_retry(self):
        self.send.side_effect = [False, True]
        self.assertEqual((await self.dispatch())['delivered'], 0)
        self.assertFalse(bot._crm_sent_keys)
        self.assertEqual((await self.dispatch())['delivered'], 1)
        self.assertEqual((await self.dispatch())['deduplicated'], 1)
        self.assertEqual(self.send.await_count, 2)

    async def test_cancelled_crm_releases_reservation(self):
        self.send.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.dispatch()
        self.assertFalse(bot._crm_inflight_keys)
        self.assertFalse(bot._crm_sent_keys)

    async def test_quota_counts_concurrent_pending_requests(self):
        with patch.multiple(bot, WA_RATE_LIMIT_ENABLED=True, WA_MAX_PER_HOUR=1, WA_MAX_PER_DAY=10,
                            WA_MIN_INTERVAL_SEC=0, WA_PER_CHAT_COOLDOWN_SEC=0):
            results = await asyncio.gather(*(bot._wa_acquire_send_slot(str(i)) for i in range(10)))
        self.assertEqual(sum(results), 1)

    async def test_chat_cooldown_does_not_block_other_chats(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def sleep(_):
            started.set()
            await release.wait()
        bot._wa_last_chat_ts['slow'] = time.time()
        with patch.multiple(bot, WA_RATE_LIMIT_ENABLED=True, WA_MAX_PER_HOUR=20, WA_MAX_PER_DAY=20,
                            WA_MIN_INTERVAL_SEC=0, WA_PER_CHAT_COOLDOWN_SEC=20), patch.object(bot.asyncio, 'sleep', sleep):
            task = asyncio.create_task(bot._wa_acquire_send_slot('slow'))
            await started.wait()
            try:
                self.assertTrue(await asyncio.wait_for(bot._wa_acquire_send_slot('other'), 0.2))
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_webhook_rejects_malformed_and_group_events(self):
        cases = [None, [], 'text', {}, {'typeWebhook': 'incomingMessageReceived', 'senderData': []},
                 {'typeWebhook': 'incomingMessageReceived', 'timestamp': int(time.time()),
                  'senderData': {'chatId': '123@g.us'}, 'messageData': {}}]
        with patch.object(bot, '_spawn_task') as spawn:
            for data in cases:
                self.assertEqual(await bot.handle_webhook(SimpleNamespace(json=AsyncMock(return_value=data))), 'ok')
            self.assertEqual(await bot.handle_webhook(SimpleNamespace(json=AsyncMock(side_effect=ValueError))), 'ok')
            spawn.assert_not_called()

    async def test_voice_ack_is_immediate_and_duplicate_ignored(self):
        release = asyncio.Event()
        data = {'typeWebhook': 'incomingMessageReceived', 'timestamp': int(time.time()),
                'senderData': {'chatId': self.chat}, 'idMessage': 'voice-id',
                'messageData': {'typeMessage': 'audioMessage'}}
        with patch.object(bot, '_buffer_incoming', AsyncMock(side_effect=lambda *a: None)) as buffer:
            async def block(*_):
                await release.wait()
            buffer.side_effect = block
            request = SimpleNamespace(json=AsyncMock(return_value=data))
            self.assertEqual(await asyncio.wait_for(bot.handle_webhook(request), .2), 'ok')
            await bot.handle_webhook(request)
            await asyncio.sleep(0)
            self.assertEqual(buffer.await_count, 1)
            release.set()

    async def test_voice_then_text_preserves_ingress_order(self):
        release = asyncio.Event()
        seen = []
        async def buffer(sender, data):
            if data['kind'] == 'voice':
                await release.wait()
            seen.append(data['kind'])
        lock = asyncio.Lock()
        with patch.object(bot, '_buffer_incoming', buffer):
            a = asyncio.create_task(bot._receive_incoming(self.chat, {'kind': 'voice'}, lock))
            b = asyncio.create_task(bot._receive_incoming(self.chat, {'kind': 'text'}, lock))
            await asyncio.sleep(0)
            self.assertEqual(seen, [])
            release.set()
            await asyncio.gather(a, b)
        self.assertEqual(seen, ['voice', 'text'])

    async def test_lookup_failure_returns_fallback(self):
        self.lookup.side_effect = RuntimeError('CRM unavailable')
        self.queue()
        await bot.process_dialog(self.chat)
        self.send.assert_awaited_once_with(self.chat, bot._DIALOG_ERROR_FALLBACK)

    async def test_failed_lead_does_not_close_dialog(self):
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {'client_phone': 77001234567, 'preference': 'Аркада'})]),
                               completion('Пока оформление не подтверждено.')]
        with patch.object(bot.crm, 'create_lead', AsyncMock(return_value=bot.crm._handoff_message('Менеджер', '+7700', success=False))) as create:
            self.queue()
            await bot.process_dialog(self.chat)
            self.assertEqual(create.call_args.kwargs['phone'], '77001234567')
        self.assertNotIn(self.chat, bot.handoff_completed)
        self.assertNotIn(self.chat, bot.session_registered_leads)

    async def test_success_survives_final_ai_failure(self):
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {'preference': 'Аркада'})]), RuntimeError('AI timeout')]
        with patch.object(bot.crm, 'create_lead', AsyncMock(return_value=bot.crm._handoff_message('Менеджер', '+7700', success=True))):
            self.queue()
            await bot.process_dialog(self.chat)
        self.assertIn(self.chat, bot.session_registered_leads)
        self.assertIn(self.chat, bot.handoff_completed)

    async def test_repeated_tool_call_creates_one_lead(self):
        args = {'preference': 'Аркада', 'client_name': 'Тест'}
        self.ai.side_effect = [completion(calls=[tool('register_client_request', args, 'a'), tool('register_client_request', args, 'b')]), completion('Заявку оформила.')]
        with patch.object(bot.crm, 'create_lead', AsyncMock(return_value=bot.crm._handoff_message('Менеджер', '+7700', success=True))) as create:
            self.queue()
            await bot.process_dialog(self.chat)
            create.assert_awaited_once()
        replies = [m for m in bot.chat_history[self.chat] if isinstance(m, dict) and m.get('role') == 'tool']
        self.assertEqual({m['tool_call_id'] for m in replies}, {'a', 'b'})

    async def test_unsent_answer_is_not_in_history(self):
        self.ai.side_effect = [completion('Стоимость занятия уточню.')]
        self.send.return_value = False
        self.queue()
        await bot.process_dialog(self.chat)
        self.assertFalse(any(isinstance(m, dict) and m.get('role') == 'assistant' for m in bot.chat_history[self.chat]))

    async def test_model_handoff_claim_does_not_mark_registration(self):
        self.ai.side_effect = [completion('Передаю вашу заявку управляющему.')]
        self.queue()
        await bot.process_dialog(self.chat)
        self.assertNotIn(self.chat, bot.session_registered_leads)
        self.assertFalse(bot._is_handoff_message(self.send.call_args.args[1]))

    async def test_callback_does_not_guess_branch_from_system_prompt(self):
        bot.chat_history[self.chat] = [{'role': 'system', 'content': bot.SYSTEM_PROMPT}, {'role': 'user', 'content': 'Свяжитесь со мной'}]
        with patch.object(bot.crm, 'notify_client_callback_request', AsyncMock(return_value='Филиал неизвестен')) as notify:
            self.assertFalse(await bot._ensure_manager_callback(self.chat, 'Свяжитесь со мной'))
            self.assertIsNone(notify.call_args.kwargs['filial_id'])

    async def test_repeated_callback_is_not_sent_twice(self):
        bot.session_manager_notified[self.chat] = time.time()
        self.ai.side_effect = [completion(calls=[tool('request_manager_callback', {'reason': 'Перезвоните'})]), completion('Управляющий свяжется с вами.')]
        with patch.object(bot.crm, 'notify_client_callback_request', AsyncMock()) as notify:
            self.queue('Перезвоните')
            await bot.process_dialog(self.chat)
            notify.assert_not_awaited()

    async def test_shared_school_branch_routes_by_name(self):
        result = await bot.crm.notify_client_callback_request(client_name='Тест', client_phone='77001234567', filial_id=54672, matched_key='quantum')
        self.assertIn('MGR_PHONE=', result)
        self.assertEqual(self.send.call_args.args[0], bot.phone_to_chat_id(bot.QUANTUM_MANAGER_PHONE))

    async def test_failed_callback_does_not_claim_delivery(self):
        self.send.return_value = False
        result = await bot.crm.notify_client_callback_request(client_name='Тест', client_phone='77001234567', filial_id=37754)
        self.assertNotIn('MGR_PHONE=', result)
        self.assertIn('НЕ ОТПРАВЛЕН', result)

    async def test_crm_token_refresh_is_serialized(self):
        crm = bot.MoyKlassCRM('fake')
        async def respond(*args, **kwargs):
            await asyncio.sleep(.01)
            return SimpleNamespace(status_code=200, json=lambda: {'accessToken': 'token'})
        with patch('httpx.AsyncClient.post', AsyncMock(side_effect=respond)) as post:
            headers = await asyncio.gather(*(crm._get_headers() for _ in range(10)))
            post.assert_awaited_once()
        self.assertTrue(all(h['x-access-token'] == 'token' for h in headers))

    async def test_lookup_uses_matching_user_instead_of_first_result(self):
        crm = bot.MoyKlassCRM('fake')
        response = SimpleNamespace(status_code=200, json=lambda: {'users': [
            {'id': 1, 'name': 'Other', 'phone': ''},
            {'id': 2, 'name': 'Correct', 'phone': '77001234567', 'filials': [42763]},
        ]})
        with patch.object(crm, '_get_headers', AsyncMock(return_value={'token': 'fake'})), \
             patch.object(crm, 'get_schedule', AsyncMock(return_value='Нет уроков')), \
             patch('httpx.AsyncClient.get', AsyncMock(return_value=response)) as get:
            found = await crm.find_user_smart('77001234567')
        self.assertEqual(found['user']['id'], 2)
        self.assertEqual(found['dossier']['filial_id'], 42763)
        self.assertEqual(get.call_args.kwargs['params']['phone'], '77001234567')

    async def test_attendance_does_not_mark_failed_delivery_as_sent(self):
        now = datetime(2026, 9, 24, 12, tzinfo=bot._SCHOOL_TZ)
        lesson = {'id': 10, 'date': '2026-09-24', 'beginTime': '11:50', 'classId': 20}
        dispatch = AsyncMock(return_value={'status': 'ok', 'delivered': 0, 'deduplicated': 0})
        with patch.object(attendance, 'datetime', wraps=datetime) as dt, \
             patch.object(attendance, 'fetch_lessons_for_day', AsyncMock(return_value=[lesson])), \
             patch.object(attendance, 'fetch_lesson_records', AsyncMock(return_value=[{'userId': 5, 'visit': False}])), \
             patch.multiple(attendance, ATTENDANCE_DRY_RUN=False, ATTENDANCE_SEED_ONLY=False, ATTENDANCE_UNMARKED_AFTER_MIN=10):
            dt.now.return_value = now
            kwargs = dict(crm=bot.crm, base_url='unused', dispatch_client_message=dispatch,
                          get_trainer_phone=AsyncMock(return_value=''), get_admin_phone=AsyncMock(return_value=''),
                          user_is_mailing_client=AsyncMock(return_value=True), get_user_phone=AsyncMock(return_value='77001234567'))
            stats = await attendance.attendance_tick(**kwargs)
            self.assertEqual(stats['missed_sent'], 0)
            self.assertFalse(attendance._missed_sent)
            dispatch.return_value['delivered'] = 1
            stats = await attendance.attendance_tick(**kwargs)
            self.assertEqual(stats['missed_sent'], 1)
            self.assertTrue(attendance._missed_sent)

    async def test_followup_next_day_retries_failed_delivery(self):
        now = datetime(2026, 9, 24, 12, tzinfo=bot._SCHOOL_TZ)
        bot.followups[self.chat] = {'last_client_ts': now.timestamp() - 86400, 'stage': 1}
        with patch.object(bot, 'datetime', wraps=datetime) as dt, patch.object(bot, '_send_followup_message', AsyncMock(return_value=False)):
            dt.now.return_value = now
            await bot._followup_tick()
        self.assertIn(self.chat, bot.followups)

    async def test_followup_next_day_obeys_quiet_hours(self):
        now = datetime(2026, 9, 24, 23, tzinfo=bot._SCHOOL_TZ)
        bot.followups[self.chat] = {'last_client_ts': now.timestamp() - 86400, 'stage': 1}
        with patch.object(bot, 'datetime', wraps=datetime) as dt, patch.object(bot, '_send_followup_message', AsyncMock()) as send:
            dt.now.return_value = now
            await bot._followup_tick()
            send.assert_not_awaited()

    def test_short_answers_and_empty_model_answer(self):
        self.assertEqual(bot.sanitize_bot_outgoing('Рақмет.'), 'Рақмет.')
        self.assertEqual(bot.sanitize_bot_outgoing(None), bot._DIALOG_ERROR_FALLBACK)
        self.assertNotIn('заявк', bot.sanitize_bot_outgoing('Произошла техническая ошибка.').lower())

    def test_phone_matching_requires_full_number(self):
        for phone in ('', None, '1234567', '77009999999'):
            self.assertFalse(bot.MoyKlassCRM._phone_tail_matches(phone, '7001234567'))
        self.assertTrue(bot.MoyKlassCRM._phone_tail_matches('+7 700 123 45 67', '7001234567'))

    def test_trim_history_preserves_tool_groups_and_system_context(self):
        history = [{'role': 'system', 'content': 'rules'}]
        for i in range(100):
            history += [{'role': 'user', 'content': str(i)}, {'role': 'assistant', 'tool_calls': [{'id': str(i)}]},
                        {'role': 'tool', 'tool_call_id': str(i), 'content': 'ok'}, {'role': 'assistant', 'content': 'answer'}]
        bot.chat_history[self.chat] = history
        bot._trim_dialog_history(self.chat)
        self.assertLessEqual(len(history), 61)
        self.assertEqual(history[0]['content'], 'rules')
        self.assertEqual(history[1]['role'], 'user')
        self.assertEqual(history[-1]['content'], 'answer')
        bot._repair_dangling_tool_calls(self.chat)
        self.assertTrue(any(bot._message_tool_call_ids(m) for m in history))

    def test_atomic_state_failure_preserves_previous_file(self):
        path = Path(self.tmp.name) / 'state.json'
        write_json_atomic(path, {'old': 1})
        with patch('runtime_state.os.replace', side_effect=OSError('disk error')):
            with self.assertRaises(OSError):
                write_json_atomic(path, {'new': 2})
        self.assertEqual(json.loads(path.read_text()), {'old': 1})
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [path])

    def test_repair_finds_old_orphan_before_valid_tool_group(self):
        bot.chat_history[self.chat] = [
            {'role': 'assistant', 'tool_calls': [{'id': 'orphan'}]},
            {'role': 'user', 'content': 'new question'},
            {'role': 'assistant', 'tool_calls': [{'id': 'valid'}]},
            {'role': 'tool', 'tool_call_id': 'valid', 'content': 'ok'},
        ]
        bot._repair_dangling_tool_calls(self.chat)
        self.assertEqual(len(bot.chat_history[self.chat]), 3)
        self.assertEqual(bot.chat_history[self.chat][0]['content'], 'new question')


if __name__ == '__main__':
    unittest.main()
