"""Offline schedule, routing and restart checks for teacher attendance reminders."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import test_reliability as reliability
from test_reliability import bot
import teacher_reminders as reminders

SEND = bot.send_whatsapp


class TeacherReminderTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown

    def setUp(self):
        reliability.ReliabilityTests.setUp(self)
        self.now = datetime(2026, 11, 3, 13, 5, tzinfo=reminders.TZ)
        self.store = reminders.ReminderStore(Path(self.tmp.name) / 'teachers.sqlite3')
        self.since = self.store.start((self.now-timedelta(hours=1)).timestamp())
        self.lesson = {'id': 42, 'date': '2026-11-03', 'beginTime': '13:00', 'endTime': '14:00',
                       'status': 0, 'teacherIds': [10], 'records': [{'id': 5, 'skip': False, 'visit': False}]}
        self.lessons = [self.lesson]
        self.fresh = deepcopy(self.lesson)
        self.staff = [{'id': 10, 'phone': '77001234567', 'isWork': True}]
        self.enterContext(patch.object(bot.crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'fake'})))
        self.get = self.enterContext(patch('httpx.AsyncClient.get', AsyncMock(side_effect=self.respond)))
        self.dispatch = AsyncMock(side_effect=self.deliver)

    def respond(self, url, **kwargs):
        params = kwargs.get('params', {})
        if url.endswith('/lessons'):
            offset = params.get('offset', 0)
            data = {'lessons': self.lessons[offset:offset+params.get('limit', 200)],
                    'stats': {'totalItems': len(self.lessons)}}
        elif '/lessons/' in url:
            data = self.fresh
        elif url.endswith('/managers'):
            data = self.staff
        else:
            raise AssertionError(url)
        return httpx.Response(200, json=deepcopy(data), request=httpx.Request('GET', url))

    async def deliver(self, phone, guard):
        return {'state': 'accepted', 'message_id': 'reminder-1'} if await guard() else {'state': 'not_sent'}

    async def tick(self):
        return await reminders.reminder_tick(crm=bot.crm, base_url=bot.MOYKLASS_BASE_URL,
            store=self.store, enabled_since=self.since, dispatch=self.dispatch, clock=lambda: self.now)

    async def test_sends_only_at_five_minutes_to_assigned_teacher_once(self):
        self.now -= timedelta(seconds=1)
        await self.tick()
        self.dispatch.assert_not_awaited()
        self.now += timedelta(seconds=1)
        self.assertEqual((await self.tick())['accepted'], 1)
        await self.tick()
        self.dispatch.assert_awaited_once()
        self.assertEqual(self.dispatch.call_args.args[0], '77001234567')

    async def test_dedup_survives_restart(self):
        await self.tick()
        self.store = reminders.ReminderStore(self.store.path)
        self.assertEqual(self.store.start(self.now.timestamp()), self.since)
        await self.tick()
        self.dispatch.assert_awaited_once()

    async def test_uncertain_send_is_not_repeated(self):
        self.dispatch.side_effect = None
        self.dispatch.return_value = {'state': 'uncertain', 'reason': 'transport_ReadTimeout'}
        self.assertEqual((await self.tick())['uncertain'], 1)
        await self.tick()
        self.dispatch.assert_awaited_once()

    async def test_restart_during_send_does_not_repeat(self):
        self.store.claim(42, 10, '77001234567', self.now.timestamp(), self.now.timestamp())
        self.store.start(self.now.timestamp())
        await self.tick()
        self.dispatch.assert_not_awaited()

    async def test_proven_unsent_is_retried_in_window(self):
        self.dispatch.side_effect = [ {'state': 'not_sent'}, {'state': 'accepted', 'message_id': 'retry'} ]
        self.assertEqual((await self.tick())['deferred'], 1)
        self.assertEqual((await self.tick())['accepted'], 1)
        self.assertEqual(self.dispatch.await_count, 2)

    async def test_no_backfill_on_first_enable_or_after_window(self):
        self.since = self.now.timestamp()+1
        await self.tick()
        self.since = 0
        self.now += timedelta(minutes=10)
        await self.tick()
        self.dispatch.assert_not_awaited()

    async def test_completed_empty_and_unknown_lessons_are_skipped(self):
        for change in ({'status': 1}, {'status': None}, {'records': []}, {'records': None},
                       {'records': [{'skip': True}]}, {'date': 'invalid'}, {'teacherIds': []}, {'beginTime': ''}):
            self.lessons = [dict(self.lesson, **change)]
            await self.tick()
        self.dispatch.assert_not_awaited()

    async def test_missing_inactive_or_invalid_teacher_has_no_manager_fallback(self):
        for staff in ([], [{'id': 99, 'phone': '77009999999', 'isWork': True}],
                      [{'id': 10, 'phone': '77001234567', 'isWork': False}],
                      [{'id': 10, 'phone': None, 'isWork': True}]):
            self.staff = staff
            await self.tick()
        self.dispatch.assert_not_awaited()

    async def test_two_assigned_teachers_receive_once_each(self):
        self.lesson['teacherIds'] = self.fresh['teacherIds'] = [10, 11]
        self.staff.append({'id': 11, 'phone': '77009999999', 'isWork': True})
        self.assertEqual((await self.tick())['accepted'], 2)
        self.assertEqual({c.args[0] for c in self.dispatch.call_args_list}, {'77001234567', '77009999999'})

    async def test_duplicate_staff_phone_does_not_get_two_copies(self):
        self.lesson['teacherIds'] = self.fresh['teacherIds'] = [10, 11]
        self.staff.append({'id': 11, 'phone': '+7 700 123 45 67', 'isWork': True})
        self.assertEqual((await self.tick())['accepted'], 1)
        self.dispatch.assert_awaited_once()

    async def test_reschedule_or_completed_after_queue_is_rechecked(self):
        for changes in ({'beginTime': '15:00'}, {'status': 1}, {'teacherIds': [99]}, {'records': []}):
            with self.subTest(changes=changes):
                self.store = reminders.ReminderStore(Path(self.tmp.name) / ('state'+str(len(changes))+str(changes)+'.sqlite3'))
                self.fresh = dict(self.lesson, **changes)
                self.assertEqual((await self.tick())['deferred'], 1)

    async def test_changed_teacher_phone_is_not_sent_to_old_number(self):
        async def changed(phone, guard):
            self.staff[0]['phone'] = '77009999999'
            return await self.deliver(phone, guard)
        self.dispatch.side_effect = changed
        self.assertEqual((await self.tick())['deferred'], 1)

    async def test_queue_delay_cannot_send_expired_reminder(self):
        async def delayed(phone, guard):
            self.now += timedelta(minutes=20)
            return await self.deliver(phone, guard)
        self.dispatch.side_effect = delayed
        self.assertEqual((await self.tick())['deferred'], 1)

    async def test_unsent_rescheduled_lesson_can_remind_at_new_start(self):
        self.fresh['beginTime'] = '15:00'
        self.assertEqual((await self.tick())['deferred'], 1)
        self.lesson['beginTime'] = '15:00'
        self.now = self.now.replace(hour=15)
        self.assertEqual((await self.tick())['accepted'], 1)

    async def test_midnight_uses_almaty_and_previous_day(self):
        self.now = datetime(2026, 11, 3, 0, 3, tzinfo=reminders.TZ)
        self.since = (self.now-timedelta(hours=1)).timestamp()
        self.lesson.update(date='2026-11-02', beginTime='23:58', endTime='00:58')
        self.fresh = deepcopy(self.lesson)
        self.assertEqual((await self.tick())['accepted'], 1)
        query = self.get.call_args_list[0].kwargs['params']
        self.assertEqual(query['date'], '2026-11-02,2026-11-03')

    async def test_lesson_on_second_page_is_not_lost(self):
        self.lessons = [dict(self.lesson, id=1000+i, status=1) for i in range(200)] + [self.lesson]
        self.assertEqual((await self.tick())['accepted'], 1)
        pages = [c.kwargs['params']['offset'] for c in self.get.call_args_list if c.args[0].endswith('/lessons')]
        self.assertEqual(pages, [0, 200])

    async def test_crm_failure_never_sends_without_teacher_verification(self):
        self.get.side_effect = httpx.ConnectTimeout('offline')
        with self.assertRaises(httpx.ConnectTimeout):
            await self.tick()
        self.dispatch.assert_not_awaited()

    async def test_second_crm_check_failure_is_deferred(self):
        normal = self.respond
        def error_on_detail(url, **kwargs):
            if '/lessons/' in url:
                raise httpx.ConnectTimeout('offline')
            return normal(url, **kwargs)
        self.get.side_effect = error_on_detail
        self.assertEqual((await self.tick())['deferred'], 1)
        self.get.side_effect = normal
        self.assertEqual((await self.tick())['accepted'], 1)

    async def test_exact_text_and_test_recipient_go_through_normal_send_limiter(self):
        response = httpx.Response(200, json={'idMessage': 'test-send'})
        with patch.object(bot, 'send_whatsapp', SEND), \
             patch.object(bot, 'MOYKLASS_WEBHOOK_TEST_PHONE', '77009999999'), \
             patch.object(bot, '_wa_acquire_send_slot', AsyncMock(return_value=True)) as quota, \
             patch('httpx.AsyncClient.post', AsyncMock(return_value=response)) as post:
            guard = AsyncMock(return_value=True)
            result = await bot._dispatch_teacher_attendance('77001234567', guard)
        self.assertEqual(result['state'], 'accepted')
        self.assertEqual(post.call_args.kwargs['json'], {'chatId': '77009999999@c.us',
                                                       'message': 'Важно! Отметьте посещение учеников'})
        quota.assert_awaited_once()
        guard.assert_awaited_once()
        self.assertIn('77001234567@c.us', bot.internal_chats)

    async def test_contact_refusal_is_honored(self):
        bot.contact_opt_out.add('77001234567@c.us')
        result = await bot._dispatch_teacher_attendance('77001234567', AsyncMock())
        self.assertEqual(result['state'], 'skipped')
        self.send.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
