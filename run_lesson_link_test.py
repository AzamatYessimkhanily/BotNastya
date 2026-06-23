#!/usr/bin/env python3
"""Тест: напоминание о занятии + ссылка из комментария группы → WhatsApp."""
import os
import sys
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv("/home/almalinux/BotNastya/.env")

API_KEY = os.getenv("MOYKLASS_API_KEY")
SECRET = os.getenv("MOYKLASS_WEBHOOK_SECRET")
TEST_LINK = "https://zoom.us/j/TEST-BOTNASTYA-LINK"
BASE = "https://api.moyklass.com/v1/company"
BOT = "http://127.0.0.1:8000"
CLASS_ID = int(os.getenv("TEST_CLASS_ID", "458952"))
TEST_USER_ID = int(os.getenv("TEST_USER_ID", "10466998"))


def main() -> int:
    with httpx.Client(timeout=30) as c:
        tok = c.post(f"{BASE}/auth/getToken", json={"apiKey": API_KEY}).json()["accessToken"]
        h = {"x-access-token": tok}

        cls = c.get(f"{BASE}/classes/{CLASS_ID}", headers=h).json()
        old_comment = (cls.get("comment") or "").strip()
        print(f"classId={CLASS_ID} name={cls.get('name')!r}")
        print(f"old_comment={old_comment!r}")

        if TEST_LINK not in old_comment:
            new_comment = TEST_LINK if not old_comment else f"{old_comment}\n{TEST_LINK}"
            resp = c.post(f"{BASE}/classes/{CLASS_ID}", headers=h, json={"comment": new_comment})
            print(f"set comment -> HTTP {resp.status_code}")
            if resp.status_code != 200:
                print(resp.text[:300])
                return 1
        else:
            print("comment already has test link")

        d1 = date.today().isoformat()
        d2 = (date.today() + timedelta(days=14)).isoformat()
        lessons_raw = c.get(f"{BASE}/lessons", headers=h, params={"classId": CLASS_ID, "date": d1, "dateEnd": d2, "limit": 5}).json()
        lessons = lessons_raw if isinstance(lessons_raw, list) else lessons_raw.get("lessons", [])
        lesson_id = lessons[0]["id"] if lessons else None
        print(f"lessonId={lesson_id}")

        import time
        today = date.today().isoformat()
        payload = {
            "event": "lesson_start_hours",
            "object": {
                "userId": TEST_USER_ID,
                "classId": CLASS_ID,
                "date": today,
                "beginTime": "23:30",
            },
            "time": int(time.time()),
        }

        url = f"{BOT}/moyklass-webhook/{SECRET}"
        r = c.post(url, json=payload)
        print(f"webhook -> HTTP {r.status_code} body={r.text}")
        print(f"event={payload['event']} object={payload['object']}")
        print("Проверь WhatsApp на MOYKLASS_WEBHOOK_TEST_PHONE")
        return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
