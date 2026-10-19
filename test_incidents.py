"""Screenshot regressions and accounting/report delivery checks; no network."""
import asyncio
import httpx
from datetime import date, datetime, timedelta
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_reliability as reliability
from test_reliability import bot, completion, tool
from conversation_memory import update_facts, STOP_CONTACT
from dialog_policy import detect_language, closure_reason, polish_kazakh, reports_existing_student
from usage_reporting import UsageLedger, TZ


def metered(identity='response1', model='gpt-4.1-mini', prompt=1000, cached=400, output=100):
    r = completion('Ответ на вопрос.')
    r.id, r.model = identity, model
    r.usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=output,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=cached))
    return r


class IncidentTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    setUp = reliability.ReliabilityTests.setUp
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown
    queue = reliability.ReliabilityTests.queue

    async def test_ill_reply_is_not_unsubscribe(self):
        for text in ('Я посоветуюсь и отпишусь', 'Завтра отпишусь', 'Мы отпишемся позже'):
            self.assertIsNone(STOP_CONTACT.search(text))
        for text in ('Отпишите меня', 'Отпиши от рассылки'):
            self.assertIsNotNone(STOP_CONTACT.search(text))

    async def test_kazakh_written_request_does_not_disable_dialog(self):
        for text in ('Жазбаша айтып беріңізші', 'Хабарласпаймын, өзім жазамын', 'Мен мазаламаймын'):
            self.assertIsNone(STOP_CONTACT.search(text))
        for text in ('Маған жазбаңыз', 'Хабарласпаңыз', 'Мазаламаңыз', 'Жазба', 'Жазбаңдар'):
            self.assertIsNotNone(STOP_CONTACT.search(text))

    async def test_upgrade_corrects_previously_misparsed_fractional_age(self):
        bot.chat_history[self.chat] = [{'role': 'user', 'content': '4,5 года'}]
        bot.conversation_facts[self.chat] = {'age': '5'}
        bot._save_conversation(self.chat)
        await bot._load_conversations()
        self.assertEqual(bot.conversation_facts[self.chat]['age'], '4,5')
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))

    async def test_kazakh_screenshot_preserves_language_age_branch_and_name(self):
        facts = {}
        for text, question in [('Балам', ''), ('9', 'Балаңыз неше жаста?'),
                               ('иа', 'Балаңыз бұрын шахматпен айналысқан ба?'),
                               ('сред', 'Шахматтағы деңгейі қандай?'), ('Камал', 'Қай филиал ыңғайлы?'),
                               ('Айторе', 'Балаңыздың аты кім?')]:
            update_facts(facts, text, last_question=question)
        self.assertEqual(facts['language'], 'kk')
        self.assertEqual(facts['age'], '9')
        self.assertEqual(facts['preference'], 'Камал')
        self.assertEqual(facts['name'], 'Айторе')
        self.assertEqual(detect_language('Напишите по-русски, пожалуйста', 'kk'), 'ru')
        self.assertEqual(polish_kazakh('Рахмет, заявканы оформдадым. Жақсы күн тілеймін.'),
                         'Рахмет, өтінішіңізді рәсімдедім. Күніңіз жақсы өтсін.')

    async def test_kazakh_confirmed_handoff_answers_visit_without_second_paid_call(self):
        bot.conversation_facts[self.chat] = {'language': 'kk', 'age': '9', 'preference': 'Камал'}
        bot.chat_history[self.chat] = [{'role': 'assistant', 'content': 'Балаңыздың аты кім?'}]
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {
            'client_name': 'Адай', 'client_age': '9', 'preference': 'Камал'})])]
        with patch.object(bot.crm, 'create_lead', AsyncMock(return_value=bot.crm._handoff_message(
                'Шолпан Жолдыбаевна', '+7 778 104 8127', success=True))):
            self.queue('Адай. Қазір баруға бола ма?')
            await bot.process_dialog(self.chat)
        self.ai.assert_awaited_once()
        answer = self.send.call_args.args[1]
        for expected in ('өтінішіңізді рәсімдедім', 'уақытты', '+7 778 104 8127'):
            self.assertIn(expected, answer.lower())
        self.assertNotIn('оформ', answer)

    async def test_under_five_no_lead_no_ai_no_followup_even_after_restart(self):
        for age in ('4,5 года', '4.5 года', '4 жас'):
            self.queue(age)
            await bot.process_dialog(self.chat)
            self.assertIn(self.chat, bot.followup_blocked)
            self.assertTrue(bot._chat_looks_followup_closed(self.chat))
        self.ai.assert_not_awaited()
        bot.followup_blocked.clear()
        await bot._load_conversations()
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))
        self.assertFalse(await bot._send_followup_message(self.chat))
        self.ai.side_effect = [completion('Индивидуальные занятия для взрослых — от 7 000 тг за час.')]
        self.queue('А сколько стоят занятия для меня?')
        await bot.process_dialog(self.chat)
        self.assertIn('7 000', self.send.call_args.args[1])

    async def test_employment_blocks_followup_but_answers_question(self):
        self.ai.side_effect = [completion('По трудоустройству: Еркежан Маратовна — +7 702 561 3672.')]
        self.queue('Здравствуйте. Вам нужны тренеры по шахматам?')
        await bot.process_dialog(self.chat)
        self.send.assert_awaited_once()
        self.assertIn(self.chat, bot.followup_blocked)
        self.assertFalse(await bot._send_followup_message(self.chat))

    async def test_historical_bot_refusal_beyond_last_eight_messages_is_closed(self):
        bot.chat_history[self.chat] = [{'role': 'assistant', 'content':
            'К сожалению, минимальный возраст для записи — 5 лет.'}] + [
            {'role': 'user', 'content': 'Спасибо'} for _ in range(15)]
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))
        bot._save_conversation(self.chat)
        bot.chat_history.clear()
        await bot._load_conversations()
        self.assertIn(self.chat, bot.followup_blocked)

    async def test_other_clubs_request_cannot_recommend_competitors(self):
        self.queue('А где в Астане можно сходить в любое время, поиграть?')
        await bot.process_dialog(self.chat)
        self.ai.assert_not_awaited()
        self.assertIn('GMCA', self.send.call_args.args[1])
        self.assertNotIn('Астана', self.send.call_args.args[1])
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))

    async def test_competitor_in_model_output_is_replaced(self):
        self.ai.side_effect = [completion('Рекомендую Шахматный клуб "Астана" и Центр шахматного образования.')]
        self.queue('Что посоветуете?')
        await bot.process_dialog(self.chat)
        self.assertNotIn('Центр шахматного', self.send.call_args.args[1])
        self.assertIn('GMCA', self.send.call_args.args[1])

    async def test_name_then_price_and_defer_is_answered_not_registered(self):
        bot.chat_history[self.chat] = [{'role': 'assistant', 'content': 'Как зовут вашего ребёнка?'}]
        bot.conversation_facts[self.chat] = {'age': '9', 'preference': 'Камал', 'experience': 'играет дома'}
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {
            'client_name': 'Максат', 'client_age': '9', 'preference': 'Камал'})]),
            completion('В GMCA Камал групповые занятия стоят 30 000 тг в месяц: 3 раза в неделю по часу. Будем рады вашему сообщению.')]
        bot.message_buffers[self.chat] = {'messages': ['Максат', 'По ценам напишите пожалуйста точно. Я посоветуюсь и отпишусь']}
        with patch.object(bot.crm, 'create_lead', AsyncMock()) as create:
            await bot.process_dialog(self.chat)
            create.assert_not_awaited()
        self.assertIn('30 000', self.send.call_args.args[1])
        self.ai.assert_not_awaited()
        self.assertEqual(bot.conversation_facts[self.chat]['name'], 'Максат')
        self.assertIn(self.chat, bot.followup_blocked)
        self.assertNotIn(self.chat, bot.pending_answers)

    async def test_failed_reply_survives_restart_and_retries_without_ai(self):
        self.ai.side_effect = [metered()]
        self.send.side_effect = [False, True]
        self.queue('Сколько стоят занятия?')
        await bot.process_dialog(self.chat)
        self.assertIn(self.chat, bot.pending_answers)
        bot.pending_answers.clear()
        await bot._load_conversations()
        bot.pending_answers[self.chat]['retry_at'] = 0
        await bot._retry_pending_answers()
        self.assertNotIn(self.chat, bot.pending_answers)
        self.assertEqual(self.ai.await_count, 1)
        self.assertEqual(self.send.await_count, 2)
        self.assertEqual(self.send.call_args_list[0].args, self.send.call_args_list[1].args)

    async def followup_at(self, when):
        with patch.object(bot.time, 'time', return_value=when.timestamp()), \
             patch.object(bot, 'datetime', wraps=datetime) as dt:
            dt.now.return_value = when
            await bot._followup_tick()

    async def test_2gis_delayed_reply_then_restart_keeps_one_reminder(self):
        morning = datetime(2026, 10, 12, 7, 50, tzinfo=bot._SCHOOL_TZ)
        delivered = morning.replace(hour=17, minute=57)
        self.ai.side_effect = [completion('Здравствуйте. Для кого рассматриваете обучение — для ребёнка или для себя?')]
        self.send.return_value = False
        with patch.object(bot.time, 'time', return_value=morning.timestamp()):
            self.queue('Здравствуйте! Пишу из приложения 2ГИС.')
            await bot.process_dialog(self.chat)
        self.assertIsNone(bot.followups[self.chat]['last_reply_ts'])
        await self.followup_at(morning + timedelta(hours=2))
        self.assertEqual(self.send.await_count, 1)

        bot.followups.clear()
        bot.pending_answers.clear()
        await bot._load_conversations()
        self.assertIn(self.chat, bot.followups)
        self.send.return_value = True
        with patch.object(bot.time, 'time', return_value=delivered.timestamp()):
            await bot._retry_pending_answers()
        self.assertEqual(bot.followups[self.chat]['last_reply_ts'], delivered.timestamp())
        bot.followups.clear()
        await bot._load_conversations()
        await self.followup_at(delivered + timedelta(hours=2, seconds=-1))
        self.assertEqual(self.send.await_count, 2)
        await self.followup_at(delivered + timedelta(hours=2))
        self.assertEqual(self.send.await_count, 3)
        self.assertEqual(self.send.call_args.args[1], bot.FOLLOWUP_MESSAGE)
        self.assertNotIn(self.chat, bot.followups)

        bot.followup_sent.clear()
        bot.followup_blocked.clear()
        await bot._load_conversations()
        await self.followup_at(delivered + timedelta(days=1))
        self.assertEqual(self.send.await_count, 3)
        self.ai.assert_awaited_once()

    async def test_failed_reminder_remains_due_after_restart(self):
        now = datetime(2026, 10, 12, 15, tzinfo=bot._SCHOOL_TZ)
        bot.chat_history[self.chat] = [{'role': 'user', 'content': 'Цена?'}]
        bot.followups[self.chat] = {'last_client_ts': now.timestamp() - 7300,
                                   'last_reply_ts': now.timestamp() - 7200, 'stage': 0}
        bot._save_conversation(self.chat)
        self.send.return_value = False
        await self.followup_at(now)
        bot.followups.clear()
        await bot._load_conversations()
        self.send.return_value = True
        await self.followup_at(now + timedelta(minutes=5))
        self.assertEqual(self.send.await_count, 2)
        self.assertIn(self.chat, bot.followup_sent)

    async def test_restored_reminder_respects_quiet_hours_and_disabled_worker(self):
        now = datetime(2026, 10, 12, 21, tzinfo=bot._SCHOOL_TZ)
        bot.chat_history[self.chat] = [{'role': 'user', 'content': 'Цена?'}]
        bot.followups[self.chat] = {'last_client_ts': now.timestamp() - 7300,
                                   'last_reply_ts': now.timestamp() - 7200, 'stage': 0}
        bot._save_conversation(self.chat)
        bot.followups.clear()
        await bot._load_conversations()
        await self.followup_at(now)
        self.send.assert_not_awaited()
        with patch.object(bot, 'FOLLOWUP_ENABLED', False):
            await self.followup_at(now + timedelta(hours=12))
        self.send.assert_not_awaited()
        await self.followup_at(now + timedelta(hours=12))
        self.send.assert_awaited_once()

    async def test_closed_or_historical_chat_is_not_enrolled_on_restart(self):
        bot.chat_history[self.chat] = [
            {'role': 'user', 'content': 'Здравствуйте! Пишу из приложения 2ГИС.'},
            {'role': 'assistant', 'content': 'Для кого рассматриваете обучение?'},
        ]
        bot.last_activity[self.chat] = time.time() - 86400
        bot._save_conversation(self.chat)
        await bot._load_conversations()
        self.assertNotIn(self.chat, bot.followups)
        bot._update_followup_tracking(self.chat, 'Здравствуйте')
        bot.chat_history[self.chat].append({'role': 'user', 'content': 'Посоветуюсь и отпишусь'})
        bot._save_conversation(self.chat)
        bot.followups.clear()
        await bot._load_conversations()
        self.assertNotIn(self.chat, bot.followups)
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))

    async def test_legacy_pending_answer_delivery_starts_reminder(self):
        bot.chat_history[self.chat] = [{'role': 'user', 'content': 'Здравствуйте! Пишу из приложения 2ГИС.'}]
        now = datetime(2026, 10, 12, 17, 57, tzinfo=bot._SCHOOL_TZ)
        bot.pending_answers[self.chat] = {'text': 'Для кого рассматриваете обучение?',
            'created': now.timestamp() - 3600, 'retry_at': now.timestamp() - 1, 'attempts': 1}
        bot._save_conversation(self.chat)
        bot.pending_answers.clear()
        await bot._load_conversations()
        self.assertNotIn(self.chat, bot.followups)
        with patch.object(bot.time, 'time', return_value=now.timestamp()):
            await bot._retry_pending_answers()
        self.assertEqual(bot.followups[self.chat]['last_reply_ts'], now.timestamp())
        self.send.assert_awaited_once()

    async def test_reminder_waiting_for_dialog_rechecks_changed_timer(self):
        now = datetime(2026, 10, 12, 15, tzinfo=bot._SCHOOL_TZ)
        bot.chat_history[self.chat] = [{'role': 'user', 'content': 'Цена?'}]
        bot.followups[self.chat] = {'last_client_ts': now.timestamp() - 7300,
                                   'last_reply_ts': now.timestamp() - 7200, 'stage': 0}
        async with bot._dialog_lock(self.chat):
            tick = asyncio.create_task(self.followup_at(now))
            await asyncio.sleep(0)
            bot._update_followup_tracking(self.chat, 'Для ребёнка')
            await bot._deliver_dialog_answer(self.chat, 'Сколько лет ребёнку?')
        await tick
        self.send.assert_awaited_once()
        self.assertNotIn(self.chat, bot.followup_sent)

    async def test_new_message_and_optout_cancel_pending_reply(self):
        self.send.return_value = False
        await bot._deliver_dialog_answer(self.chat, 'Старый ответ')
        await bot._buffer_incoming(self.chat, {'typeMessage': 'textMessage', 'textMessageData': {'textMessage': 'Другой вопрос'}})
        self.assertNotIn(self.chat, bot.pending_answers)
        await bot._deliver_dialog_answer(self.chat, 'Другой ответ')
        bot._stop_contact(self.chat)
        self.assertNotIn(self.chat, bot.pending_answers)
        await bot._retry_pending_answers()
        self.assertEqual(self.send.await_count, 2)

    async def test_api_usage_recorded_even_when_new_input_discards_answer(self):
        async def generated(**kwargs):
            bot.incoming_versions[self.chat] += 1
            return metered()
        self.ai.side_effect = generated
        self.queue('Подскажите цену')
        await bot.process_dialog(self.chat)
        self.send.assert_not_awaited()
        self.assertEqual(bot._ledger().summary(datetime.now(TZ).date())['calls'], 1)

    async def test_both_model_calls_and_errors_are_metered(self):
        self.ai.side_effect = [metered('a'), metered('b'), RuntimeError('timeout')]
        for op in ('dialog', 'tool_result'):
            await bot._chat_completion(self.chat, op, messages=[])
        with self.assertRaises(RuntimeError):
            await bot._chat_completion(self.chat, 'dialog', messages=[])
        s = bot._ledger().summary(datetime.now(TZ).date())
        self.assertEqual((s['calls'], s['errors'], s['unknown']), (3, 1, 1))

    async def test_report_sends_to_director_once_and_not_before_nine(self):
        yesterday = datetime.now(TZ).date() - timedelta(days=1)
        ledger = bot._ledger()
        with ledger.db() as db:
            db.execute("UPDATE metadata SET value=? WHERE key='started_at'",
                       (str(datetime.combine(yesterday, datetime.min.time(), TZ).timestamp()),))
        today = yesterday + timedelta(days=1)
        with patch.object(bot, 'DAILY_REPORT_ENABLED', True), patch.object(bot, 'DAILY_REPORT_HOUR', 9):
            await bot._daily_report_tick(datetime(today.year, today.month, today.day, 8, 59, tzinfo=TZ))
            self.send.assert_not_awaited()
            await asyncio.gather(*(bot._daily_report_tick(datetime(today.year, today.month, today.day, 9, tzinfo=TZ)) for _ in range(2)))
            await bot._daily_report_tick(datetime(today.year, today.month, today.day, 10, tzinfo=TZ))
        self.send.assert_awaited_once()
        self.assertEqual(self.send.call_args.args[0], '77779559999@c.us')
        self.assertFalse(self.send.call_args.kwargs['sanitize'])

    async def test_report_failure_retries_and_director_never_gets_sales(self):
        self.assertTrue(bot._is_internal_chat('77779559999@c.us'))
        day = datetime.now(TZ).date() - timedelta(days=1)
        with bot._ledger().db() as db:
            db.execute("UPDATE metadata SET value=? WHERE key='started_at'", (str(time.time() - 86400),))
        self.send.side_effect = [False, True]
        now = datetime.combine(day + timedelta(days=1), datetime.min.time(), TZ) + timedelta(hours=10)
        with patch.object(bot, 'DAILY_REPORT_ENABLED', True):
            await bot._daily_report_tick(now)
            await bot._daily_report_tick(now)
            await bot._daily_report_tick(now)
        self.assertEqual(self.send.await_count, 2)


class StudentSupportTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    setUp = reliability.ReliabilityTests.setUp
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown
    queue = reliability.ReliabilityTests.queue

    def remember_student(self):
        bot.known_users[self.chat] = {'id': 42, 'name': 'Тест Ученик', 'phone': '77001234567', 'clientStateId': 5}
        bot.client_dossiers[self.chat] = {'id': 42, 'name': 'Тест Ученик', 'filial_id': 42763,
                                        'schedule': 'Устаревшее расписание', 'balance': 12345}
        bot.crm_lookup_state[self.chat] = {'status': 'found', 'checked_at': time.time() - bot.SESSION_TIMEOUT - 1}

    def test_self_report_does_not_match_prospective_or_former_students(self):
        for text in ('Наш ребенок обучается у Вас', 'Мы у вас учимся', 'Мы ходим к вам',
                     'Ребёнок занимается у вас', 'Мы ваши ученики', 'Балам сізде оқиды', 'Біз сіздерде оқимыз'):
            self.assertTrue(reports_existing_student(text), text)
        for text in ('Мы ещё не учимся у вас', 'Мы не у вас учимся', 'Хотим у вас учиться',
                     'У вас можно учиться?', 'Раньше занимались у вас', 'Балам сізде оқымайды'):
            self.assertFalse(reports_existing_student(text), text)

    async def test_expired_identity_survives_restart_and_crm_outage(self):
        self.remember_student()
        bot.last_activity[self.chat] = time.time() - bot.SESSION_TIMEOUT - 1
        bot._save_conversation(self.chat)
        for state in (bot.known_users, bot.client_dossiers, bot.crm_lookup_state, bot.chat_history):
            state.clear()
        await bot._load_conversations()
        self.lookup.side_effect = bot.CRMUnavailable('auth')
        self.queue('Здравствуйте')
        await bot.process_dialog(self.chat)
        self.assertIn('по вашим занятиям', self.send.call_args.args[1])
        self.assertEqual(bot.client_dossiers[self.chat]['id'], 42)
        self.assertTrue(bot._student_support(self.chat))
        self.assertIn(self.chat, bot.followup_blocked)
        self.ai.assert_not_awaited()
        directive = bot._student_dialog_directive(self.chat)
        self.assertNotIn('Устаревшее расписание', directive)
        self.assertNotIn('12345', directive)

    async def test_old_crm_notice_screenshot_survives_timeout(self):
        bot.record_crm_notification_in_history(self.chat, 'Ученик не пришёл на урок. Свяжитесь с тренером.')
        bot.crm_notify_recent[self.chat] = time.time() - 7 * 86400
        bot.last_activity[self.chat] = time.time() - 7 * 86400
        self.lookup.side_effect = bot.CRMUnavailable('auth')
        for text in ('Здравствуйте', 'Можно номер и имя тренера', 'Наш ребенок обучается у Вас', 'Тест Ученик, 9 лет'):
            self.queue(text)
            await bot.process_dialog(self.chat)
        self.ai.assert_not_awaited()
        self.lookup.assert_awaited_once()  # outage backoff
        for call in self.send.call_args_list:
            self.assertNotRegex(call.args[1].lower(), r'возраст|сколько лет|35 000|для кого|записаться')
        self.assertIn('филиале вы занимаетесь', self.send.call_args.args[1])

    async def test_trainer_request_from_unknown_phone_never_starts_sales(self):
        self.queue('Можно номер и имя тренера')
        await bot.process_dialog(self.chat)
        self.assertIn('филиале вы занимаетесь', self.send.call_args.args[1])
        self.assertNotIn(self.chat, bot.known_users)
        self.ai.assert_not_awaited()

    async def test_self_report_from_other_phone_is_persisted_without_binding_identity(self):
        self.queue('Наш ребенок обучается у Вас')
        await bot.process_dialog(self.chat)
        bot.conversation_facts.clear()
        await bot._load_conversations()
        self.assertTrue(bot._student_support(self.chat))
        self.assertNotIn(self.chat, bot.known_users)
        self.assertNotIn(self.chat, bot.client_dossiers)
        self.assertTrue(bot._chat_looks_followup_closed(self.chat))

    async def test_sales_reply_and_registration_tool_are_blocked_for_support(self):
        bot.conversation_facts[self.chat] = {'student_reported': True, 'language': 'ru'}
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {'preference': 'Камал'})]),
                               completion('Сколько лет вашему ребёнку?')]
        with patch.object(bot.crm, 'create_lead', AsyncMock()) as create:
            self.queue('Вопрос по оплате')
            await bot.process_dialog(self.chat)
            create.assert_not_awaited()
        self.assertNotIn('Сколько лет', self.send.call_args.args[1])
        request = self.ai.call_args_list[0].kwargs
        self.assertEqual([t['function']['name'] for t in request['tools']], ['request_manager_callback'])
        for call in self.ai.call_args_list:
            self.assertIn('РЕЖИМ ПОДДЕРЖКИ', call.kwargs['messages'][-1]['content'])
            self.assertNotIn('Предлагай детские группы', call.kwargs['messages'][-1]['content'])

    async def test_verified_trainer_is_returned_without_ai(self):
        self.remember_student()
        self.lookup.side_effect = bot.CRMUnavailable('auth')
        with patch.object(bot.crm, 'get_student_trainers', AsyncMock(return_value=[
                {'name': 'Тест Тренер', 'phone': '77000000001'}])) as trainers:
            self.queue('Можно номер и имя тренера')
            await bot.process_dialog(self.chat)
            trainers.assert_awaited_once_with(42)
        self.assertIn('Тест Тренер — 77000000001', self.send.call_args.args[1])
        self.ai.assert_not_awaited()

    async def test_trainer_outage_offers_verified_branch_manager(self):
        self.remember_student()
        self.lookup.side_effect = bot.CRMUnavailable('auth')
        with patch.object(bot.crm, 'get_student_trainers', AsyncMock(side_effect=bot.CRMUnavailable('auth'))):
            self.queue('Можно номер и имя тренера')
            await bot.process_dialog(self.chat)
        answer = self.send.call_args.args[1]
        self.assertIn(bot.BRANCH_PHONES[42763], answer)
        self.assertIn('управляющий', answer)
        self.assertNotIn('свяжется', answer)

    async def test_new_lead_status_is_not_current_student(self):
        self.lookup.return_value = {'user': {'id': 42, 'clientStateId': 99}, 'dossier': {'id': 42}}
        self.ai.side_effect = [completion('Для ребёнка или для себя?')]
        with patch.object(bot.crm, '_get_client_state_id', AsyncMock(return_value=5)):
            self.queue('Хочу записаться')
            await bot.process_dialog(self.chat)
        self.assertFalse(bot._student_support(self.chat))
        self.assertEqual(bot.crm_lookup_state[self.chat]['status'], 'not_student')
        self.ai.assert_awaited_once()

    async def test_fresh_inactive_status_replaces_cached_student_after_restart(self):
        self.remember_student()
        bot.crm_notify_recent[self.chat] = time.time() - 7 * 86400
        self.lookup.return_value = {'user': {'id': 42, 'clientStateId': 99}, 'dossier': {'id': 42}}
        with patch.object(bot.crm, '_get_client_state_id', AsyncMock(return_value=5)):
            await bot._refresh_student_context(self.chat)
        bot._save_conversation(self.chat)
        await bot._load_conversations()
        self.assertFalse(bot._student_support(self.chat))

    async def test_prompt_marker_is_not_a_dossier_and_actual_dossier_reaches_model(self):
        # The old detector found the marker inside SYSTEM_PROMPT and skipped
        # injecting the actual matched student's details into the model.
        self.assertIn('[СИСТЕМНОЕ ДОСЬЕ КЛИЕНТА]', bot.SYSTEM_PROMPT)
        bot.chat_history[self.chat] = [{'role': 'system', 'content': bot.SYSTEM_PROMPT}]
        bot._save_conversation(self.chat)
        await bot._load_conversations()
        self.assertNotIn(self.chat, bot.known_users)
        self.lookup.return_value = {
            'user': {'id': 42, 'name': 'Тест Ученик', 'clientStateId': 5},
            'dossier': {'id': 42, 'name': 'Тест Ученик', 'filial_id': 42763, 'schedule': 'Свежий урок'}}
        self.ai.side_effect = [completion('Ваш урок указан в расписании.')]
        with patch.object(bot.crm, '_get_client_state_id', AsyncMock(return_value=5)):
            self.queue('Когда следующий урок?')
            await bot.process_dialog(self.chat)
        messages = self.ai.call_args.kwargs['messages']
        self.assertEqual(messages[0]['content'], bot.SYSTEM_PROMPT)
        self.assertIn('Тест Ученик', messages[-1]['content'])
        self.assertIn('Свежий урок', messages[-1]['content'])
        self.assertNotIn('Предлагай детские группы', messages[-1]['content'])

    async def test_phone_lookup_errors_are_not_empty_searches(self):
        crm = bot.MoyKlassCRM('fake')
        for status in (403, 429, 500):
            response = httpx.Response(status, json={}, request=httpx.Request('GET', 'https://example.test/users'))
            with patch.object(crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'fake'})), \
                 patch('httpx.AsyncClient.get', AsyncMock(return_value=response)):
                with self.assertRaises(bot.CRMUnavailable):
                    await crm.find_user_smart('77001234567')
        with patch.object(crm, '_get_headers', AsyncMock(return_value=None)):
            with self.assertRaises(bot.CRMUnavailable):
                await crm.find_user_smart('77001234567')

    async def test_expired_token_is_refreshed_before_phone_search(self):
        crm = bot.MoyKlassCRM('fake')
        user = {'id': 42, 'name': 'Тест', 'phone': '77001234567', 'filials': [42763]}
        responses = [SimpleNamespace(status_code=401), SimpleNamespace(status_code=200, json=lambda: {'users': [user]})]
        with patch.object(crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'fake'})) as auth, \
             patch('httpx.AsyncClient.get', AsyncMock(side_effect=responses)):
            found = await crm.find_user_smart('77001234567')
        self.assertEqual(found['dossier']['id'], 42)
        self.assertEqual(auth.await_count, 2)

    async def test_enrichment_timeout_does_not_discard_saved_identity(self):
        self.lookup.return_value = {'user': {'id': 42, 'name': 'Тест', 'clientStateId': 5},
                                    'dossier': {'id': 42, 'name': 'Тест', 'filial_id': 42763}}
        with patch.object(bot.crm, '_get_client_state_id', AsyncMock(return_value=5)), \
             patch.object(bot.crm, 'enrich_student_dossier', AsyncMock(side_effect=asyncio.TimeoutError)):
            await bot._refresh_student_context(self.chat)
        bot.known_users.clear()
        bot.client_dossiers.clear()
        await bot._load_conversations()
        self.assertEqual(bot.known_users[self.chat]['id'], 42)
        self.assertEqual(bot.client_dossiers[self.chat]['filial_id'], 42763)

    async def test_unknown_contact_gets_neutral_greeting_during_crm_outage(self):
        self.lookup.side_effect = bot.CRMUnavailable('auth')
        self.queue('Здравствуйте')
        await bot.process_dialog(self.chat)
        self.assertEqual(self.send.call_args.args[1], 'Здравствуйте! Чем могу помочь?')
        self.ai.assert_not_awaited()

    async def test_prospect_can_correct_trainer_support_assumption(self):
        self.queue('Можно номер тренера')
        await bot.process_dialog(self.chat)
        self.ai.side_effect = [completion('Занятия для ребёнка или для вас?')]
        self.queue('Нет, мы ещё не учимся, хотим записаться')
        await bot.process_dialog(self.chat)
        self.assertFalse(bot._student_support(self.chat))
        self.ai.assert_awaited_once()

    async def test_unrelated_phone_is_not_bound(self):
        crm = bot.MoyKlassCRM('fake')
        response = SimpleNamespace(status_code=200, json=lambda: {'users': [
            {'id': 7, 'name': 'Другой', 'phone': '77007654321'}]})
        with patch.object(crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'fake'})), \
             patch('httpx.AsyncClient.get', AsyncMock(return_value=response)):
            self.assertIsNone(await crm.find_user_smart('77001234567'))

    async def test_trainer_lookup_excludes_another_students_records(self):
        crm = bot.MoyKlassCRM('fake')
        today = datetime.now(TZ).date().isoformat()
        records = {'lessonRecords': [
            {'userId': 42, 'lesson': {'date': today, 'teacherIds': [10]}},
            {'userId': 99, 'lesson': {'date': today, 'teacherIds': [20]}},
            {'userId': 42, 'lesson': {'date': '2000-01-01', 'teacherIds': [20]}}]}
        managers = [{'id': 10, 'name': 'Верный', 'phone': '77000000001'},
                    {'id': 20, 'name': 'Чужой', 'phone': '77000000002'}]
        responses = [httpx.Response(200, json=value, request=httpx.Request('GET', 'https://example.test'))
                     for value in (records, managers)]
        with patch.object(crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'fake'})), \
             patch('httpx.AsyncClient.get', AsyncMock(side_effect=responses)):
            found = await crm.get_student_trainers(42)
        self.assertEqual(found, [{'name': 'Верный', 'phone': '77000000001'}])


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = UsageLedger(str(Path(self.tmp.name) / 'usage.sqlite3'))
        self.day = date(2026, 10, 7)
        self.ts = datetime(2026, 10, 7, 12, tzinfo=TZ).timestamp()

    def test_cached_price_and_response_dedup_and_persistent_rate(self):
        for _ in range(2):
            self.ledger.record(metered(), chat='client', operation='dialog', model='gpt-4.1-mini', ts=self.ts)
        s = self.ledger.summary(self.day)
        self.assertEqual((s['calls'], s['input'], s['cached'], s['output']), (1, 1000, 400, 100))
        self.assertAlmostEqual(s['cost'], 0.00044)
        reopened = UsageLedger(self.ledger.path, {'gpt-4.1-mini': [100, 100, 100]})
        self.assertAlmostEqual(reopened.summary(self.day)['cost'], 0.00044)

    def test_unknown_model_or_missing_usage_not_free(self):
        self.ledger.record(metered(model='unknown'), chat='c', operation='dialog', model='unknown', ts=self.ts)
        self.ledger.record(completion('text'), chat='c', operation='dialog', model='gpt-4.1-mini', ts=self.ts)
        self.assertEqual(self.ledger.summary(self.day)['unknown'], 2)
        self.assertIn('Итог неполный', self.ledger.report_text(self.day))

    def test_voice_billed_seconds_and_price(self):
        self.ledger.record(SimpleNamespace(text='voice', duration=9.2, usage=SimpleNamespace(seconds=10)),
                           chat='c', operation='voice', model='whisper-1', ts=self.ts)
        s = self.ledger.summary(self.day)
        self.assertEqual(s['audio'], 10)
        self.assertAlmostEqual(s['cost'], 0.001)

    def test_local_midnight_unique_clients_new_vs_returning_and_activity_dedup(self):
        midnight = datetime(2026, 10, 7, tzinfo=TZ).timestamp()
        self.ledger.event('incoming', 'returning', 'old', ts=midnight - 1)
        for identity, chat in [('a', 'new'), ('b', 'returning'), ('a', 'new')]:
            self.ledger.event('incoming', chat, identity, ts=midnight)
        self.ledger.event('service', 'manager', ts=midnight)
        s = self.ledger.summary(self.day)
        self.assertEqual((s['clients'], s['new_clients'], s['events']['incoming']), (2, 1, 2))
        self.assertEqual(s['events']['service'], 1)

    def test_report_claim_recovery_after_process_crash(self):
        self.assertTrue(self.ledger.claim_report(self.day, now=1000))
        self.assertFalse(self.ledger.claim_report(self.day, now=1001))
        self.assertTrue(self.ledger.claim_report(self.day, now=1601))
        self.ledger.finish_report(self.day, True)
        self.assertFalse(self.ledger.claim_report(self.day, now=10000))


if __name__ == '__main__':
    unittest.main()
