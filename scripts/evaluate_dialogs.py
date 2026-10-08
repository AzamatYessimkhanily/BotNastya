"""Opt-in live model evaluation. CRM/WhatsApp are mocked; only OpenAI is reachable.

python scripts/evaluate_dialogs.py --env-file /path/to/.env --model gpt-4.1-mini
Uses synthetic conversations, at most two model requests per case.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv


async def evaluate(args):
    load_dotenv(args.env_file)
    os.environ['OPENAI_MODEL'] = args.model
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import bot
    from usage_reporting import UsageLedger
    ledger = UsageLedger(args.usage_file)
    cases = [
        ('kk_name', {'language': 'kk', 'audience': 'ребёнок', 'age': '9', 'experience': 'орташа', 'preference': 'Камал'},
         'Балаңыздың аты кім?', ['Адай', 'Қазір баруға бола ма?'], ['рәсім', 'уақыт'], []),
        ('kk_short', {'language': 'kk', 'audience': 'ребёнок', 'age': '9', 'experience': 'орташа'},
         'Қай филиал ыңғайлы?', ['Камал'], ['аты'], ['как зовут', 'заявка']),
        ('ru_price', {'language': 'ru', 'audience': 'ребёнок', 'age': '6', 'preference': 'Камал', 'experience': 'играет дома'},
         'Как зовут вашего ребёнка?', ['Максат', 'По ценам напишите пожалуйста точно. Я посоветуюсь и отпишусь'],
         ['30 000'], ['как зовут', 'какой филиал', 'свяжется']),
        ('work', {}, '', ['Вам нужны тренеры по шахматам?'], ['Еркежан'], ['сколько лет', 'пробный']),
        ('competitor', {}, '', ['А где в Астане можно сходить поиграть в любое время?'], ['GMCA'], ['Центр шахматного', '2ГИС']),
        ('underage', {}, 'Сколько лет ребёнку?', ['4,5 года'], ['5'], ['ещё помощь', '?']),
        ('kk_price', {'language': 'kk', 'age': '9', 'preference': 'Камал', 'experience': 'играет дома'},
         'Балаңыздың аты кім?', ['Айтөре', 'Бағасы қанша? Ақылдасып, өзім хабарласамын.'], ['30 000'], ['заявка', 'оформ']),
        ('ru_lead', {'language': 'ru', 'age': '8', 'experience': 'начинающий', 'preference': 'Камал'},
         'Как зовут ребёнка?', ['Марат'], ['Шолпан'], []),
    ]
    results = []
    real_http_send = bot.httpx.AsyncClient.send

    async def only_openai(client, request, *a, **kw):
        if request.url.host != 'api.openai.com':
            raise AssertionError('Evaluation attempted non-OpenAI network request')
        return await real_http_send(client, request, *a, **kw)

    def record(response, chat, operation, model=None, status='ok'):
        ledger.record(response, chat='', operation='evaluation', model=model or args.model, status=status)

    with tempfile.TemporaryDirectory() as state, patch.object(bot.httpx.AsyncClient, 'send', only_openai), \
         patch.object(bot, '_record_usage', record), patch.object(bot, '_record_event'), \
         patch.object(bot, '_hydrate_chat_history', AsyncMock()), \
         patch.object(bot.crm, 'find_user_smart', AsyncMock(return_value=None)), \
         patch.object(bot.crm, 'create_lead', AsyncMock(return_value=bot.crm._handoff_message(
             'Шолпан Жолдыбаевна', '+7 778 104 8127', success=True))) as create, \
         patch.object(bot.crm, 'notify_client_callback_request', AsyncMock(side_effect=AssertionError('Unexpected callback'))), \
         patch.object(bot, 'send_whatsapp', AsyncMock(return_value=True)) as send, \
         patch.object(bot, 'CONVERSATION_STATE_FILE', str(Path(state) / 'conversations.sqlite3')), \
         patch.object(bot, 'FOLLOWUP_BLOCKED_FILE', str(Path(state) / 'followup.json')):
        for i, (name, facts, question, inputs, required, forbidden) in enumerate(cases):
            chat = f'7700000100{i}@c.us'
            bot.conversation_facts[chat] = facts.copy()
            bot.chat_history[chat] = [{'role': 'system', 'content': bot.SYSTEM_PROMPT},
                                      {'role': 'system', 'content': '[ТЕЛЕФОН КЛИЕНТА]: 77000001000'}]
            if question:
                bot.chat_history[chat].append({'role': 'assistant', 'content': question})
            bot.message_buffers[chat] = {'messages': inputs}
            create.reset_mock(); send.reset_mock()
            await bot.process_dialog(chat)
            answer = send.call_args.args[1] if send.call_args else ''
            answer = re.sub(r'(?<=\d)[\u00a0\u202f](?=\d)', ' ', answer)
            ok = bool(answer) and all(s.lower() in answer.lower() for s in required)
            ok = ok and not any(s.lower() in answer.lower() for s in forbidden)
            if name in ('ru_price', 'kk_price', 'work', 'underage', 'competitor'):
                ok = ok and create.await_count == 0 and bot._chat_looks_followup_closed(chat)
            if name == 'kk_name':
                ok = ok and create.await_count == 1 and create.call_args.kwargs['name'] == 'Адай'
            if name not in ('kk_name',):
                ok = ok and not re.search('сегодня|бүгін', answer, re.I)
            results.append({'case': name, 'ok': ok, 'answer': answer, 'created': create.await_count})
            print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    await bot.openai_client.close()
    if not all(r['ok'] for r in results):
        raise SystemExit('Live evaluation failed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--model', default='gpt-4.1-mini')
    parser.add_argument('--usage-file', default='evaluation_usage.sqlite3')
    asyncio.run(evaluate(parser.parse_args()))
