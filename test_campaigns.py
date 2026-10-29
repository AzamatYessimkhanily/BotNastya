"""Offline tests of real queue persistence, send failures and reply context."""
from datetime import datetime, timedelta
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import httpx

from campaigns import CampaignStore, TZ, normalize_phone, dialog_context
from campaign_import import read_contacts
import test_reliability as reliability
from test_reliability import bot, completion, tool

NOW = datetime(2026, 10, 28, 12, tzinfo=TZ).timestamp()
SPEC = json.loads(Path(__file__).with_name('campaign_gm_intellect.json').read_text())
# Fixed fixture keeps boundary checks independent of the live offer and pace.
SPEC.update(expires_at='2026-10-31T23:59:59+05:00', interval_sec=600, hourly=6, daily=30)


def rows(phone='77001234567', consent='да', evidence='Форма GMCA, согласие от 2026-10-20'):
    return [{'phone': phone, 'consent': consent, 'evidence': evidence}]


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = CampaignStore(Path(self.tmp.name) / 'campaigns.sqlite3')
        self.cid = SPEC['id']
        self.store.create(SPEC, NOW)

    def active(self):
        self.store.import_rows(self.cid, rows())
        self.store.control(self.cid, 'activate', NOW)

    def test_import_normalization_missing_consent_and_duplicates(self):
        result = self.store.import_rows(self.cid, rows('8 (700) 123-45-67') + rows('+77001234567') + rows('77001234568', 'нет') + rows('77001234569', 'да', '') + rows('broken'))
        self.assertEqual(result['counts'], {'pending': 1, 'duplicate': 1, 'invalid': 1, 'no_consent': 2, 'suppressed': 0})
        self.assertEqual(normalize_phone('+90 507 400 3612'), '905074003612')
        self.assertRaises(ValueError, normalize_phone, '7.7001234567E10')
        self.assertRaises(ValueError, normalize_phone, '7001234567')

    def test_duplicate_conflicting_consent_is_excluded(self):
        self.store.import_rows(self.cid, rows() + rows(consent='нет'))
        self.assertRaises(ValueError, self.store.control, self.cid, 'activate', NOW)

    def test_explicit_operator_confirmation_does_not_override_refusal(self):
        result = self.store.import_rows(self.cid, rows(consent='', evidence='') + rows('77001234568', 'нет', ''),
                                       consent_note='Пользователь подтвердил согласие всего списка 28.10.2026')
        self.assertEqual(result['counts']['pending'], 1)
        self.assertEqual(result['counts']['no_consent'], 1)
        with self.store.db() as db:
            evidence = db.execute('SELECT evidence FROM recipients WHERE phone=?', ('77001234567',)).fetchone()[0]
        self.assertIn('Пользователь подтвердил', evidence)

    def test_working_window_is_rechecked_after_waiting(self):
        self.active()
        day = datetime(2026, 10, 28, tzinfo=TZ)
        at = lambda hour, minute=0: day.replace(hour=hour, minute=minute).timestamp()
        self.assertIsNone(self.store.claim(at(8, 59)))
        self.assertIsNotNone(self.store.claim(at(9)))
        self.assertTrue(self.store.eligible(self.cid, '77001234567', at(19, 59)))
        self.assertFalse(self.store.eligible(self.cid, '77001234567', at(20)))
        self.assertFalse(self.store.eligible(self.cid, '77001234567', at(8, 59)))

    def test_undated_campaign_has_no_implicit_expiry(self):
        self.store.create({**SPEC, 'id': 'undated', 'expires_at': None}, NOW)
        self.store.import_rows('undated', rows())
        self.store.control('undated', 'activate', NOW + 120*86400)
        entry = self.store.claim(NOW + 120*86400)
        self.assertIsNotNone(entry)
        self.assertTrue(self.store.eligible('undated', '77001234567', NOW + 120*86400))
        self.assertNotIn('ИСТЁК', dialog_context(self.store.context('77001234567'), NOW + 120*86400))

    def test_restart_pauses_campaign_and_never_resends_unknown_result(self):
        self.active()
        self.assertIsNotNone(self.store.claim(NOW))
        store = CampaignStore(self.store.path)
        store.recover()
        self.assertEqual(store.report(self.cid)['counts'], {'uncertain': 1})
        self.assertIsNone(store.claim(NOW + 600))
        self.assertRaises(ValueError, store.control, self.cid, 'activate', NOW)

    def test_limits_pause_cancel_and_expiry(self):
        self.store.import_rows(self.cid, rows() + rows('77001234568'))
        self.store.control(self.cid, 'activate', NOW)
        self.assertIsNotNone(self.store.claim(NOW))
        self.assertIsNone(self.store.claim(NOW + 599))
        self.store.control(self.cid, 'pause', NOW)
        self.assertFalse(self.store.eligible(self.cid, '77001234567', NOW))
        self.store.control(self.cid, 'activate', NOW)
        self.assertIsNotNone(self.store.claim(NOW + 600))
        self.assertIsNone(self.store.claim(NOW + 4*86400))
        self.assertEqual(self.store.report(self.cid)['reason'], 'expired')

    def test_no_sending_in_quiet_hours_or_without_consent(self):
        self.assertRaises(ValueError, self.store.control, self.cid, 'activate', NOW)
        self.active()
        self.assertIsNone(self.store.claim(NOW + 9*3600))

    def test_no_repeat_across_campaigns_and_optout_persists(self):
        self.active()
        self.store.claim(NOW)
        self.store.result(self.cid, '77001234567', 'accepted', 'id1', now=NOW)
        self.store.control(self.cid, 'cancel', NOW)
        self.store.create({**SPEC, 'id': 'second'}, NOW)
        self.store.import_rows('second', rows())
        self.store.control('second', 'activate', NOW)
        self.assertIsNone(self.store.claim(NOW+3600))
        self.store.suppress('77001234567', 'stop', NOW)
        self.store.create({**SPEC, 'id': 'third'}, NOW)
        self.assertEqual(self.store.import_rows('third', rows())['counts']['suppressed'], 1)

    def test_early_delivery_receipt_and_reply_race_preserve_state(self):
        self.active()
        self.store.claim(NOW)
        self.store.delivery('id1', 'read')
        self.store.inbound('77001234567', '+ Камал', 'reply1', NOW)
        self.store.result(self.cid, '77001234567', 'accepted', 'id1', now=NOW)
        self.store.delivery('id1', 'sent')
        context = self.store.context('77001234567')
        self.assertEqual(context['state'], 'replied')
        self.assertEqual(context['delivery'], 'read')
        self.store.inbound('77001234567', '+ Камал', 'reply1', NOW)
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM replies').fetchone()[0], 1)

    def test_uncertain_or_delivery_error_stops_queue(self):
        self.active()
        self.store.claim(NOW)
        self.store.result(self.cid, '77001234567', 'uncertain', reason='timeout', now=NOW)
        self.assertEqual(self.store.report(self.cid)['status'], 'paused')
        self.assertIn('55 000', dialog_context(self.store.context('77001234567'), NOW))
        self.assertIn('ИСТЁК', dialog_context(self.store.context('77001234567'), NOW+4*86400))

    def test_bare_no_only_declines_the_initial_invitation(self):
        self.active()
        self.store.claim(NOW)
        self.assertFalse(self.store.inbound('77001234567', '+', 'first', NOW))
        self.assertFalse(self.store.inbound('77001234567', 'Нет', 'second', NOW))
        self.assertTrue(self.store.inbound('77001234567', 'Нет, спасибо', 'third', NOW))
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM suppressed').fetchone()[0], 1)

    def test_initial_decline_is_idempotent_across_webhook_and_buffer(self):
        self.active()
        self.store.claim(NOW)
        self.assertTrue(self.store.inbound('77001234567', 'Нет', 'first', NOW))
        self.assertTrue(self.store.inbound('77001234567', 'Нет', 'first', NOW))

    def test_inbound_cancelling_unsent_campaign_does_not_invent_context(self):
        self.active()
        self.store.claim(NOW)
        self.store.inbound('77001234567', 'Хочу узнать расписание', 'first', NOW)
        self.assertFalse(self.store.eligible(self.cid, '77001234567', NOW))
        self.store.result(self.cid, '77001234567', 'skipped', now=NOW)
        self.assertIsNone(self.store.context('77001234567'))

    def test_unsent_reservation_can_be_deferred_but_accepted_cannot(self):
        self.active()
        self.store.claim(NOW)
        self.assertTrue(self.store.defer(self.cid, '77001234567', NOW+86400, 'active_chat'))
        self.assertIsNone(self.store.claim(NOW+3600))
        self.assertEqual(self.store.report(self.cid)['status'], 'active')
        self.assertIsNotNone(self.store.claim(NOW+86400))
        self.store.result(self.cid, '77001234567', 'accepted', 'id1', now=NOW+86400)
        self.assertFalse(self.store.defer(self.cid, '77001234567', NOW+2*86400, 'must_not_retry'))

    def test_new_incoming_defers_pending_contact_instead_of_dropping_it(self):
        self.active()
        self.store.inbound('77001234567', 'Вопрос по оплате', 'before', NOW)
        self.assertIsNone(self.store.context('77001234567'))
        self.assertIsNone(self.store.claim(NOW))
        self.assertEqual(self.store.report(self.cid)['counts'], {'pending': 1})
        self.assertIsNotNone(self.store.claim(NOW+86400))

    def test_rolling_hour_and_day_caps_survive_reopening(self):
        self.store.import_rows(self.cid, [row for n in range(35) for row in rows(str(77001234000 + n))])
        self.store.control(self.cid, 'activate', NOW)
        with self.store.db() as db:
            for n in range(6):
                db.execute("UPDATE recipients SET state='accepted',attempt_at=? WHERE phone=?", (NOW-3000+n*60, str(77001234000+n)))
        reopened = CampaignStore(self.store.path)
        self.assertIsNone(reopened.claim(NOW))
        with self.store.db() as db:
            for n in range(30):
                db.execute("UPDATE recipients SET state='accepted',attempt_at=? WHERE phone=?", (NOW-4000-n*60, str(77001234000+n)))
        self.assertIsNone(reopened.claim(NOW))

    def test_failed_receipt_pauses_without_retrying_recipient(self):
        self.active()
        self.store.claim(NOW)
        self.store.result(self.cid, '77001234567', 'accepted', 'id1', now=NOW)
        self.store.delivery('id1', 'failed')
        self.assertEqual(self.store.report(self.cid)['status'], 'paused')
        self.assertEqual(self.store.report(self.cid)['deliveries'], {'failed': 1})
        self.assertIsNone(self.store.claim(NOW+3600))

    def test_no_whatsapp_account_skips_recipient_and_continues_queue(self):
        self.store.import_rows(self.cid, rows() + rows('77001234568'))
        self.store.control(self.cid, 'activate', NOW)
        self.store.claim(NOW)
        # The receipt may precede the HTTP response; reconciliation must agree.
        self.store.delivery('id1', 'noAccount')
        self.store.result(self.cid, '77001234567', 'accepted', 'id1', now=NOW)
        self.store.delivery('id1', 'sent')
        report = self.store.report(self.cid)
        self.assertEqual(report['status'], 'active')
        self.assertEqual(report['counts'], {'failed': 1, 'pending': 1})
        self.assertEqual(report['deliveries'], {'noAccount': 1})
        self.assertIsNone(self.store.context('77001234567'))
        self.assertEqual(self.store.claim(NOW+600)['phone'], '77001234568')

    def test_provider_restriction_still_pauses_all_sending(self):
        self.active()
        self.store.delivery('unrelated-message', 'suspended')
        self.assertEqual(self.store.report(self.cid)['status'], 'paused')
        self.assertIsNone(self.store.claim(NOW))

    def test_csv_reader_header_and_phone_as_text(self):
        path = Path(self.tmp.name) / 'contacts.csv'
        path.write_text('Телефон;Имя;Согласие;Подтверждение согласия\n87771234567;Тест;да;Форма\n', encoding='utf-8-sig')
        self.assertEqual(read_contacts(path)[0]['phone'], '87771234567')
        path.write_text('Телефон\n87771234567\n')
        self.assertEqual(read_contacts(path)[0]['consent'] if 'consent' in read_contacts(path)[0] else '', '')

    def test_faster_limits_keep_previous_attempts_and_durable_daily_budget(self):
        self.store.import_rows(self.cid, [row for n in range(302) for row in rows(str(77001234000 + n))])
        self.store.control(self.cid, 'activate', NOW)
        self.store.claim(NOW)
        self.store.set_limits(self.cid, 60, 60, 300)
        reopened = CampaignStore(self.store.path)
        self.assertIsNone(reopened.claim(NOW + 59))
        self.assertIsNotNone(reopened.claim(NOW + 60))
        with self.store.db() as db:
            for n in range(300):
                db.execute("UPDATE recipients SET state='accepted',attempt_at=? WHERE phone=?",
                           (NOW-4000-n, str(77001234000+n)))
        self.assertIsNone(reopened.claim(NOW + 120))
        self.assertRaises(ValueError, reopened.set_limits, self.cid, 10, 200, 1000)
        self.assertEqual(reopened.report(self.cid)['daily'], 300)

    def test_unlimited_daily_queue_runs_full_window_and_keeps_minute_spacing_after_restart(self):
        self.store.import_rows(self.cid, [row for n in range(661) for row in rows(str(77001234000+n))])
        self.store.set_limits(self.cid, 60, 60, 0)
        self.store.control(self.cid, 'activate', NOW)
        start = datetime(2026, 10, 28, 9, tzinfo=TZ).timestamp()
        self.assertIsNone(self.store.claim(start-1))
        for minute in range(660):
            at = start + minute*60
            if minute == 300:
                self.store = CampaignStore(self.store.path)
                self.store.recover()
                self.store.control(self.cid, 'activate', at)
            entry = self.store.claim(at)
            self.assertIsNotNone(entry, minute)
            self.store.result(self.cid, entry['phone'], 'accepted', 'sent-'+str(minute), now=at)
            if minute in (0, 299, 300, 659):
                self.assertIsNone(self.store.claim(at+59))
        self.assertEqual(self.store.report(self.cid)['counts'], {'accepted': 660, 'pending': 1})
        self.assertIsNone(self.store.claim(start + 11*3600))
        self.assertFalse(self.store.eligible(self.cid, entry['phone'], start + 11*3600))
        self.assertIsNotNone(self.store.claim(start + 86400))
        self.assertRaises(ValueError, self.store.set_limits, self.cid, 60, 60, -1)
        self.assertEqual(self.store.report(self.cid)['daily'], 0)

    def test_complaint_detection_preserves_questions_and_partial_course_choices(self):
        self.active()
        self.store.claim(NOW)
        for n, text in enumerate(('Нет опыта', 'Нет, ребёнку 8 лет', 'Дорого. Есть скидки?',
                'Английский не интересует, хочу математику', 'Можно заехать сегодня?',
                'У меня жалоба на преподавателя', 'Где номер управляющего?',
                'Мен мазаламаймын', 'Спасибо, всё понятно')):
            with self.subTest(text=text):
                self.assertFalse(self.store.inbound('77001234567', text, str(n), NOW))
        for n, text in enumerate(('Что за спам?!', 'Вы достали своей рекламой', 'Откуда у вас мой номер?',
                'Я не подписывалась на рассылку', 'Пошли нахуй', 'Спасибо, но не интересно',
                'Мне не нужна ваша реклама', 'Маған қызық емес')):
            with self.subTest(text=text):
                self.assertTrue(self.store.inbound('77001234567', text, 'negative'+str(n), NOW))

    def test_apology_reservation_is_unique_and_expires_without_retry(self):
        phone = '77001234567'
        self.assertFalse(self.store.reserve_refusal_ack(phone, 'id', NOW))
        self.store.suppress(phone, 'refused', NOW)
        self.assertTrue(self.store.reserve_refusal_ack(phone, 'id', NOW))
        reopened = CampaignStore(self.store.path)
        self.assertFalse(reopened.reserve_refusal_ack(phone, 'id2', NOW))
        self.assertTrue(reopened.refusal_ack_allowed(phone, NOW+299))
        self.assertFalse(reopened.refusal_ack_allowed(phone, NOW+300))
        reopened.finish_refusal_ack(phone, 'uncertain')
        self.assertFalse(reopened.refusal_ack_allowed(phone, NOW+1))


class CampaignDialogTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown
    queue = reliability.ReliabilityTests.queue

    def setUp(self):
        reliability.ReliabilityTests.setUp(self)
        self.enterContext(patch.object(bot, 'CAMPAIGN_STATE_FILE', str(Path(self.tmp.name) / 'campaigns.sqlite3')))
        self.enterContext(patch.object(bot, 'CAMPAIGNS_ENABLED', True))
        self.store = CampaignStore(bot.CAMPAIGN_STATE_FILE)
        self.store.create(SPEC, NOW)
        self.cid = SPEC['id']
        self.store.import_rows(self.cid, rows())
        self.store.control(self.cid, 'activate', NOW)

    def accepted(self):
        self.store.claim(NOW)
        self.store.result(self.cid, '77001234567', 'accepted', 'id1', now=NOW)

    async def test_plus_reply_gets_original_campaign_in_ai_context(self):
        self.accepted()
        self.ai.side_effect = [completion('Как зовут ребёнка и сколько ему лет?')]
        self.queue('+ Камал')
        await bot.process_dialog(self.chat)
        messages = self.ai.call_args.kwargs['messages']
        text = '\n'.join(m['content'] for m in messages if isinstance(m, dict) and m.get('content'))
        for expected in ('GM Intellect', '55 000', 'Cambridge', 'не бронь'):
            self.assertIn(expected, text)
        self.assertEqual(bot.conversation_facts[self.chat]['preference'], 'Камал')

    async def test_existing_pupil_keeps_campaign_context_without_new_chess_sales(self):
        self.accepted()
        bot.known_users[self.chat] = {'id': 42, 'phone': '77001234567'}
        bot.crm_lookup_state[self.chat] = {'status': 'found', 'checked_at': bot.time.time()}
        self.ai.side_effect = [completion('По новым направлениям помогу с диагностикой.')]
        self.queue('+')
        await bot.process_dialog(self.chat)
        self.assertIn('GM Intellect', self.ai.call_args.kwargs['messages'][-1]['content'])
        self.assertNotIn('register_client_request', [tool['function']['name'] for tool in self.ai.call_args.kwargs['tools']])

    async def test_semantic_refusal_takes_priority_over_sales_and_sends_only_apology(self):
        self.accepted()
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {}, 'sale'),
                                                tool('decline_campaign', {}, 'refusal')])]
        with patch.object(bot.crm, 'create_lead', AsyncMock()) as lead:
            self.queue('Подобным предложениям не место в моём телефоне')
            await bot.process_dialog(self.chat)
            await asyncio.gather(*list(bot._background_tasks))
        lead.assert_not_awaited()
        self.ai.assert_awaited_once()
        self.send.assert_awaited_once()
        self.assertIn('Извините', self.send.call_args.args[1])
        self.assertIn(self.chat, bot.contact_opt_out)
        self.assertNotIn(self.chat, bot.followups)
        self.assertIn('decline_campaign', [t['function']['name'] for t in self.ai.call_args.kwargs['tools']])
        self.assertFalse(any(isinstance(m, dict) and m.get('tool_calls') for m in bot.chat_history[self.chat]))

    async def test_semantic_refusal_tool_is_not_offered_without_campaign(self):
        self.ai.side_effect = [completion('Подскажите возраст ребёнка.')]
        self.queue('Хочу узнать про математику')
        await bot.process_dialog(self.chat)
        self.assertNotIn('decline_campaign', [t['function']['name'] for t in self.ai.call_args.kwargs['tools']])

    async def test_diagnostic_callback_uses_chosen_branch_preserving_enrollment(self):
        self.accepted()
        bot.known_users[self.chat] = {'id': 42, 'phone': '77001234567'}
        bot.client_dossiers[self.chat] = {'id': 42, 'filial_id': 54648, 'matched_key': 'кадыр', 'name': 'Тест'}
        bot.crm_lookup_state[self.chat] = {'status': 'found', 'checked_at': bot.time.time()}
        bot.conversation_facts[self.chat] = {'courses': ['шахматы'], 'preference': 'Бином Кадыр', 'name': 'Тест', 'age': '9'}
        self.ai.side_effect = [completion(calls=[tool('request_manager_callback', {'reason': 'Диагностика GM Intellect в Камале'})]),
                               completion('Передала запрос управляющему.')]
        with patch.object(bot.crm, 'notify_client_callback_request', AsyncMock(return_value='MGR_PHONE=77001234500')) as callback:
            self.queue('+ Камал')
            await bot.process_dialog(self.chat)
        self.assertEqual(callback.call_args.kwargs['filial_id'], bot._guess_filial_from_text('Камал'))
        self.assertEqual(callback.call_args.kwargs['matched_key'], 'камал')
        self.assertEqual(bot.client_dossiers[self.chat]['filial_id'], 54648)
        self.assertNotIn('courses', bot.conversation_facts[self.chat])

    async def test_ok_accepts_new_invitation_despite_old_handoff(self):
        self.accepted()
        bot.chat_history[self.chat] = [{'role': 'system', 'content': bot.SYSTEM_PROMPT}, {'role': 'assistant', 'content': SPEC['message']}]
        bot.handoff_completed[self.chat] = NOW-86400
        bot.crm_notify_recent[self.chat] = bot.time.time()
        self.ai.side_effect = [completion('В каком филиале удобнее: Аркада или Камал?')]
        self.queue('Хорошо')
        await bot.process_dialog(self.chat)
        self.ai.assert_awaited_once()

    async def test_pause_during_rate_wait_prevents_send(self):
        async def state(request, **kwargs):
            return httpx.Response(200, request=request, json={'stateInstance': 'authorized'})
        async def send(chat, text, **kwargs):
            self.store.control(self.cid, 'pause', NOW)
            self.assertFalse(await kwargs['before_send']())
            return False
        self.send.side_effect = send
        with patch('httpx.AsyncClient.send', side_effect=state), patch.object(bot.time, 'time', return_value=NOW):
            await bot._campaign_tick()
        self.assertEqual(self.store.report(self.cid)['counts'], {'pending': 1})
        self.assertIsNone(self.store.context('77001234567'))

    async def test_provider_events_ignore_malformed_instance_and_pause_real_account(self):
        for data in ({'instanceData': []}, {'instanceData': {'idInstance': 'another'}},
                     {'instanceData': {'idInstance': bot.GREEN_API_ID}}):
            await bot.handle_webhook(SimpleNamespace(json=AsyncMock(return_value={**data, 'typeWebhook': 'outgoingMessageStatus'})))
        self.assertEqual(self.store.report(self.cid)['status'], 'active')
        await bot.handle_webhook(SimpleNamespace(json=AsyncMock(return_value={
            'typeWebhook': 'stateInstanceChanged', 'instanceData': {'idInstance': bot.GREEN_API_ID},
            'stateInstance': 'blocked'})))
        self.assertEqual(self.store.report(self.cid)['status'], 'paused')

    async def test_active_conversation_has_priority_over_campaign(self):
        self.queue('Сколько стоит?')
        with patch.object(bot.time, 'time', return_value=NOW):
            await bot._campaign_tick()
        self.send.assert_not_awaited()
        self.assertEqual(self.store.report(self.cid)['counts'], {'pending': 1})

    async def test_decline_apologizes_once_and_cancels_pending_sales(self):
        self.accepted()
        self.queue('Старый вопрос')
        bot.pending_answers[self.chat] = {'text': 'Старый ответ'}
        await bot._buffer_incoming(self.chat, {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'Нет, спасибо'}})
        await asyncio.gather(*list(bot._background_tasks))
        self.assertFalse(bot.message_buffers)
        self.assertNotIn(self.chat, bot.pending_answers)
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1], 'Извините, что побеспокоили. Больше не будем присылать вам рекламные сообщения.')
        self.ai.assert_not_awaited()
        self.assertIn(self.chat, bot.contact_opt_out)
        self.assertFalse(bot._outbound_allowed(self.chat))
        bot.contact_opt_out.clear()
        await bot._load_conversations()
        self.assertIn(self.chat, bot.contact_opt_out)
        await bot._buffer_incoming(self.chat, {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'Отстаньте'}})
        self.send.assert_awaited_once()

    async def test_duplicate_stop_webhooks_send_only_one_apology_via_real_sender(self):
        self.accepted()
        data = {'typeWebhook': 'incomingMessageReceived', 'idMessage': 'stop-1', 'timestamp': int(bot.time.time()),
                'senderData': {'chatId': self.chat},
                'messageData': {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'СТОП'}}}
        with patch.object(bot, 'send_whatsapp', REAL_SEND), \
                patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)), \
                patch('httpx.AsyncClient.send', AsyncMock(return_value=httpx.Response(200, json={'idMessage': 'ack-1'}))) as http:
            await bot.handle_webhook(SimpleNamespace(json=AsyncMock(return_value=data)))
            await bot.handle_webhook(SimpleNamespace(json=AsyncMock(return_value=data)))
            await asyncio.gather(*list(bot._background_tasks))
            self.assertEqual(http.await_count, 1)
            self.assertFalse(await REAL_SEND(self.chat, 'Обычная реклама'))
            self.assertEqual(http.await_count, 1)
        self.assertFalse(self.store.refusal_ack_allowed('77001234567'))
        self.ai.assert_not_awaited()

    async def test_apology_timeout_never_retries_or_reopens_optout(self):
        self.accepted()
        with patch.object(bot, 'send_whatsapp', REAL_SEND), \
                patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)), \
                patch('httpx.AsyncClient.send', AsyncMock(side_effect=httpx.ReadTimeout('timeout'))) as http:
            bot._queue_incoming(self.chat, {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'Что за спам?!'}}, 'bad-1', None)
            await asyncio.gather(*list(bot._background_tasks))
            bot._queue_incoming(self.chat, {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'Отстаньте'}}, 'bad-2', None)
            await asyncio.gather(*list(bot._background_tasks))
            self.assertEqual(http.await_count, 1)
        self.assertNotIn(self.chat, bot.pending_answers)
        self.assertIn(self.chat, bot.contact_opt_out)
        self.assertEqual(self.store.report(self.cid)['suppressed_contacts'], 1)

    async def test_voice_refusal_uses_kazakh_apology(self):
        self.accepted()
        transcription = SimpleNamespace(text='Маған жазбаңыз', usage=None)
        with patch('httpx.AsyncClient.send', AsyncMock(return_value=httpx.Response(200, content=b'voice', request=httpx.Request('GET', 'https://example.com/voice.ogg')))), \
                patch.object(bot.openai_client.audio.transcriptions, 'create', AsyncMock(return_value=transcription)):
            await bot._buffer_incoming(self.chat, {'typeMessage': 'voiceMessage', 'voiceMessageData': {'downloadUrl': 'https://example.com/voice.ogg'}})
        await asyncio.gather(*list(bot._background_tasks))
        self.send.assert_awaited_once()
        self.assertIn('кешіріңіз', self.send.call_args.args[1])
        self.assertIn(self.chat, bot.contact_opt_out)

    async def test_old_stop_suppresses_without_sending_late_apology(self):
        self.accepted()
        data = {'typeWebhook': 'incomingMessageReceived', 'idMessage': 'old-stop', 'timestamp': 1,
                'senderData': {'chatId': self.chat},
                'messageData': {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'СТОП'}}}
        with patch.object(bot, 'WA_SKIP_OLD_INCOMING', True):
            await bot.handle_webhook(SimpleNamespace(json=AsyncMock(return_value=data)))
        self.assertIn(self.chat, bot.contact_opt_out)
        self.send.assert_not_awaited()

    async def test_apology_rechecks_expiry_after_send_cooldown(self):
        self.accepted()
        current = bot.time.time()
        async def delayed_slot(chat_id):
            with self.store.db() as db:
                db.execute('UPDATE refusal_acks SET created=?', (current-301,))
            return True
        with patch.object(bot, 'send_whatsapp', REAL_SEND), \
                patch.object(bot, '_wa_acquire_send_slot', side_effect=delayed_slot), \
                patch('httpx.AsyncClient.send', AsyncMock()) as http:
            bot._handle_contact_refusal(self.chat, 'СТОП', 'stop-1')
            await asyncio.gather(*list(bot._background_tasks))
        http.assert_not_awaited()
        self.assertIn(self.chat, bot.contact_opt_out)

    async def test_one_minute_campaign_has_room_in_shared_sender_and_keeps_reply_reserve(self):
        self.store.set_limits(self.cid, 60, 60, 0)
        bot._wa_send_times.extend([NOW-7200] * 700)
        bot._wa_send_times.extend([NOW-100] * 59)
        with patch.multiple(bot, WA_MAX_PER_HOUR=120, WA_MAX_PER_DAY=0), \
                patch.object(bot.time, 'time', return_value=NOW), \
                patch('httpx.AsyncClient.send', AsyncMock(return_value=httpx.Response(200,
                    json={'stateInstance': 'authorized'}, request=httpx.Request('GET', 'https://example.com')))):
            await bot._campaign_tick()
            self.send.assert_awaited_once()
            self.send.reset_mock()
            bot._wa_send_times.append(NOW-100)
            await bot._campaign_tick()
            self.send.assert_not_awaited()

    async def test_sending_uses_shared_sender_and_records_exact_message(self):
        async def state(request, **kwargs):
            return httpx.Response(200, request=request, json={'stateInstance': 'authorized'})
        async def send(chat, text, **kwargs):
            self.assertTrue(await kwargs['before_send']())
            kwargs['delivery_result'].update(state='accepted', message_id='provider-id', reason='')
            return True
        self.send.side_effect = send
        with patch('httpx.AsyncClient.send', side_effect=state), patch.object(bot.time, 'time', return_value=NOW):
            await bot._campaign_tick()
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[1], SPEC['message'])
        self.assertEqual(bot.chat_history[self.chat][-1]['content'], SPEC['message'])
        self.assertNotIn(self.chat, bot.followups)
        self.assertEqual(self.store.context('77001234567')['message_id'], 'provider-id')

    async def test_sender_timeout_is_uncertain_not_automatic_retry(self):
        self.enterContext(patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)))
        result = {}
        # Use the real sender while replacing only HTTP.
        with patch('httpx.AsyncClient.send', AsyncMock(side_effect=httpx.ReadTimeout('timeout'))):
            self.assertFalse(await REAL_SEND(self.chat, 'test', delivery_result=result))
        self.assertEqual(result['state'], 'uncertain')


REAL_SEND = bot.send_whatsapp
