#!/usr/bin/env python3
"""Список статусов учеников и записей в группу из MoyKlass API."""
import json
import os
import httpx
from dotenv import load_dotenv

load_dotenv("/home/almalinux/BotNastya/.env")
BASE = "https://api.moyklass.com/v1/company"
API_KEY = os.getenv("MOYKLASS_API_KEY")

with httpx.Client(timeout=30) as c:
    tok = c.post(f"{BASE}/auth/getToken", json={"apiKey": API_KEY}).json()["accessToken"]
    h = {"x-access-token": tok}
    for path in ("/joinStatuses", "/userStatuses", "/statuses", "/clientStatuses"):
        r = c.get(f"{BASE}{path}", headers=h)
        print(f"=== GET {path} -> {r.status_code} ===")
        if r.status_code == 200:
            data = r.json()
            items = data if isinstance(data, list) else data.get("statuses") or data.get("joinStatuses") or data
            if isinstance(items, list):
                for s in items:
                    print(json.dumps(s, ensure_ascii=False))
            else:
                print(json.dumps(data, ensure_ascii=False)[:2000])
        print()

    # sample users with different statusId
    r = c.get(f"{BASE}/users", headers=h, params={"limit": 5, "offset": 0})
    print("=== sample users statusId ===")
    if r.status_code == 200:
        for u in (r.json().get("users") or [])[:10]:
            print(u.get("id"), u.get("name"), "statusId=", u.get("statusId"))

    u = c.get(f"{BASE}/users/10466998", headers=h).json()
    print("=== test user 10466998 ===")
    print(json.dumps({k: u.get(k) for k in ("id", "name", "statusId", "phone")}, ensure_ascii=False))
    joins = u.get("joins") or []
    print("joins:", [(j.get("classId"), j.get("statusId")) for j in joins[:5]])
