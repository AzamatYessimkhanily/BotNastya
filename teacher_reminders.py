"""One attendance reminder per lesson/teacher, five minutes after the start."""
import asyncio
from contextlib import closing
from datetime import datetime, timedelta
import logging
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

import httpx
from attendance_monitor import _crm_rows, _parse_lesson_dt

logger = logging.getLogger('bot')
TZ = ZoneInfo('Asia/Almaty')
ENABLED = os.getenv('TEACHER_ATTENDANCE_REMINDERS_ENABLED', '0').lower() in ('1', 'true', 'yes', 'on')
STATE_FILE = os.getenv('TEACHER_ATTENDANCE_STATE_FILE', 'teacher_attendance.sqlite3')
MESSAGE = 'Важно! Отметьте посещение учеников'
DELAY = timedelta(minutes=5)
MAX_LATENESS = timedelta(minutes=10)
CHECK_INTERVAL = 30


class ReminderStore:
    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS reminders (
                    lesson INTEGER NOT NULL, teacher INTEGER NOT NULL, phone TEXT NOT NULL,
                    due REAL NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    message_id TEXT, updated REAL NOT NULL,
                    PRIMARY KEY (lesson, teacher), UNIQUE (lesson, phone));
            ''')
        os.chmod(self.path, 0o600)

    def start(self, now):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('enabled_since', ?)", (now,))
            # A crash may have happened after the provider accepted a send.
            db.execute("UPDATE reminders SET status='uncertain',reason='process_interrupted' WHERE status='sending'")
            return db.execute("SELECT value FROM metadata WHERE key='enabled_since'").fetchone()[0]

    def claim(self, lesson, teacher, phone, due, now):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM reminders WHERE lesson=? AND (teacher=? OR phone=?)',
                             (lesson, teacher, phone)).fetchone()
            if row and row[0] != 'deferred':
                return False
            if row:
                db.execute("DELETE FROM reminders WHERE lesson=? AND (teacher=? OR phone=?) AND status='deferred'",
                           (lesson, teacher, phone))
            db.execute('INSERT INTO reminders(lesson,teacher,phone,due,status,updated) VALUES(?,?,?,?,?,?)',
                       (lesson, teacher, phone, due, 'sending', now))
            return True

    def finish(self, lesson, teacher, result, now):
        status = result.get('state', 'uncertain')
        if status not in ('accepted', 'failed', 'uncertain', 'skipped', 'deferred'):
            status = 'uncertain'
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE reminders SET status=?,reason=?,message_id=?,updated=? WHERE lesson=? AND teacher=?',
                       (status, result.get('reason', ''), result.get('message_id'), now, lesson, teacher))


def normalized_phone(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    return digits if re.fullmatch(r'[1-9]\d{10,14}', digits) else None


def lesson_due(lesson, now, enabled_since):
    """Only scheduled lessons with pupils; visit=false also means absence, not unmarked."""
    if (type(lesson.get('id')) is not int or lesson['id'] <= 0
            or type(lesson.get('status')) is not int or lesson['status'] != 0
            or not isinstance(lesson.get('beginTime'), str)
            or not re.fullmatch(r'\d{2}:\d{2}', lesson['beginTime'])):
        return None
    start = _parse_lesson_dt(lesson.get('date'), lesson.get('beginTime'))
    if start is None:
        return None
    due = start + DELAY
    if due.timestamp() < enabled_since or not due <= now < due + MAX_LATENESS:
        return None
    records = lesson.get('records')
    if not isinstance(records, list) or not any(isinstance(r, dict) and r.get('skip') is False for r in records):
        return None
    return due


async def _read(client, base_url, path, headers, **params):
    response = await client.get(base_url + '/' + path, headers=headers, params=params)
    response.raise_for_status()
    return response.json()


async def reminder_tick(*, crm, base_url, store, enabled_since, dispatch, clock=None):
    """dispatch(phone, before_send) uses the bot's normal outbound limiter."""
    clock = clock or (lambda: datetime.now(TZ))
    now = clock()
    stats = {'lessons': 0, 'due': 0, 'accepted': 0, 'skipped': 0, 'deferred': 0, 'uncertain': 0, 'failed': 0}
    headers = await crm._get_headers()
    if not headers:
        raise ValueError('CRM authentication unavailable')
    earlier = (now - DELAY - MAX_LATENESS).date().isoformat()
    today = now.date().isoformat()
    dates = today if earlier == today else earlier + ',' + today
    async with httpx.AsyncClient(timeout=15.0) as client:
        lessons = await _crm_rows(client, base_url, headers, 'lessons', 'lessons',
                                  {'date': dates, 'includeRecords': 'true'})
        stats['lessons'] = len(lessons)
        candidates = [(lesson, lesson_due(lesson, now, enabled_since)) for lesson in lessons]
        candidates = [(lesson, due) for lesson, due in candidates if due is not None]
        if not candidates:
            return stats
        managers = await _read(client, base_url, 'managers', headers)
        if not isinstance(managers, list):
            raise ValueError('Invalid CRM staff list')
        by_id = {m.get('id'): m for m in managers if isinstance(m, dict)}
        for lesson, due in sorted(candidates, key=lambda row: row[1]):
            stats['due'] += 1
            teacher_ids = lesson.get('teacherIds')
            if not isinstance(teacher_ids, list) or not teacher_ids:
                stats['skipped'] += 1
                logger.warning('teacher-attendance: no assigned teacher lesson=%s', lesson['id'])
                continue
            for teacher in teacher_ids:
                manager = by_id.get(teacher, {})
                phone = normalized_phone(manager.get('phone'))
                if type(teacher) is not int or not phone or manager.get('isWork') is not True:
                    stats['skipped'] += 1
                    continue
                if not store.claim(lesson['id'], teacher, phone, due.timestamp(), clock().timestamp()):
                    continue
                guard = {'reason': 'verification_unavailable'}

                async def before_send():
                    # Called after the send limiter: a cancelled, rescheduled,
                    # completed or reassigned lesson must not notify the old coach.
                    guard['reason'] = 'verification_unavailable'
                    try:
                        fresh_headers = await crm._get_headers()
                        if not fresh_headers:
                            return False
                        fresh = await _read(client, base_url, 'lessons/' + str(lesson['id']), fresh_headers,
                                            includeRecords='true')
                        fresh_staff = await _read(client, base_url, 'managers', fresh_headers)
                        current = next((m for m in fresh_staff if isinstance(m, dict) and m.get('id') == teacher), {})
                        valid = (lesson_due(fresh, clock(), enabled_since) is not None
                                 and teacher in (fresh.get('teacherIds') or [])
                                 and current.get('isWork') is True
                                 and normalized_phone(current.get('phone')) == phone)
                        guard['reason'] = '' if valid else 'lesson_or_teacher_changed'
                        return valid
                    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                        return False

                try:
                    result = await dispatch(phone, before_send)
                except BaseException:
                    store.finish(lesson['id'], teacher, {'state': 'uncertain', 'reason': 'dispatch_interrupted'}, clock().timestamp())
                    raise
                if result.get('state') == 'not_sent':
                    # Proven unsent can be reconsidered at a rescheduled start
                    # or after a staff contact correction; accepted sends cannot.
                    result = {'state': 'deferred',
                              'reason': guard['reason'] or 'outbound_unavailable'}
                store.finish(lesson['id'], teacher, result, clock().timestamp())
                status = result.get('state', 'uncertain')
                stats[status if status in stats else 'uncertain'] += 1
    return stats


async def reminder_loop(**kwargs):
    store = ReminderStore(STATE_FILE)
    enabled_since = store.start(datetime.now(TZ).timestamp())
    logger.info('teacher-attendance: started delay=5min interval=%ss', CHECK_INTERVAL)
    while True:
        try:
            stats = await reminder_tick(store=store, enabled_since=enabled_since, **kwargs)
            logger.info('teacher-attendance: tick %s', stats)
        except Exception as error:
            logger.warning('teacher-attendance: tick failed (%s)', type(error).__name__)
        await asyncio.sleep(CHECK_INTERVAL)
