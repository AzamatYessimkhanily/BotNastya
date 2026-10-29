"""Durable, opt-in campaign queue. No network or automatic retries in this module."""
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Almaty')
WORK_START_HOUR = 9
WORK_END_HOUR = 20
DECLINE = re.compile(r'^\s*(?:(?:спасибо|рахмет|рақмет)[,\s]+(?:но\s+)?)?'
    r'(?:нет(?:[,\s]+спасибо)?|(?:(?:нам|мне|это)\s+)?не\s*(?:интересно|актуально|надо|нужно)|'
    r'отказ|стоп|жоқ|керек\s+емес|(?:маған\s+)?қызық\s+емес)[.!\s]*$', re.I)
CAMPAIGN_COMPLAINT = re.compile(
    r'\b(?:спам(?:ите|ить|еры?|ом|а)?|за[её]б(?:ал|ала|али)|отвали\w*|достали|задолбали|надоели|лохотрон|мошенники)\b'
    r'|\b(?:пош[её]л|пошли|иди(?:те)?)\s+(?:на\s*х\w*|к\s+ч[её]рту)\b'
    r'|\b(?:пожалуюсь|заблокирую|забаню)\b'
    r'|\bжалоб\w*\b.{0,35}\b(?:рассыл\w*|реклам\w*|сообщени\w*)'
    r'|\bоткуда\b.{0,35}\b(?:мой|наш|мои|наши)\s+(?:номер|контакт)\w*\b'
    r'|\bгде\b.{0,15}\b(?:взяли|нашли|получили)\b.{0,25}\b(?:номер|контакт)\w*\b'
    r'|\bзачем\s+(?:вы\s+)?(?:мне\s+|нам\s+)?(?:пишете|присылаете|отправляете)\b'
    r'|\b(?:я|мы)\s+не\s+(?:подписывал\w*|соглашал\w*|давал\w*\s+соглас\w*)'
    r'|\b(?:не\s+нужн\w*|не\s+хоч\w*)\b.{0,30}\b(?:реклам\w*|рассыл\w*|предложени\w*)'
    r'|\b(?:уберите|исключите|удалите)\s+(?:пожалуйста\s+)?(?:меня|наш\s+номер)\b'
    r'|\b(?:ошиблись\s+номером|не\s+туда\s+(?:пишете|попали)|нет\s+детей)\b'
    r'|\b(?:жазбаңыз\w*|шағымданам\w*)\b', re.I)


def validate_limits(interval_sec, hourly, daily):
    values = tuple(int(v) for v in (interval_sec, hourly, daily))
    if values[0] < 60 or not 1 <= values[1] <= 60 or not 1 <= values[2] <= 300:
        raise ValueError('Интервал от 60 секунд, максимум 60 в час и 300 за 24 часа')
    return values


def normalize_phone(value):
    text = str(value or '').strip()
    if not re.fullmatch(r'\+?[\d\s()\-]+', text):
        raise ValueError('Номер должен содержать цифры, без формулы или экспоненты')
    digits = re.sub(r'\D', '', text)
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if not 11 <= len(digits) <= 15 or digits.startswith('0'):
        raise ValueError('Нужен международный номер, например +7 7XX XXX XX XX')
    return digits


