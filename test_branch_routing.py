"""Branch isolation and October curriculum regressions; all HTTP is mocked."""
import httpx
import unittest
from unittest.mock import AsyncMock, patch

import test_reliability as reliability
from test_reliability import bot, completion, tool
from conversation_memory import update_facts
from dialog_policy import SCHOOL_ADMIN_REPLY, qualification_directive, deferred_price_reply


class BranchRoutingTests(unittest.IsolatedAsyncioTestCase):
    enterContext = reliability.ReliabilityTests.enterContext
    setUp = reliability.ReliabilityTests.setUp
    asyncTearDown = reliability.ReliabilityTests.asyncTearDown
    queue = reliability.ReliabilityTests.queue

    async def run_lead(self, preference, existing=None, fail_patch=False, courses='шахматы'):
        self.requests = []
        branch = bot._guess_filial_from_text(preference)

        async def respond(request, **kwargs):
            self.requests.append(request)
            path = request.url.path
            if path.endswith('/managers'):
                data = [{'id': 73, 'name': 'Шолпан', 'phone': '77781048127', 'filials': [42763]},
                        {'id': 95, 'name': 'Айгерим', 'phone': '77752542671', 'filials': [50847, 54648]},
                        {'id': 96, 'name': 'Томирис Кайсина', 'phone': '77712314549', 'filials': [59]}]
            elif request.method == 'GET' and path.endswith('/users/42'):
                data = existing
            elif request.method == 'PATCH':
                return httpx.Response(500 if fail_patch else 200, json=existing, request=request)
            elif path.endswith('/users'):
                data = {'id': 42, 'filials': [branch]}
            else:
                data = {'id': 1}
            return httpx.Response(200, json=data, request=request)

        with patch('httpx.AsyncClient.send', side_effect=respond), \
             patch.object(bot.crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'test'})), \
             patch.object(bot.crm, 'find_user_smart', AsyncMock(return_value={'user': existing} if existing else None)), \
             patch.object(bot.crm, 'notify_new_lead_managers', AsyncMock()) as notify, \
             patch.object(bot, '_mark_lead_notified'):
            result = await bot.crm.create_lead('Тест', '77001234567', '9', 'новичок', preference,
                                               courses=courses)
        return result, notify

    async def test_new_leads_stay_in_selected_branch_without_class_enrollment(self):
        import json
        for preference, branch in [('Камал', 42763), ('онлайн', 50847), ('Binom Кадыр Мырза Али', 54648)]:
            courses = 'английский, математика' if branch == 42763 else 'шахматы'
            result, notify = await self.run_lead(preference, courses=courses)
            self.assertIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
            self.assertFalse(any(r.url.path.endswith('/joins') for r in self.requests))
            create = next(r for r in self.requests if r.method == 'POST' and r.url.path.endswith('/users'))
            payload = json.loads(create.content)
            self.assertEqual(payload['filials'], [branch])
            self.assertEqual(payload['mainFilialId'], branch)
            self.assertNotIn(37754, payload['filials'])
            comment = next(r for r in self.requests if r.url.path.endswith('/userComments'))
            self.assertIn('Курсы: ' + courses, json.loads(comment.content)['comment'])
            notify.assert_awaited_once()
            self.assertEqual(notify.call_args.args[0], branch)
            self.assertEqual(notify.call_args.kwargs['courses'], courses)

    async def test_unconfirmed_course_location_is_not_registered(self):
        result, notify = await self.run_lead('онлайн', courses='английский')
        self.assertNotIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
        self.assertFalse(self.requests)
        notify.assert_not_awaited()

    async def test_existing_card_preserves_real_branches_and_records_new_request(self):
        import json
        existing = {'id': 42, 'phone': '77001234567', 'filials': [50847], 'responsibles': [95]}
        result, _ = await self.run_lead('Камал', existing)
        self.assertIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
        patch_request = next(r for r in self.requests if r.method == 'PATCH')
        self.assertEqual(json.loads(patch_request.content), {'filials': [50847, 42763], 'responsibles': [95, 73]})
        self.assertFalse(any(r.url.path.endswith('/joins') for r in self.requests))

    async def test_failed_existing_branch_update_does_not_claim_success(self):
        result, notify = await self.run_lead('Камал', {'id': 42, 'phone': '77001234567', 'filials': [50847]}, True)
        self.assertNotIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
        notify.assert_not_awaited()

    async def test_manager_from_another_branch_is_never_assigned(self):
        result, notify = await self.run_lead('Аркада')
        self.assertNotIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
        self.assertFalse(any(r.method != 'GET' for r in self.requests))
        notify.assert_not_awaited()

    async def test_external_schools_redirect_without_ai_or_crm_writes(self):
        for school in ('Harmony', 'Harmony Global', 'NGS', 'Quantum', 'Хармони Глобал', 'Квантум', 'НГС'):
            bot.conversation_facts.pop(self.chat, None)
            self.queue('Хочу записать ребёнка в ' + school)
            await bot.process_dialog(self.chat)
            self.assertEqual(self.send.call_args.args[1], SCHOOL_ADMIN_REPLY)
        self.ai.assert_not_awaited()
        self.assertIn(self.chat, bot.followup_blocked)
        with patch.object(bot.crm, '_get_headers', AsyncMock()) as auth:
            result = await bot.crm.create_lead('Тест', '77001234567', '9', '', 'Harmony Global')
            auth.assert_not_awaited()
            self.assertIn(SCHOOL_ADMIN_REPLY, result)

    async def test_external_school_kazakh_and_later_internal_choice(self):
        self.queue('Балам Quantum мектебінде оқиды, шахматқа жазылғымыз келеді')
        await bot.process_dialog(self.chat)
        self.assertIn('мектеп әкімшілігіне', self.send.call_args.args[1])
        self.ai.side_effect = [completion('Камалда ағылшын тілі бар.')]
        self.queue('Камал. Ағылшын')
        await bot.process_dialog(self.chat)
        self.ai.assert_awaited_once()

    async def test_courses_survive_short_answers_and_reach_crm(self):
        facts = {}
        for text in ('Интересует английский и математика', 'Камал', '9'):
            update_facts(facts, text, last_question='Сколько лет ребёнку?')
        self.assertEqual(facts['courses'], ['английский', 'математика'])
        self.assertIn('не спрашивай шахматный разряд', qualification_directive(facts))
        self.assertIn('155 000', deferred_price_reply(facts, 'По ценам напишите, я подумаю'))
        bot.conversation_facts[self.chat] = facts
        self.ai.side_effect = [completion(calls=[tool('register_client_request', {'preference': 'Камал'})])]
        with patch.object(bot.crm, 'create_lead', AsyncMock(return_value=bot.crm._handoff_message(
                'Менеджер', '+7700', success=True))) as create:
            self.queue('Зовут Тест')
            await bot.process_dialog(self.chat)
            self.assertEqual(create.call_args.kwargs['courses'], 'английский, математика')

    async def test_school59_never_falls_back_to_arkada_or_fake_success(self):
        with patch.object(bot, 'SCHOOL_59_FILIAL_ID', None), \
             patch.object(bot.crm, '_resolve_school59_filial', AsyncMock(return_value=None)), \
             patch.object(bot.crm, '_get_headers', AsyncMock()) as auth:
            result = await bot.crm.create_lead('Тест', '77001234567', '9', '', 'Школа №59', 'математика')
            auth.assert_not_awaited()
            self.assertNotIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
            self.assertIn('Томирис Ержановна', result)
            self.assertIn('+7 771 231 4549', result)
        facts = {}
        update_facts(facts, 'Школа № 59')
        self.assertEqual(facts['preference'], 'Школа №59')

    async def test_school59_lookup_requires_unique_active_branch(self):
        async def respond(request, **kwargs):
            return httpx.Response(200, request=request, json=[
                {'id': 59, 'name': 'Школа номер 59', 'status': 'active'},
                {'id': 60, 'name': 'Школа №59 старая', 'status': 'archive'}])
        with patch.object(bot, 'SCHOOL_59_FILIAL_ID', None), \
             patch.dict(bot.BRANCH_MANAGERS), patch.dict(bot.BRANCH_PHONES), \
             patch('httpx.AsyncClient.send', side_effect=respond), \
             patch.object(bot.crm, '_get_headers', AsyncMock(return_value={} )):
            # Empty auth must fail closed, without an API request.
            self.assertIsNone(await bot.crm._resolve_school59_filial())
        with patch.object(bot, 'SCHOOL_59_FILIAL_ID', None), \
             patch.dict(bot.BRANCH_MANAGERS), patch.dict(bot.BRANCH_PHONES), \
             patch('httpx.AsyncClient.send', side_effect=respond), \
             patch.object(bot.crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'test'})):
            self.assertEqual(await bot.crm._resolve_school59_filial(), 59)
            self.assertEqual(bot._guess_filial_from_text('Школа номер 59'), 59)
            self.assertEqual(bot._matched_key_from_text('Школа номер 59'), 'school59')
            self.assertEqual(bot._new_lead_recipients(59), [('Томирис Ержановна', '+7 771 231 4549')])

    async def test_school59_routes_to_tomiris_after_branch_exists(self):
        with patch.object(bot, 'SCHOOL_59_FILIAL_ID', 59):
            result, notify = await self.run_lead('Школа номер 59', courses='математика')
            self.assertIn('ЗАЯВКА ОФОРМЛЕНА В CRM', result)
            self.assertIn('Томирис Ержановна', result)
            self.assertEqual(notify.call_args.args, (59, 'school59'))

    async def test_multiple_active_school59_branches_are_not_guessed(self):
        async def respond(request, **kwargs):
            return httpx.Response(200, request=request, json=[
                {'id': 59, 'name': 'Школа номер 59', 'status': 'active'},
                {'id': 60, 'name': 'Школа №59', 'status': 'active'}])
        with patch.object(bot, 'SCHOOL_59_FILIAL_ID', None), \
             patch('httpx.AsyncClient.send', side_effect=respond), \
             patch.object(bot.crm, '_get_headers', AsyncMock(return_value={'x-access-token': 'test'})):
            self.assertIsNone(await bot.crm._resolve_school59_filial())

    async def test_second_course_is_added_and_not_mistaken_for_name(self):
        facts = {}
        update_facts(facts, 'Шахматы')
        update_facts(facts, 'И ещё английский')
        self.assertEqual(facts['courses'], ['шахматы', 'английский'])
        update_facts(facts, 'Математика', last_question='Как зовут ребёнка?')
        self.assertNotIn('name', facts)

    async def test_ambiguous_branch_and_primary_branch(self):
        self.assertIsNone(bot._guess_filial_from_text('Аркада или Камал'))
        self.assertEqual(bot._primary_filial({'filials': [37754, 42763], 'mainFilialId': 42763}), 42763)
        self.assertIsNone(bot._primary_filial({'filials': [37754, 42763]}))
