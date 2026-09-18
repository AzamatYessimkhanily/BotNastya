"""
Монитор посещаемости MoyKlass → WhatsApp родителям.

1) Через ATTENDANCE_UNMARKED_AFTER_MIN после начала урока, если визит не отмечен
   (visit=false, не skip) — сообщение «не пришёл / не отмечен».
2) При visit=true: если это N-е посещение с начала месяца (по умолчанию 2) —
   сообщение «в долг».

Безопасность:
- обрабатываем только уроки, у которых порог (begin+N мин) только что наступил
  (скользящее окно тика) — без спама по утренним урокам при рестарте;
- дедуп в attendance_state.json;
- в тест-режиме (MOYKLASS_WEBHOOK_TEST_PHONE) все уходит на тест-номер.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Callable, Awaitable, Dict, List, Optional, Set, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger("bot")

_SCHOOL_TZ = ZoneInfo("Asia/Almaty")

_ATT_ENV = os.getenv("ATTENDANCE_MONITOR_ENABLED", "0").strip().lower()
ATTENDANCE_MONITOR_ENABLED = _ATT_ENV in ("1", "true", "yes", "on")
ATTENDANCE_CHECK_INTERVAL = int(os.getenv("ATTENDANCE_CHECK_INTERVAL", "60") or "60")
ATTENDANCE_UNMARKED_AFTER_MIN = int(os.getenv("ATTENDANCE_UNMARKED_AFTER_MIN", "10") or "10")
MONTHLY_DEBT_VISIT_N = int(os.getenv("MONTHLY_DEBT_VISIT_N", "2") or "2")
ATTENDANCE_STATE_FILE = os.getenv("ATTENDANCE_STATE_FILE", "attendance_state.json")
# Не трогаем уроки старше этого окна даже если порог «только что» (защита).
ATTENDANCE_LOOKBACK_HOURS = int(os.getenv("ATTENDANCE_LOOKBACK_HOURS", "6") or "6")
# Долг: смотрим только уроки, начавшиеся не раньше N минут назад (иначе тик тяжёлый).
ATTENDANCE_DEBT_LOOKBACK_MIN = int(os.getenv("ATTENDANCE_DEBT_LOOKBACK_MIN", "120") or "120")
# Потолок реальных отправок за один тик (защита от всплеска при тесте/рестарте).
ATTENDANCE_MAX_SEND_PER_TICK = int(os.getenv("ATTENDANCE_MAX_SEND_PER_TICK", "5") or "5")

_DRY = os.getenv("ATTENDANCE_DRY_RUN", "0").strip().lower()
ATTENDANCE_DRY_RUN = _DRY in ("1", "true", "yes", "on")
# Одноразовый прогон: пометить текущих кандидатов в state без WhatsApp (анти-бэклог перед продом).
_SEED = os.getenv("ATTENDANCE_SEED_ONLY", "0").strip().lower()
ATTENDANCE_SEED_ONLY = _SEED in ("1", "true", "yes", "on")

_state_lock = asyncio.Lock()
_missed_sent: Set[str] = set()
_debt_sent: Set[str] = set()


def _load_state() -> None:
    global _missed_sent, _debt_sent
    try:
        with open(ATTENDANCE_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        _missed_sent = set(data.get("missed") or [])
        _debt_sent = set(data.get("debt") or [])
        logger.info(
            "attendance: state loaded missed=%d debt=%d",
            len(_missed_sent), len(_debt_sent),
        )
    except FileNotFoundError:
        _missed_sent, _debt_sent = set(), set()
    except Exception as e:
        logger.warning("attendance: state load failed: %s", e)
        _missed_sent, _debt_sent = set(), set()


def _save_state() -> None:
    try:
        # ограничиваем рост файла
        missed = list(_missed_sent)[-4000:]
        debt = list(_debt_sent)[-2000:]
        with open(ATTENDANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"missed": missed, "debt": debt}, f)
        _missed_sent.clear()
        _missed_sent.update(missed)
        _debt_sent.clear()
        _debt_sent.update(debt)
    except Exception as e:
        logger.warning("attendance: state save failed: %s", e)


def _parse_lesson_dt(date_s: str, begin_s: str) -> Optional[datetime]:
    try:
        d = datetime.strptime((date_s or "")[:10], "%Y-%m-%d").date()
        parts = re.split(r"[:.]", (begin_s or "0:0").strip())
        hh = int(parts[0])
        mm = int(parts[1]) if len(parts) > 1 else 0
        return datetime(d.year, d.month, d.day, hh, mm, tzinfo=_SCHOOL_TZ)
    except Exception:
        return None


def _month_key(user_id: int, when: datetime) -> str:
    return f"{user_id}:{when.strftime('%Y-%m')}"


def _missed_key(lesson_id: int, user_id: int, date_s: str) -> str:
    return f"{lesson_id}:{user_id}:{date_s}"


def format_missed_message(trainer_phone: Optional[str]) -> str:
    """Текст «не пришёл» — с номером тренера, если он известен."""
    phone = (trainer_phone or "").strip()
    if phone:
        return (
            "Ученик не пришёл на урок или тренер не отметил посещение. "
            f"Свяжитесь с тренером {phone}"
        )
    return (
        "Ученик не пришёл на урок или тренер не отметил посещение. "
        "Напишите нам — разберёмся."
    )


def format_debt_message(admin_phone: Optional[str]) -> str:
    phone = (admin_phone or "").strip()
    if phone:
        return (
            "Занятие проведено в долг. "
            f"Для оплаты свяжитесь с администратором {phone}"
        )
    return (
        "Занятие проведено в долг. "
        "Для оплаты свяжитесь с администратором филиала."
    )


async def count_visits_this_month(crm, base_url: str, user_id: int, now: datetime) -> int:
    headers = await crm._get_headers()
    if not headers:
        return 0
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    count = 0
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base_url}/lessonRecords",
                headers=headers,
                params={
                    "userId": int(user_id),
                    "includeLessons": "true",
                    "limit": 200,
                    "sort": "id",
                    "sortDirection": "desc",
                },
            )
            if resp.status_code != 200:
                logger.warning("attendance: lessonRecords userId=%s -> %s", user_id, resp.status_code)
                return 0
            for rec in resp.json().get("lessonRecords") or []:
                if not rec.get("visit"):
                    continue
                lesson = rec.get("lesson") or {}
                date_s = lesson.get("date") or ""
                try:
                    d = datetime.strptime(date_s[:10], "%Y-%m-%d").replace(tzinfo=_SCHOOL_TZ)
                except Exception:
                    continue
                if d >= month_start:
                    count += 1
    except Exception as e:
        logger.warning("attendance: count visits userId=%s: %s", user_id, e)
    return count


async def fetch_lessons_for_day(crm, base_url: str, day: str) -> List[dict]:
    headers = await crm._get_headers()
    if not headers:
        return []
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base_url}/lessons",
                headers=headers,
                params={"date": [day, day], "limit": 200},
            )
            if resp.status_code != 200:
                logger.warning("attendance: lessons %s -> %s", day, resp.status_code)
                return []
            data = resp.json()
            return data.get("lessons") or (data if isinstance(data, list) else [])
    except Exception as e:
        logger.warning("attendance: lessons fetch: %s", e)
        return []


async def fetch_lesson_records(crm, base_url: str, lesson_id: int) -> List[dict]:
    headers = await crm._get_headers()
    if not headers:
        return []
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                f"{base_url}/lessonRecords",
                headers=headers,
                params={"lessonId": int(lesson_id), "limit": 200},
            )
            if resp.status_code != 200:
                return []
            return resp.json().get("lessonRecords") or []
    except Exception as e:
        logger.warning("attendance: lessonRecords lessonId=%s: %s", lesson_id, e)
        return []


async def attendance_tick(
    *,
    crm,
    base_url: str,
    dispatch_client_message: Callable[..., Awaitable[dict]],
    get_trainer_phone: Callable[..., Awaitable[str]],
    get_admin_phone: Callable[..., Awaitable[str]],
    user_is_mailing_client: Callable[..., Awaitable[bool]],
    get_user_phone: Callable[..., Awaitable[Optional[str]]],
    window_sec: Optional[float] = None,
) -> Dict[str, int]:
    """Один тик монитора. Возвращает счётчики для логов/тестов."""
    stats = {"lessons": 0, "missed_candidates": 0, "missed_sent": 0, "debt_sent": 0, "skipped": 0}
    now = datetime.now(_SCHOOL_TZ)
    today = now.strftime("%Y-%m-%d")
    win = float(window_sec if window_sec is not None else max(ATTENDANCE_CHECK_INTERVAL + 30, 90))
    window_start = now - timedelta(seconds=win)
    lookback = now - timedelta(hours=ATTENDANCE_LOOKBACK_HOURS)
    debt_lookback = now - timedelta(minutes=ATTENDANCE_DEBT_LOOKBACK_MIN)
    sends_left = max(0, ATTENDANCE_MAX_SEND_PER_TICK)

    lessons = await fetch_lessons_for_day(crm, base_url, today)
    stats["lessons"] = len(lessons)
    visits_cache: Dict[int, int] = {}
    client_cache: Dict[int, bool] = {}
    phone_cache: Dict[int, Optional[str]] = {}
    trainer_cache: Dict[Tuple[int, int], str] = {}

    for lesson in lessons:
        lesson_id = lesson.get("id")
        if lesson_id is None:
            continue
        begin_dt = _parse_lesson_dt(lesson.get("date") or today, lesson.get("beginTime") or "")
        if begin_dt is None or begin_dt < lookback:
            continue
        mark_at = begin_dt + timedelta(minutes=ATTENDANCE_UNMARKED_AFTER_MIN)
        due_missed = window_start <= mark_at <= now
        in_debt_window = begin_dt <= now and begin_dt >= debt_lookback

        if not due_missed and not in_debt_window:
            continue

        records = await fetch_lesson_records(crm, base_url, int(lesson_id))
        lid = int(lesson_id)

        for rec in records:
            user_id = rec.get("userId")
            if user_id is None:
                continue
            uid = int(user_id)
            if uid not in client_cache:
                client_cache[uid] = await user_is_mailing_client(uid)
            if not client_cache[uid]:
                stats["skipped"] += 1
                continue
            if uid not in phone_cache:
                phone_cache[uid] = await get_user_phone(uid)
            phone = phone_cache[uid]
            if not phone:
                stats["skipped"] += 1
                continue

            # --- missed / unmarked ---
            if due_missed and not rec.get("visit") and not rec.get("skip") and not rec.get("goodReason"):
                stats["missed_candidates"] += 1
                mkey = _missed_key(lid, uid, today)
                if mkey in _missed_sent:
                    continue
                tkey = (lid, uid)
                if tkey not in trainer_cache:
                    trainer_cache[tkey] = await get_trainer_phone(
                        user_id=uid,
                        class_id=lesson.get("classId"),
                        lesson_id=lid,
                    )
                trainer_phone = trainer_cache[tkey]
                msg = format_missed_message(trainer_phone)
                logger.info(
                    "attendance: MISSED userId=%s lessonId=%s phone=%s trainer=%s dry=%s seed=%s",
                    user_id, lesson_id, phone, trainer_phone or "-",
                    ATTENDANCE_DRY_RUN, ATTENDANCE_SEED_ONLY,
                )
                if ATTENDANCE_SEED_ONLY:
                    _missed_sent.add(mkey)
                    stats["missed_sent"] += 1
                    continue
                if sends_left <= 0 and not ATTENDANCE_DRY_RUN:
                    stats["skipped"] += 1
                    continue
                await dispatch_client_message(
                    event="attendance_unmarked",
                    obj={
                        "userId": user_id,
                        "lessonId": lesson_id,
                        "classId": lesson.get("classId"),
                        "trainer_phone": trainer_phone,
                    },
                    message=msg,
                    phones=[phone],
                )
                if not ATTENDANCE_DRY_RUN:
                    _missed_sent.add(mkey)
                    sends_left -= 1
                stats["missed_sent"] += 1

            # --- monthly debt on 2nd visit ---
            if in_debt_window and rec.get("visit"):
                dkey = _month_key(uid, now)
                if dkey in _debt_sent:
                    continue
                if uid not in visits_cache:
                    visits_cache[uid] = await count_visits_this_month(
                        crm, base_url, uid, now,
                    )
                visits = visits_cache[uid]
                if visits != MONTHLY_DEBT_VISIT_N:
                    continue
                admin_phone = await get_admin_phone(uid)
                msg = format_debt_message(admin_phone)
                logger.info(
                    "attendance: DEBT userId=%s visits=%s phone=%s dry=%s seed=%s",
                    user_id, visits, phone, ATTENDANCE_DRY_RUN, ATTENDANCE_SEED_ONLY,
                )
                if ATTENDANCE_SEED_ONLY:
                    _debt_sent.add(dkey)
                    stats["debt_sent"] += 1
                    continue
                if sends_left <= 0 and not ATTENDANCE_DRY_RUN:
                    stats["skipped"] += 1
                    continue
                await dispatch_client_message(
                    event="sub_lesson_in_debt",
                    obj={"userId": user_id, "lessonId": lesson_id, "admin_phone": admin_phone},
                    message=msg,
                    phones=[phone],
                )
                if not ATTENDANCE_DRY_RUN:
                    _debt_sent.add(dkey)
                    sends_left -= 1
                stats["debt_sent"] += 1

    if not ATTENDANCE_DRY_RUN or ATTENDANCE_SEED_ONLY:
        _save_state()
    return stats


async def attendance_loop(**kwargs) -> None:
    logger.info(
        "attendance: monitor started interval=%ss unmarked_after=%smin debt_n=%s dry=%s seed=%s",
        ATTENDANCE_CHECK_INTERVAL,
        ATTENDANCE_UNMARKED_AFTER_MIN,
        MONTHLY_DEBT_VISIT_N,
        ATTENDANCE_DRY_RUN,
        ATTENDANCE_SEED_ONLY,
    )
    _load_state()
    while True:
        try:
            stats = await attendance_tick(**kwargs)
            if stats["missed_sent"] or stats["debt_sent"] or stats["missed_candidates"]:
                logger.info("attendance: tick %s", stats)
            if ATTENDANCE_SEED_ONLY:
                logger.info("attendance: SEED done — stopping seed loop (set ATTENDANCE_SEED_ONLY=0)")
                return
        except Exception as e:
            logger.error("attendance: tick error: %s", e, exc_info=True)
            if ATTENDANCE_SEED_ONLY:
                return
        await asyncio.sleep(ATTENDANCE_CHECK_INTERVAL)
