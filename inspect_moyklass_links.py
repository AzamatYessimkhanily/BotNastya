#!/usr/bin/env python3
"""Разовый аудит: где в MoyKlass лежат ссылки на онлайн-урок."""
import os
import re
import json
import httpx
from dotenv import load_dotenv

load_dotenv("/home/almalinux/BotNastya/.env")
api = os.getenv("MOYKLASS_API_KEY")
base = "https://api.moyklass.com/v1/company"
URL = re.compile(r"https?://[^\s<>\"']+", re.I)


def has_url(s):
    return bool(s and URL.search(str(s)))


def main():
    with httpx.Client(timeout=30) as c:
        tok = c.post(f"{base}/auth/getToken", json={"apiKey": api}).json()["accessToken"]
        h = {"x-access-token": tok}
        raw = c.get(f"{base}/classes", headers=h, params={"limit": 500}).json()
        classes = raw if isinstance(raw, list) else raw.get("classes", [])

        print("=== GROUPS with URL in comment ===")
        found = 0
        for cl in classes:
            com = (cl.get("comment") or "").strip()
            if has_url(com):
                found += 1
                print(f"id={cl.get('id')} name={cl.get('name')!r}")
                print(f"  comment={com[:150]}")
        print(f"scanned={len(classes)} with_url={found}\n")

        print("=== binom / online / kamal (name match) ===")
        for cl in classes:
            n = (cl.get("name") or "").lower()
            if any(k in n for k in ("binom", "онлайн", "online", "камал", "zoom")):
                com = (cl.get("comment") or "").strip()
                print(f"id={cl.get('id')} name={cl.get('name')!r} comment={com[:150]!r}")

        print("\n=== STAFF with URL in comment/description ===")
        mgrs = c.get(f"{base}/managers", headers=h).json() or []
        sf = 0
        for m in mgrs:
            for field in ("comment", "description", "shortDescription", "additionalContacts"):
                val = (m.get(field) or "").strip()
                if has_url(val):
                    sf += 1
                    print(f"id={m.get('id')} name={m.get('name')!r} {field}={val[:150]}")
                    break
        print(f"managers={len(mgrs)} with_url={sf}")

        print("\n=== RECENT lessons with URL in description/params (sample) ===")
        from datetime import date, timedelta
        d1 = (date.today() - timedelta(days=7)).isoformat()
        d2 = (date.today() + timedelta(days=14)).isoformat()
        lessons_raw = c.get(
            f"{base}/lessons", headers=h, params={"date": d1, "dateEnd": d2, "limit": 100}
        ).json()
        lessons = lessons_raw if isinstance(lessons_raw, list) else lessons_raw.get("lessons", [])
        lf = 0
        for les in lessons:
            desc = (les.get("description") or "").strip()
            params = les.get("params") or {}
            blob = desc + " " + json.dumps(params, ensure_ascii=False)
            if has_url(blob):
                lf += 1
                print(
                    f"lessonId={les.get('id')} classId={les.get('classId')} "
                    f"date={les.get('date')} begin={les.get('beginTime')}"
                )
                print(f"  description={desc[:120]!r}")
                if params:
                    print(f"  params_keys={list(params.keys())}")
            if lf >= 10:
                break
        print(f"lessons_sample={len(lessons)} with_url={lf}")

        print("\n=== Search 'binom3' / individual groups ===")
        for cl in classes:
            n = (cl.get("name") or "").lower()
            if "binom3" in n or "индивидуальное" in n and "binom" in n:
                cid = cl.get("id")
                detail = c.get(f"{base}/classes/{cid}", headers=h).json()
                print(f"id={cid} name={detail.get('name')!r}")
                print(f"  comment={detail.get('comment')!r}")
                print(f"  managerIds={detail.get('managerIds')}")
                for mid in (detail.get("managerIds") or [])[:3]:
                    m = c.get(f"{base}/managers/{mid}", headers=h).json()
                    print(f"  manager {mid} {m.get('name')!r} comment={m.get('comment')!r}")


if __name__ == "__main__":
    main()
