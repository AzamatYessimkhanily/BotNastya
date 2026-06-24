#!/usr/bin/env python3
import json, os, httpx
from dotenv import load_dotenv
load_dotenv("/home/almalinux/BotNastya/.env")
BASE = "https://api.moyklass.com/v1/company"
with httpx.Client(timeout=30) as c:
    t = c.post(f"{BASE}/auth/getToken", json={"apiKey": os.getenv("MOYKLASS_API_KEY")}).json()["accessToken"]
    h = {"x-access-token": t}
    for uid in (3284384, 10466998):
        u = c.get(f"{BASE}/users/{uid}", headers=h).json()
        print("===", uid, "===")
        print("statusId", u.get("statusId"), "keys with status:", [k for k in u.keys() if "status" in k.lower()])
        for k in u:
            if "status" in k.lower():
                print(k, u.get(k))
    # users with client status filter?
    for params in ({"statusId": 168442, "limit": 3}, {"clientStatusId": 168442, "limit": 3}):
        r = c.get(f"{BASE}/users", headers=h, params=params)
        print("users params", params, "->", r.status_code, (r.text or "")[:300])
