"""Debt notification regressions. All CRM/WhatsApp traffic is mocked."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import test_reliability as reliability
from test_reliability import bot, attendance

SEND = bot.send_whatsapp


class DebtNotificationTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown

    def setUp(self):
        reliability.ReliabilityTests.setUp(self)
        self.now = datetime(2026, 9, 28, 13, 57, tzinfo=bot._SCHOOL_TZ)
        dt = self.enterContext(patch.object(attendance, 'datetime', wraps=datetime))
        dt.now.return_value = self.now
        self.enterContext(patch.object(bot.crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'test'})))
        self.enterContext(patch.object(bot, '_webhook_test_mode_active', return_value=False))
        self.record = {
            'id': 100, 'userId': 42, 'lessonId': 10, 'visit': True,
            'paid': True, 'free': False, 'skip': False, 'bill': None,
            'lesson': {'id': 10, 'classId': 20, 'date': '2026-09-28', 'beginTime': '13:10', 'status': 1},
        }
        self.join = {'id': 1, 'userId': 42, 'classId': 20, 'stats': {'nonPayedLessons': 1}}
        self.user = {'id': 42, 'availableBalance': 0}
        self.subscriptions = []
        self.get = self.enterContext(patch('httpx.AsyncClient.get', AsyncMock(side_effect=self.respond)))

    def respond(self, url, **kwargs):
        path = url.rsplit('/', 1)[-1]
        params = kwargs.get('params', {})
        if path == 'users' or path == '42':
            data = self.user
        else:
            key, rows = {
                'lessonRecords': ('lessonRecords', [self.record]),
                'joins': ('joins', [self.join]),
                'userSubscriptions': ('subscriptions', self.subscriptions),
            }[path]
            offset, limit = params.get('offset', 0), params.get('limit', 200)
            data = {key: rows[offset:offset + limit], 'stats': {'totalItems': len(rows)}}
        return httpx.Response(200, json=deepcopy(data), request=httpx.Request('GET', url))

    def subscription(self, **overrides):
        return {
            'id': 8, 'userId': 42, 'subscriptionId': 93630, 'statusId': 2,
            'price': 12000, 'payed': 12000, 'beginDate': '2026-09-01', 'endDate': '2026-09-30',
            'classIds': [20], 'mainClassId': 20, 'courseIds': [], 'visitCount': 6,
            'stats': {'totalPayed': 12000, 'totalVisited': 2, 'totalBurned': 0}, **overrides,
        }

    async def check(self, user_id=42, lesson_id=10):
        return await attendance.has_confirmed_lesson_debt(bot.crm, bot.MOYKLASS_BASE_URL, user_id, lesson_id)

    async def dispatch(self, source='attendance-monitor'):
        return await bot._dispatch_moyklass_webhook(
            source, 'sub_lesson_in_debt', {'userId': 42, 'lessonId': 10},
            'Занятие проведено в долг.', ['77001234567'], True,
        )

    async def tick(self):
        async def dispatch(**kw):
            return await bot._dispatch_moyklass_webhook('attendance-monitor', record_history=True, **kw)
        with patch.object(attendance, 'fetch_lessons_for_day', AsyncMock(return_value=[self.record['lesson']])), \
             patch.object(attendance, 'fetch_lesson_records', AsyncMock(return_value=[self.record])), \
             patch.object(attendance, 'count_visits_this_month', AsyncMock(return_value=2)), \
             patch.multiple(attendance, ATTENDANCE_DRY_RUN=False, ATTENDANCE_SEED_ONLY=False,
                            MONTHLY_DEBT_VISIT_N=2, ATTENDANCE_MAX_SEND_PER_TICK=5):
            return await attendance.attendance_tick(
                crm=bot.crm, base_url=bot.MOYKLASS_BASE_URL, dispatch_client_message=dispatch,
                get_trainer_phone=AsyncMock(return_value=''), get_admin_phone=AsyncMock(return_value=''),
                user_is_mailing_client=AsyncMock(return_value=True),
                get_user_phone=AsyncMock(return_value='77001234567'),
            )

    async def test_incident_paid_second_visit_is_not_a_debt(self):
        # Same financial facts as the incident: 12,000 paid, lesson charged to
        # that subscription, zero balance and zero unbilled visits.
        self.record['bill'] = {'id': 99, 'userSubscriptionId': 8, 'summa': 2000}
        self.subscriptions = [self.subscription()]
        self.join['stats']['nonPayedLessons'] = 0
        self.assertFalse(await self.check())
        for source in ('attendance-monitor', 'moyklass-webhook'):
            self.assertEqual((await self.dispatch(source))['delivered'], 0)
        self.assertEqual((await self.tick())['debt_sent'], 0)
        self.send.assert_not_awaited()
        self.assertFalse(attendance._debt_sent)
        self.assertFalse(bot._crm_sent_keys)

    async def test_confirmed_unbilled_chargeable_visit_can_send_once(self):
        self.assertTrue(await self.check())
        self.assertEqual((await self.dispatch())['delivered'], 1)
        self.assertEqual((await self.dispatch('moyklass-webhook'))['deduplicated'], 1)
        self.send.assert_awaited_once()

    async def test_paid_flag_alone_never_proves_payment_or_debt(self):
        self.assertTrue(await self.check())  # paid=True means chargeable, with no bill.
        for value in (False, None, 'true', 1):
            with self.subTest(value=value):
                self.record['paid'] = value
                self.assertFalse(await self.check())

    async def test_free_unvisited_skipped_or_unknown_records_never_send(self):
        original = deepcopy(self.record)
        for field, value in [('free', True), ('visit', False), ('skip', True), ('bill', {}), ('bill', [])]:
            with self.subTest(field=field):
                self.record = {**original, field: value}
                self.assertFalse(await self.check())
        for field in ('free', 'visit', 'skip', 'bill', 'paid', 'userId', 'lessonId', 'lesson'):
            with self.subTest(missing=field):
                self.record = deepcopy(original)
                del self.record[field]
                self.assertFalse(await self.check())

    async def test_future_unheld_or_wrong_lesson_never_sends(self):
        original = deepcopy(self.record['lesson'])
        for field, value in [('date', '2026-09-30'), ('beginTime', '23:00'), ('beginTime', None), ('status', 0),
                             ('date', 'invalid'), ('id', 99), ('classId', None)]:
            with self.subTest(field=field, value=value):
                self.record['lesson'] = {**original, field: value}
                self.assertFalse(await self.check())

    async def test_debt_in_another_class_does_not_trigger_this_lesson(self):
        for change in ({'classId': 99}, {'userId': 99}, {'stats': {'nonPayedLessons': 0}}, {'stats': {}}):
            with self.subTest(change=change):
                self.join = {'id': 1, 'userId': 42, 'classId': 20, 'stats': {'nonPayedLessons': 1}, **change}
                self.assertFalse(await self.check())

    async def test_missing_or_invalid_ids_never_query_crm(self):
        for value in (None, '', 0, -1, True, 'bad', 42.5):
            self.assertFalse(await self.check(user_id=value))
            self.assertFalse(await self.check(lesson_id=value))
        self.get.assert_not_awaited()

    async def test_crm_errors_and_partial_responses_fail_closed(self):
        for failure in (httpx.ReadTimeout('timeout'), ValueError('bad JSON')):
            self.get.side_effect = failure
            self.assertFalse(await self.check())
        for status, data in [(401, {}), (429, {}), (500, {}), (200, {}), (200, []),
                             (200, {'lessonRecords': [self.record], 'stats': {'totalItems': 2}}),
                             (200, {'lessonRecords': [], 'stats': {'totalItems': 0}}),
                             (200, {'lessonRecords': [self.record, self.record], 'stats': {'totalItems': 2}})]:
            with self.subTest(status=status, data=data):
                self.get.side_effect = None
                self.get.return_value = httpx.Response(status, json=data, request=httpx.Request('GET', 'https://test'))
                self.assertFalse(await self.check())

    async def test_missing_balance_and_unallocated_payment_never_send(self):
        for value in (12000, '2000.50', None, True, 'NaN', 'Infinity', 'bad'):
            self.user['availableBalance'] = value
            self.assertFalse(await self.check())
        del self.user['availableBalance']
        self.assertFalse(await self.check())

    async def test_paid_subscription_awaiting_allocation_suppresses_debt(self):
        self.subscriptions = [self.subscription()]
        self.assertFalse(await self.check())
        # The API sometimes returns payed=0 alongside stats.totalPayed=12000.
        self.subscriptions[0]['payed'] = 0
        self.assertFalse(await self.check())

    async def test_free_qosymsha_and_family_subscription_suppress_debt(self):
        for sub in (self.subscription(price=0, payed=0), self.subscription(subscriptionId=132913),
                    self.subscription(userId=99), self.subscription(classIds=[], courseIds=[7])):
            self.subscriptions = [sub]
            self.assertFalse(await self.check())

    async def test_unrelated_expired_or_exhausted_subscription_is_not_coverage(self):
        for sub in (self.subscription(classIds=[99], mainClassId=99),
                    self.subscription(endDate='2026-08-31'), self.subscription(statusId=4),
                    self.subscription(visitCount=2), self.subscription(beginDate='2026-10-01')):
            with self.subTest(sub=sub):
                self.subscriptions = [sub]
                self.assertTrue(await self.check())

    async def test_paid_subscription_on_later_page_is_not_missed(self):
        self.subscriptions = [self.subscription(id=i + 100, statusId=4) for i in range(200)] + [self.subscription()]
        self.assertFalse(await self.check())
        self.assertTrue(any(call.kwargs.get('params', {}).get('offset') == 200 for call in self.get.call_args_list))

    async def test_failure_in_each_financial_lookup_suppresses_send(self):
        for endpoint in ('joins', '42', 'userSubscriptions'):
            def respond(url, **kwargs):
                if url.rsplit('/', 1)[-1] == endpoint:
                    return httpx.Response(503, request=httpx.Request('GET', url))
                return self.respond(url, **kwargs)
            self.get.side_effect = respond
            self.assertFalse(await self.check())

    async def test_payment_while_waiting_for_send_slot_cancels_message(self):
        async def wait_for_slot(_):
            self.record['bill'] = {'id': 99, 'userSubscriptionId': 8}
            return True
        with patch.object(bot, 'send_whatsapp', SEND), \
             patch.object(bot, '_wa_acquire_send_slot', AsyncMock(side_effect=wait_for_slot)), \
             patch('httpx.AsyncClient.post', AsyncMock()) as post:
            result = await self.dispatch()
        self.assertEqual(result['delivered'], 0)
        post.assert_not_awaited()
        self.assertFalse(bot._crm_sent_keys)
        self.assertNotIn(self.chat, bot.chat_history)

    async def test_confirmed_debt_rechecked_after_slot_and_delivered(self):
        response = httpx.Response(200, json={'idMessage': 'offline-message'})
        with patch.object(bot, 'send_whatsapp', SEND), \
             patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)), \
             patch('httpx.AsyncClient.post', AsyncMock(return_value=response)) as post:
            self.assertEqual((await self.dispatch())['delivered'], 1)
            self.assertEqual((await self.dispatch())['deduplicated'], 1)
        post.assert_awaited_once()
        checks = [c for c in self.get.call_args_list if c.args[0].endswith('/lessonRecords')]
        self.assertEqual(len(checks), 2)

    async def test_unknown_debt_can_retry_without_poisoning_monthly_state(self):
        self.join['stats']['nonPayedLessons'] = 0
        self.assertEqual((await self.tick())['debt_sent'], 0)
        self.assertFalse(attendance._debt_sent)
        self.join['stats']['nonPayedLessons'] = 1
        self.assertEqual((await self.tick())['debt_sent'], 1)
        self.assertEqual((await self.tick())['debt_sent'], 0)
        self.send.assert_awaited_once()

    async def test_client_webhook_uses_shared_guard(self):
        request = SimpleNamespace(json=AsyncMock(return_value={
            'event': 'sub_lesson_in_debt', 'object': {'userId': 42, 'lessonId': 10, 'admin_phone': '+7700'},
        }))
        self.record['bill'] = {'id': 99}
        with patch.object(bot, 'MOYKLASS_WEBHOOK_SECRET', 'test-secret'), \
             patch.object(bot, 'MOYKLASS_CLIENT_WEBHOOKS_ENABLED', True), \
             patch.object(bot, '_should_skip_stale_client_webhook', return_value=False), \
             patch.object(bot.crm, 'enrich_webhook_object_context', AsyncMock(side_effect=lambda obj: obj)), \
             patch.object(bot.crm, 'resolve_phones_for_webhook', AsyncMock(return_value=['77001234567'])):
            result = await bot.handle_moyklass_webhook('test-secret', request)
        self.assertEqual(result['delivered'], 0)
        self.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
