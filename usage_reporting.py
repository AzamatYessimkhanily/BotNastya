"""Persistent API usage and activity ledger. Costs are estimates, never invoices."""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, time as day_time, timedelta
from decimal import Decimal
import json
import math
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Almaty')
# USD per million tokens (input, cached input, output), standard API pricing.
# https://developers.openai.com/api/docs/models/{model}, checked 2026-10-07.
RATES = {
    'gpt-4o-mini': (0.15, 0.075, 0.60),
    'gpt-4o-mini-2024-07-18': (0.15, 0.075, 0.60),
    'gpt-4.1-mini': (0.40, 0.10, 1.60),
    'gpt-4.1-mini-2025-04-14': (0.40, 0.10, 1.60),
    'gpt-4-turbo': (10, 10, 30),
    'gpt-4-turbo-2024-04-09': (10, 10, 30),
}


def get_field(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class UsageLedger:
    def __init__(self, path, rates=None):
        self.path = path
        self.rates = dict(RATES)
        if rates:
            for model, values in rates.items():
                if len(values) != 3 or any(not math.isfinite(float(v)) or float(v) < 0 for v in values):
                    raise ValueError('Rates must be three nonnegative finite USD amounts')
                self.rates[model] = tuple(float(v) for v in values)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.executescript('''
                    CREATE TABLE IF NOT EXISTS usage (
                        id TEXT PRIMARY KEY, ts REAL NOT NULL, chat TEXT NOT NULL,
                        operation TEXT NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL, cached_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL, audio_seconds REAL NOT NULL,
                        cost_usd REAL, rates TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts);
                    CREATE TABLE IF NOT EXISTS activity (
                        id TEXT PRIMARY KEY, ts REAL NOT NULL, chat TEXT NOT NULL, kind TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS activity_ts ON activity(ts);
                    CREATE TABLE IF NOT EXISTS reports (
                        day TEXT PRIMARY KEY, status TEXT NOT NULL, attempted REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                ''')
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('started_at', ?)", (str(time.time()),))
                yield db
        finally:
            db.close()

    def started_at(self):
        with self.db() as db:
            return float(db.execute("SELECT value FROM metadata WHERE key='started_at'").fetchone()[0])

    def event(self, kind, chat, identity=None, ts=None):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO activity VALUES (?, ?, ?, ?)',
                       (identity or str(uuid4()), time.time() if ts is None else ts, chat, kind))

    def record(self, response, *, chat, operation, model, status='ok', ts=None):
        model = get_field(response, 'model') or model
        usage = get_field(response, 'usage')
        prompt = int(get_field(usage, 'prompt_tokens', 0) or 0)
        output = int(get_field(usage, 'completion_tokens', 0) or 0)
        cached = min(prompt, int(get_field(get_field(usage, 'prompt_tokens_details'), 'cached_tokens', 0) or 0))
        seconds = get_field(usage, 'seconds', get_field(response, 'duration'))
        seconds = math.ceil(float(seconds)) if seconds is not None else None
        rates = self.rates.get(model)
        cost = None
        if model == 'whisper-1' and seconds is not None and status == 'ok':
            cost = float(Decimal(seconds) * Decimal('0.006') / 60)
            rates = {'usd_per_minute': 0.006}
        elif rates and usage is not None and get_field(usage, 'prompt_tokens') is not None and status == 'ok':
            a, b, c = (Decimal(str(v)) for v in rates)
            cost = float(((prompt - cached) * a + cached * b + output * c) / 1000000)
        # Response IDs prevent counting the same returned response twice.
        identity = get_field(response, 'id') or str(uuid4())
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO usage VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (identity, time.time() if ts is None else ts, chat, operation, model, status,
                        prompt, cached, output, seconds or 0, cost, json.dumps(rates)))

    def summary(self, day):
        start = datetime.combine(day, day_time.min, TZ).timestamp()
        end = datetime.combine(day + timedelta(days=1), day_time.min, TZ).timestamp()
        with self.db() as db:
            rows = list(db.execute('SELECT * FROM usage WHERE ts >= ? AND ts < ?', (start, end)))
            events = list(db.execute('SELECT * FROM activity WHERE ts >= ? AND ts < ?', (start, end)))
            clients = {r['chat'] for r in events if r['kind'] == 'incoming'}
            new_clients = sum(not db.execute(
                "SELECT 1 FROM activity WHERE kind='incoming' AND chat=? AND ts < ? LIMIT 1", (chat, start)
            ).fetchone() for chat in clients)
        groups = {}
        for row in rows:
            key = (row['model'], row['operation'])
            item = groups.setdefault(key, {'calls': 0, 'cost': 0.0})
            item['calls'] += 1
            item['cost'] += row['cost_usd'] or 0
        client_cost = Counter()
        for row in rows:
            if row['chat']:
                client_cost[row['chat']] += row['cost_usd'] or 0
        return {
            'clients': len(clients), 'new_clients': new_clients,
            'events': Counter(row['kind'] for row in events), 'calls': len(rows),
            'input': sum(r['input_tokens'] for r in rows), 'cached': sum(r['cached_tokens'] for r in rows),
            'output': sum(r['output_tokens'] for r in rows), 'audio': sum(r['audio_seconds'] for r in rows),
            'cost': sum(r['cost_usd'] or 0 for r in rows), 'unknown': sum(r['cost_usd'] is None for r in rows),
            'errors': sum(r['status'] != 'ok' for r in rows), 'groups': groups, 'client_cost': client_cost,
        }

    def report_text(self, day):
        s = self.summary(day)
        e = s['events']
        lines = [f'Отчёт бота за {day:%d.%m.%Y} (Астана, 00:00–24:00)',
                 f'Клиентов написало: {s["clients"]}. Впервые с начала учёта: {s["new_clients"]}.',
                 f'Входящих сообщений: {e["incoming"]}. Ответов: {e["reply"]}.',
                 f'Заявок оформлено: {e["lead"]}. Обращений управляющему: {e["callback"]}.',
                 f'Напоминаний: {e["followup"]}. Служебных/CRM сообщений: {e["service"]}.',
                 f'Неудачных отправок ответов: {e["reply_failed"]}. Истекло без отправки: {e["reply_expired"]}.',
                 f'Запросов к ИИ: {s["calls"]}; ошибок: {s["errors"]}.',
                 f'Токены: вход {s["input"]:,}, из них кеш {s["cached"]:,}; выход {s["output"]:,}.',
                 f'Голосовые: {s["audio"]:.0f} сек.',
                 f'Расход ИИ по известным данным: ${s["cost"]:.4f}.']
        labels = {'dialog': 'ответы', 'tool_result': 'обработка заявок', 'voice': 'голосовые', 'evaluation': 'проверки'}
        for (model, operation), g in sorted(s['groups'].items()):
            lines.append(f'• {model}, {labels.get(operation, operation)}: {g["calls"]} запросов, ${g["cost"]:.4f}')
        if s['clients']:
            lines.append(f'Средний расход ИИ на написавшего клиента: ${sum(s["client_cost"].values()) / s["clients"]:.4f}.')
        if s['client_cost']:
            lines.append('Наибольшие расходы по чатам:')
            for chat, cost in s['client_cost'].most_common(5):
                lines.append(f'• …{chat.split("@")[0][-4:]}: ${cost:.4f}')
        if s['unknown']:
            lines.append(f'ВНИМАНИЕ: стоимость {s["unknown"]} запросов неизвестна (нет usage/тарифа или ошибка). Итог неполный.')
        started = datetime.fromtimestamp(self.started_at(), TZ)
        lines.append(f'Учёт начат {started:%d.%m.%Y %H:%M}; за первый день данные частичные.')
        lines.append('Расчёт по тарифам API, не счёт OpenAI. WhatsApp, CRM, сервер и налоги не включены. Отправлено = принято провайдером.')
        return '\n'.join(lines)

    def due_days(self, now, hour=9):
        now = now.astimezone(TZ)
        last_day = now.date() - timedelta(days=1 if now.hour >= hour else 2)
        first_day = datetime.fromtimestamp(self.started_at(), TZ).date()
        with self.db() as db:
            sent = {r[0] for r in db.execute("SELECT day FROM reports WHERE status='sent'")}
        # One report per tick; after downtime, catch up in chronological order.
        while first_day <= last_day:
            if first_day.isoformat() not in sent:
                return [first_day]
            first_day += timedelta(days=1)
        return []

    def claim_report(self, day, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO reports VALUES (?, ?, ?)', (day.isoformat(), 'pending', 0))
            cur = db.execute("UPDATE reports SET status='sending', attempted=? WHERE day=? AND "
                             "status != 'sent' AND (status != 'sending' OR attempted < ?)",
                             (now, day.isoformat(), now - 600))
            return cur.rowcount == 1

    def finish_report(self, day, sent):
        with self.db() as db:
            db.execute('UPDATE reports SET status=?, attempted=? WHERE day=?',
                       ('sent' if sent else 'pending', time.time(), day.isoformat()))
