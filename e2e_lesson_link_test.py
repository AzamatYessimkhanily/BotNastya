#!/usr/bin/env python3
"""E2E: тестовая группа + вебинар-ссылка в занятии → WhatsApp с ссылкой."""
import json
import os
import sys
import time
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv("/home/almalinux/BotNastya/.env")

API_KEY = os.getenv("MOYKLASS_API_KEY")
SECRET = os.getenv("MOYKLASS_WEBHOOK_SECRET")
BASE = "https://api.moyklass.com/v1/company"
BOT = "http://127.0.0.1:8000"
CLASS_ID = int(os.getenv("TEST_CLASS_ID", "458952"))
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "10466998"))
TEST_LINK = "https://zoom.us/j/BOTNASTYA-E2E-TEST-LINK"
TEST_PHONE = os.getenv("MOYKLASS_WEBHOOK_TEST_PHONE", "+77769969251")


def auth_headers(c: httpx.Client) -> dict:
    tok = c.post(f"{BASE}/auth/getToken", json={"apiKey": API_KEY}).json()["accessToken"]
    return {"x-access-token": tok}


def get_user(c: httpx.Client, h: dict, uid: int) -> dict:
    return c.get(f"{BASE}/users/{uid}", headers=h).json()


def ensure_join(c: httpx.Client, h: dict, class_id: int, user_id: int) -> None:
    joins_raw = c.get(f"{BASE}/joins", headers=h, params={"classId": class_id, "limit": 200}).json()
    joins = joins_raw if isinstance(joins_raw, list) else joins_raw.get("joins", [])
    if any(int(j.get("userId") or 0) == user_id for j in joins):
        print(f"  join OK: userId={user_id} уже в classId={class_id}")
        return
    payload = {"classId": class_id, "userId": user_id, "statusId": 2}  # «Учится»
    resp = c.post(f"{BASE}/joins", headers=h, json=payload)
    print(f"  POST /joins -> {resp.status_code} {resp.text[:200]}")


def pick_lesson(c: httpx.Client, h: dict, class_id: int, filial_id: int, room_id: int) -> dict:
    d1 = date.today().isoformat()
    d2 = (date.today() + timedelta(days=21)).isoformat()
    raw = c.get(
        f"{BASE}/lessons", headers=h, params={"classId": class_id, "date": d1, "dateEnd": d2, "limit": 10}
    ).json()
    lessons = raw if isinstance(raw, list) else raw.get("lessons", [])
    if lessons:
        return lessons[0]
    # создать занятие на сегодня вечером
    payload = {
        "classId": class_id,
        "filialId": filial_id,
        "roomId": room_id,
        "date": d1,
        "beginTime": "22:30",
        "endTime": "23:30",
    }
    resp = c.post(f"{BASE}/lessons", headers=h, json=payload)
    print(f"  POST /lessons -> {resp.status_code} {resp.text[:300]}")
    if resp.status_code not in (200, 201):
        raise SystemExit("не удалось создать занятие")
    return resp.json()


def get_room_id(c: httpx.Client, h: dict, filial_id: int) -> int:
    raw = c.get(f"{BASE}/rooms", headers=h, params={"filialId": filial_id, "limit": 20}).json()
    rooms = raw if isinstance(raw, list) else raw.get("rooms", [])
    if not rooms:
        raise SystemExit(f"нет аудиторий для filialId={filial_id}")
    return int(rooms[0]["id"])


def set_webinar_link(c: httpx.Client, h: dict, lesson_id: int, link: str) -> None:
    """MoyKlass API: params.webinars через POST не сохраняется — пишем в description занятия."""
    detail = c.get(f"{BASE}/lessons/{lesson_id}", headers=h).json()
    if link in (detail.get("description") or ""):
        print(f"  lesson {lesson_id} уже содержит ссылку")
        return
    payload = {"description": link}
    for field in ("date", "beginTime", "endTime", "classId", "filialId", "roomId"):
        if detail.get(field) is not None:
            payload[field] = detail[field]
    resp = c.post(f"{BASE}/lessons/{lesson_id}", headers=h, json=payload)
    print(f"  POST /lessons/{lesson_id} (description) -> {resp.status_code} {resp.text[:200]}")
    verify = c.get(f"{BASE}/lessons/{lesson_id}", headers=h).json()
    print(f"  verify description={verify.get('description')!r}")


def fire_webhook(c: httpx.Client, lesson: dict, user_id: int, class_id: int) -> None:
    today = date.today().isoformat()
    payload = {
        "event": "lesson_start_hours",
        "object": {
            "userId": user_id,
            "lessonId": lesson["id"],
            "classId": class_id,
            "date": lesson.get("date") or today,
            "beginTime": lesson.get("beginTime") or "23:30",
        },
        "time": int(time.time()),
    }
    url = f"{BOT}/moyklass-webhook/{SECRET}"
    r = c.post(url, json=payload)
    print(f"  client webhook -> HTTP {r.status_code} {r.text}")
    print(f"  object={json.dumps(payload['object'], ensure_ascii=False)}")


def main() -> int:
    print("=== E2E lesson link test ===")
    print(f"CLASS_ID={CLASS_ID} USER_ID={TEST_USER_ID} TEST_PHONE={TEST_PHONE}")
    with httpx.Client(timeout=45) as c:
        h = auth_headers(c)

        cls = c.get(f"{BASE}/classes/{CLASS_ID}", headers=h).json()
        print(f"\n1) Группа: id={CLASS_ID} name={cls.get('name')!r}")

        user = get_user(c, h, TEST_USER_ID)
        print(f"2) Ученик: id={TEST_USER_ID} name={user.get('name')!r} phone={user.get('phone')!r} clientStateId={user.get('clientStateId')}")

        print("3) Запись в группу...")
        ensure_join(c, h, CLASS_ID, TEST_USER_ID)

        print("4) Занятие + вебинар-ссылка...")
        filial_id = int(cls["filialId"])
        room_id = get_room_id(c, h, filial_id)
        lesson = pick_lesson(c, h, CLASS_ID, filial_id, room_id)
        print(f"   lessonId={lesson.get('id')} date={lesson.get('date')} begin={lesson.get('beginTime')}")
        set_webinar_link(c, h, int(lesson["id"]), TEST_LINK)

        print("5) Вебхук lesson_start_hours (клиентский)...")
        fire_webhook(c, lesson, TEST_USER_ID, CLASS_ID)

    print(f"\nГотово. Проверь WhatsApp на {TEST_PHONE}")
    print(f"Ожидаем: [ТЕСТ CRM] ... Ссылка на урок: {TEST_LINK}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
