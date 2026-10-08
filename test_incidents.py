"""Screenshot regressions and accounting/report delivery checks; no network."""
import asyncio
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
from dialog_policy import detect_language, closure_reason, polish_kazakh
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
