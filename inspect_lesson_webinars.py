#!/usr/bin/env python3
"""Где в MoyKlass лежат ссылки на онлайн — урок, группа, тренер."""
import json
import os
import re
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv("/home/almalinux/BotNastya/.env")
BASE = "https://api.moyklass.com/v1/company"
URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def urls_in(obj) -> list:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and URL.search(v):
                found.append(f"{k}={v[:100]}")
            elif isinstance(v, (dict, list)):
                found.extend(urls_in(v))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(urls_in(item))
    return found


def main():
    with httpx.Client(timeout=30) as c:
        tok = c.post(f"{BASE}/auth/getToken", json={"apiKey": os.getenv("MOYKLASS_API_KEY")}).json()["accessToken"]
        h = {"x-access-token": tok}

        d1 = (date.today() - timedelta(days=7)).isoformat()
        d2 = (date.today() + timedelta(days=21)).isoformat()
        raw = c.get(f"{BASE}/lessons", headers=h, params={"date": d1, "dateEnd": d2, "limit": 200}).json()
        lessons = raw if isinstance(raw, list) else raw.get("lessons", [])

        print(f"=== LESSONS with any URL in JSON ({len(lessons)} scanned) ===")
        n = 0
        for les in lessons:
            hits = urls_in(les)
            if hits:
                n += 1
                print(f"\nlessonId={les.get('id')} classId={les.get('classId')} date={les.get('date')} {les.get('beginTime')}")
                for hit in hits[:8]:
                    print(f"  {hit}")
                if n >= 12:
                    break
        print(f"\nlessons_with_url={n}")

        print("\n=== SAMPLE online lesson FULL keys (first with 'онлайн' in class name) ===")
        classes_raw = c.get(f"{BASE}/classes", headers=h, params={"limit": 500}).json()
        classes = classes_raw if isinstance(classes_raw, list) else classes_raw.get("classes", [])
        online_ids = {cl["id"] for cl in classes if "онлайн" in (cl.get("name") or "").lower()}

        for les in lessons:
            if les.get("classId") in online_ids:
                lid = les["id"]
                detail = c.get(f"{BASE}/lessons/{lid}", headers=h).json()
                print(f"lessonId={lid} classId={detail.get('classId')}")
                print("top_keys:", sorted(detail.keys()))
                print("params:", json.dumps(detail.get("params") or {}, ensure_ascii=False)[:500])
                print("description:", repr((detail.get("description") or "")[:120]))
                print("urls:", urls_in(detail)[:10])
                break
        else:
            print("no online lesson in date range")

        print("\n=== GROUPS with comment or lessonSettings ===")
        for cl in classes:
            if "онлайн" not in (cl.get("name") or "").lower():
                continue
            cid = cl["id"]
            detail = c.get(f"{BASE}/classes/{cid}", headers=h).json()
            print(f"\nclassId={cid} name={detail.get('name')!r}")
            print(f"  comment={detail.get('comment')!r}")
            ls = detail.get("lessonSettings") or {}
            if ls:
                print(f"  lessonSettings={json.dumps(ls, ensure_ascii=False)[:300]}")


if __name__ == "__main__":
    main()