class CampaignStore:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS campaigns (
              id TEXT PRIMARY KEY, title TEXT NOT NULL, message TEXT NOT NULL, brief TEXT NOT NULL,
              expires REAL NOT NULL, status TEXT NOT NULL DEFAULT 'draft', reason TEXT NOT NULL DEFAULT '',
              created REAL NOT NULL, interval_sec INTEGER NOT NULL, hourly INTEGER NOT NULL, daily INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS recipients (
              campaign TEXT NOT NULL, phone TEXT NOT NULL, name TEXT, branch TEXT,
              consent INTEGER NOT NULL, evidence TEXT NOT NULL, state TEXT NOT NULL,
              attempt_at REAL, accepted_at REAL, message_id TEXT, delivery TEXT, reason TEXT,
              defer_until REAL NOT NULL DEFAULT 0,
              PRIMARY KEY(campaign, phone));
            CREATE TABLE IF NOT EXISTS replies (
              campaign TEXT NOT NULL, phone TEXT NOT NULL, message_id TEXT NOT NULL,
              text TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(campaign, phone, message_id));
            CREATE TABLE IF NOT EXISTS suppressed (phone TEXT PRIMARY KEY, reason TEXT NOT NULL, at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (message_id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS refusal_acks (
              phone TEXT PRIMARY KEY, incoming_id TEXT NOT NULL, created REAL NOT NULL,
              state TEXT NOT NULL, message_id TEXT);
            CREATE INDEX IF NOT EXISTS recipient_phone ON recipients(phone, attempt_at);
            ''')
            if 'defer_until' not in {r['name'] for r in db.execute('PRAGMA table_info(recipients)')}:
                db.execute('ALTER TABLE recipients ADD COLUMN defer_until REAL NOT NULL DEFAULT 0')
        Path(self.path).chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, spec, now=None):
        now = time.time() if now is None else now
        # An explicit null means an undated campaign; 0 is its persisted marker.
        expiry = 0
        if spec['expires_at'] is not None:
            parsed_expiry = datetime.fromisoformat(spec['expires_at'])
            if parsed_expiry.tzinfo is None or parsed_expiry.timestamp() <= now:
                raise ValueError('Нужен будущий срок окончания с часовым поясом')
            expiry = parsed_expiry.timestamp()
        if not re.fullmatch(r'[a-z0-9_-]{1,80}', spec['id']):
            raise ValueError('Некорректный ID кампании')
        if not spec['message'].strip() or len(spec['message']) > 4000:
            raise ValueError('Текст должен содержать от 1 до 4000 символов')
        interval, hourly, daily = (int(spec.get(k, default)) for k, default in
                                    [('interval_sec', 600), ('hourly', 6), ('daily', 30)])
        interval, hourly, daily = validate_limits(interval, hourly, daily)
        with self.db() as db:
            db.execute('INSERT INTO campaigns(id,title,message,brief,expires,created,interval_sec,hourly,daily) VALUES(?,?,?,?,?,?,?,?,?)',
                       (spec['id'], spec['title'], spec['message'], spec['brief'], expiry, now, interval, hourly, daily))

    def set_limits(self, campaign, interval_sec, hourly, daily):
        limits = validate_limits(interval_sec, hourly, daily)
        with self.db() as db:
            if not db.execute("UPDATE campaigns SET interval_sec=?,hourly=?,daily=? WHERE id=? AND status NOT IN ('completed','cancelled')",
                              (*limits, campaign)).rowcount:
                raise ValueError('Кампания отсутствует или завершена')

    def import_rows(self, campaign, rows, consent_note=''):
        counts = {'pending': 0, 'no_consent': 0, 'duplicate': 0, 'invalid': 0, 'suppressed': 0}
        errors = []
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            c = db.execute('SELECT * FROM campaigns WHERE id=?', (campaign,)).fetchone()
            if not c or c['status'] != 'draft':
                raise ValueError('Импорт возможен только в черновик')
            for line, row in enumerate(rows, 2):
                try:
                    phone = normalize_phone(row.get('phone'))
                except ValueError as error:
                    counts['invalid'] += 1
                    errors.append({'row': line, 'reason': str(error)})
                    continue
                supplied_consent = str(row.get('consent', '') or '').strip().lower()
                consent = supplied_consent in ('да', 'yes', 'true', '1')
                evidence = str(row.get('evidence', '') or '').strip()
                if not supplied_consent and consent_note.strip():
                    consent, evidence = True, consent_note.strip()
                consent = bool(consent and evidence)
                state = 'pending' if consent else 'no_consent'
                if db.execute('SELECT 1 FROM suppressed WHERE phone=?', (phone,)).fetchone():
                    state = 'suppressed'
                added = db.execute('INSERT OR IGNORE INTO recipients(campaign,phone,name,branch,consent,evidence,state) VALUES(?,?,?,?,?,?,?)',
                    (campaign, phone, str(row.get('name') or ''), str(row.get('branch') or ''), consent, evidence, state)).rowcount
                counts[state if added else 'duplicate'] += 1
                if not added and not consent:
                    db.execute("UPDATE recipients SET consent=0,state='no_consent',reason='conflicting_duplicate_consent' WHERE campaign=? AND phone=?", (campaign, phone))
        return {'counts': counts, 'errors': errors}

    def control(self, campaign, action, now=None):
        now = time.time() if now is None else now
        if action not in ('activate', 'pause', 'cancel'):
            raise ValueError('Неизвестное действие')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            c = db.execute('SELECT * FROM campaigns WHERE id=?', (campaign,)).fetchone()
            if not c or c['status'] in ('completed', 'cancelled'):
                raise ValueError('Кампания отсутствует или завершена')
            if action == 'activate':
                if c['expires'] and now >= c['expires']:
                    raise ValueError('Акция закончилась')
                if db.execute("SELECT 1 FROM campaigns WHERE status='active' AND id!=?", (campaign,)).fetchone():
                    raise ValueError('Уже запущена другая кампания')
                if not db.execute("SELECT 1 FROM recipients WHERE campaign=? AND state='pending'", (campaign,)).fetchone():
                    raise ValueError('Нет получателей с подтверждённым согласием')
            status = {'activate': 'active', 'pause': 'paused', 'cancel': 'cancelled'}[action]
            db.execute('UPDATE campaigns SET status=?,reason=? WHERE id=?', (status, 'operator_' + action, campaign))
            if action == 'cancel':
                db.execute("UPDATE recipients SET state='cancelled' WHERE campaign=? AND state='pending'", (campaign,))

    def recover(self):
        with self.db() as db:
            db.execute("UPDATE recipients SET state='uncertain',reason='process_restarted' WHERE state='sending'")
            db.execute("UPDATE campaigns SET status='paused',reason='process_restarted' WHERE status='active'")

    def pause_all(self, reason):
        with self.db() as db:
            db.execute("UPDATE campaigns SET status='paused',reason=? WHERE status='active'", (reason,))

    def claim(self, now=None):
        now = time.time() if now is None else now
        local = datetime.fromtimestamp(now, TZ)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE campaigns SET status='completed',reason='expired' WHERE status='active' AND expires>0 AND expires<=?", (now,))
            c = db.execute("SELECT * FROM campaigns WHERE status='active' ORDER BY created LIMIT 1").fetchone()
            if not c or not WORK_START_HOUR <= local.hour < WORK_END_HOUR:
                return None
            # Durable rolling budgets are shared across campaigns and survive restarts.
            times = [r[0] for r in db.execute('SELECT attempt_at FROM recipients WHERE attempt_at>?', (now - 86400,))]
            if len(times) >= c['daily'] or sum(t > now - 3600 for t in times) >= c['hourly']:
                return None
            if times and now - max(times) < c['interval_sec']:
                return None
            while True:
                r = db.execute("SELECT * FROM recipients WHERE campaign=? AND state='pending' AND defer_until<=? ORDER BY rowid LIMIT 1", (c['id'], now)).fetchone()
                if not r:
                    if not db.execute("SELECT 1 FROM recipients WHERE campaign=? AND state='pending'", (c['id'],)).fetchone():
                        db.execute("UPDATE campaigns SET status='completed',reason='queue_finished' WHERE id=?", (c['id'],))
                    return None
                suppressed = db.execute('SELECT 1 FROM suppressed WHERE phone=?', (r['phone'],)).fetchone()
                recent = db.execute('SELECT 1 FROM recipients WHERE phone=? AND attempt_at>?', (r['phone'], now - 30*86400)).fetchone()
                if not r['consent'] or not r['evidence'] or suppressed or recent:
                    db.execute("UPDATE recipients SET state='suppressed',reason=? WHERE campaign=? AND phone=?",
                               ('no_consent' if not r['consent'] else 'suppressed_or_recent_campaign', c['id'], r['phone']))
                    continue
                db.execute("UPDATE recipients SET state='sending',attempt_at=? WHERE campaign=? AND phone=?", (now, c['id'], r['phone']))
                return {**dict(c), **dict(r), 'campaign': c['id'], 'attempt_at': now, 'state': 'sending'}

    def eligible(self, campaign, phone, now=None):
        now = time.time() if now is None else now
        if not WORK_START_HOUR <= datetime.fromtimestamp(now, TZ).hour < WORK_END_HOUR:
            return False
        with self.db() as db:
            return bool(db.execute('''SELECT 1 FROM recipients r JOIN campaigns c ON c.id=r.campaign
                WHERE c.id=? AND r.phone=? AND c.status='active' AND (c.expires=0 OR c.expires>?)
                AND r.state='sending' AND r.consent=1 AND r.evidence!=''
                AND NOT EXISTS(SELECT 1 FROM suppressed s WHERE s.phone=r.phone)''', (campaign, phone, now)).fetchone())

    def result(self, campaign, phone, state, message_id=None, reason='', now=None):
        if state not in ('accepted', 'uncertain', 'failed', 'skipped'):
            raise ValueError(state)
        now = time.time() if now is None else now
        with self.db() as db:
            # Preserve an immediate reply/opt-out arriving while HTTP was in flight.
            db.execute('''UPDATE recipients SET state=CASE WHEN state='sending' OR ? IN ('skipped','failed') THEN ? ELSE state END,
                       message_id=COALESCE(?,message_id),accepted_at=CASE WHEN ?='accepted' THEN ? ELSE accepted_at END,
                       reason=? WHERE campaign=? AND phone=?''', (state, state, message_id, state, now, reason, campaign, phone))
            if state in ('failed', 'uncertain'):
                db.execute("UPDATE campaigns SET status='paused',reason=? WHERE id=? AND status='active'", (reason or state, campaign))
        if message_id:
            with self.db() as db:
                receipt = db.execute('SELECT status FROM receipts WHERE message_id=?', (message_id,)).fetchone()
            if receipt:
                self.delivery(message_id, receipt['status'])

    def defer(self, campaign, phone, until, reason):
        """Return a definitely unsent reservation to the queue, without a quota cost."""
        with self.db() as db:
            changed = db.execute('''UPDATE recipients SET state='pending',attempt_at=NULL,reason=?,defer_until=?
                WHERE campaign=? AND phone=? AND state IN ('sending','replied') AND message_id IS NULL
                AND campaign IN (SELECT id FROM campaigns WHERE status IN ('active','paused'))
                AND NOT EXISTS(SELECT 1 FROM suppressed WHERE phone=recipients.phone)''',
                (reason, until, campaign, phone)).rowcount
            if changed:
                # Inbound arriving during a cancelled reservation was not a reply
                # to this campaign. Ordinary conversation history retains it.
                db.execute('DELETE FROM replies WHERE campaign=? AND phone=?', (campaign, phone))
        return bool(changed)

    def suppress(self, phone, reason, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO suppressed VALUES(?,?,?)', (phone, reason, now))
            db.execute("UPDATE recipients SET state='suppressed',reason=? WHERE phone=? AND state IN ('pending','sending')", (reason, phone))

    def reserve_refusal_ack(self, phone, incoming_id, now=None):
        """At most one acknowledgement per contact, including after a restart."""
        now = time.time() if now is None else now
        with self.db() as db:
            return bool(db.execute('''INSERT OR IGNORE INTO refusal_acks(phone,incoming_id,created,state)
                SELECT ?,?,?,'reserved' WHERE EXISTS(SELECT 1 FROM suppressed WHERE phone=?)''',
                (phone, incoming_id, now, phone)).rowcount)

    def refusal_ack_allowed(self, phone, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM refusal_acks WHERE phone=? AND state='reserved' AND created>?",
                                   (phone, now - 300)).fetchone())

    def finish_refusal_ack(self, phone, state, message_id=None):
        with self.db() as db:
            db.execute("UPDATE refusal_acks SET state=?,message_id=? WHERE phone=? AND state='reserved'",
                       (state, message_id, phone))

    def inbound(self, phone, text, message_id, now=None):
        now = time.time() if now is None else now
        context = self.context(phone)
        first_reply = False
        with self.db() as db:
            db.execute("UPDATE recipients SET defer_until=?,reason='client_contacted_before_campaign' WHERE phone=? AND state='pending'", (now + 86400, phone))
            if context:
                first_reply = not db.execute('SELECT 1 FROM replies WHERE campaign=? AND phone=? AND message_id!=?',
                                             (context['campaign'], phone, message_id)).fetchone()
                db.execute('''INSERT INTO replies VALUES(?,?,?,?,?) ON CONFLICT(campaign,phone,message_id)
                    DO UPDATE SET text=excluded.text''', (context['campaign'], phone, message_id, text, now))
                db.execute("UPDATE recipients SET state='replied' WHERE campaign=? AND phone=? AND state NOT IN ('suppressed','failed','skipped')", (context['campaign'], phone))
        # A bare "no" answers the invitation only on the first response. Later
        # it may answer a normal qualification question (e.g. prior experience).
        bare_no = text.strip().lower().strip('.! ') in ('нет', 'жоқ')
        declined = bool(context and ((DECLINE.fullmatch(text) and (not bare_no or first_reply))
                                    or CAMPAIGN_COMPLAINT.search(text)))
        if declined:
            self.suppress(phone, 'campaign_declined', now)
        return declined

    def context(self, phone):
        with self.db() as db:
            r = db.execute('''SELECT c.*,r.campaign,r.state,r.message_id,r.attempt_at,r.accepted_at,r.delivery
                FROM recipients r JOIN campaigns c ON c.id=r.campaign WHERE r.phone=?
                AND r.attempt_at IS NOT NULL AND r.state NOT IN ('failed','skipped','no_consent','cancelled')
                ORDER BY r.attempt_at DESC LIMIT 1''', (phone,)).fetchone()
            return dict(r) if r else None

    def delivery(self, message_id, status):
        if status not in ('sent', 'delivered', 'read', 'failed', 'notDelivered', 'yellowCard', 'suspended', 'noAccount'):
            return
        with self.db() as db:
            old = db.execute('SELECT status FROM receipts WHERE message_id=?', (message_id,)).fetchone()
            ranks = {'sent': 1, 'delivered': 2, 'read': 3}
            if old and ranks.get(status, 4) < ranks.get(old['status'], 4):
                status = old['status']
            db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?)', (message_id, status))
            row = db.execute('SELECT campaign,delivery FROM recipients WHERE message_id=?', (message_id,)).fetchone()
            if row:
                ranks = {'sent': 1, 'delivered': 2, 'read': 3}
                if ranks.get(status, 4) >= ranks.get(row['delivery'], 4 if row['delivery'] else 0):
                    db.execute('UPDATE recipients SET delivery=? WHERE message_id=?', (status, message_id))
                if status == 'noAccount':
                    # This recipient has no WhatsApp account; it is not an
                    # account-wide restriction. Keep the attempt, never retry it.
                    db.execute("UPDATE recipients SET state='failed',reason='delivery_noAccount' WHERE message_id=?", (message_id,))
                if status in ('failed', 'notDelivered', 'yellowCard', 'suspended'):
                    db.execute("UPDATE campaigns SET status='paused',reason=? WHERE id=? AND status='active'", ('delivery_' + status, row['campaign']))
        if status in ('yellowCard', 'suspended'):
            self.pause_all('provider_' + status)

    def report(self, campaign):
        with self.db() as db:
            c = db.execute('SELECT * FROM campaigns WHERE id=?', (campaign,)).fetchone()
            if not c:
                raise ValueError('Кампания не найдена')
            return {**dict(c), 'counts': dict(db.execute('SELECT state,COUNT(*) FROM recipients WHERE campaign=? GROUP BY state', (campaign,)).fetchall()),
                    'deliveries': dict(db.execute("SELECT COALESCE(delivery,'unknown'),COUNT(*) FROM recipients WHERE campaign=? AND message_id IS NOT NULL GROUP BY delivery", (campaign,)).fetchall()),
                    'suppressed_contacts': db.execute('SELECT COUNT(*) FROM recipients r JOIN suppressed s ON s.phone=r.phone WHERE r.campaign=?', (campaign,)).fetchone()[0],
                    'replied_contacts': db.execute('SELECT COUNT(DISTINCT phone) FROM replies WHERE campaign=?', (campaign,)).fetchone()[0]}


def dialog_context(campaign, now=None):
    if not campaign:
        return ''
    now = time.time() if now is None else now
    expired = bool(campaign['expires'] and now >= campaign['expires'])
    return ('\n[КОНТЕКСТ РАССЫЛКИ GM INTELLECT]\n'
            'Клиент получил/мог получить это предложение. Короткий отклик может относиться к нему; '
            'если клиент явно сменил тему, отвечай на его текущий вопрос. '
            'Если клиент недоволен именно рассылкой, раздражён нежелательной рекламой или отказывается '
            'получать предложения, сразу вызови decline_campaign. Это сохранит отказ и отправит одно '
            'короткое извинение; не уговаривай, не задавай вопросов и не вызывай другие инструменты. '
            'Не используй decline_campaign для жалобы на урок/оплату, вопроса о цене, отсутствия опыта '
            'или отказа от одного курса при интересе к другому. '
            'Не начинай заново продажу шахмат. «+» — интерес к диагностике, не бронь. '
            '«Камал»/«Аркада» — выбранный филиал. Уточни только недостающие имя/возраст/филиал. '
            'Для действующего ученика также обсуждай новые направления; для связи используй request_manager_callback. '
            'Для диагностики уточни выбор Аркада/Камал, даже если ребёнок сейчас учится в другом филиале. '
            'В reason обращения о диагностике обязательно укажи GM Intellect и выбранный филиал. '
            'Не утверждай, что место или время уже забронированы. Расписание и оставшиеся места подтверждает управляющий. '
            '55 000 тг — месячный пакет двух направлений; не обещай 15% и не складывай скидки. '
            'Продвинутые шахматы и совмещение тарифов рассчитывает администратор. '
            + ('Срок акции ИСТЁК: не предлагай прежние даты и не обещай прежние условия. '
               if expired else 'Точное время диагностики согласует управляющий; это не подтверждённая запись. ')
            + '\nУсловия кампании: ' + campaign['brief'] + '\nТочный текст предложения:\n' + campaign['message'])
