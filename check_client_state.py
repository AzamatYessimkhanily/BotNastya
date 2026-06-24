#!/usr/bin/env python3
import os, httpx
from dotenv import load_dotenv
load_dotenv("/home/almalinux/BotNastya/.env")
b = "https://api.moyklass.com/v1/company"
with httpx.Client(timeout=30) as c:
    t = c.post(f"{b}/auth/getToken", json={"apiKey": os.getenv("MOYKLASS_API_KEY")}).json()["accessToken"]
    h = {"x-access-token": t}
    for uid in (3284384, 10466998):
        r = c.get(f"{b}/users", headers=h, params={"id": uid})
        u = (r.json().get("users") or [{}])[0]
        print(uid, u.get("name"), "clientStateId=", u.get("clientStateId"))
    r = c.get(f"{b}/users", headers=h, params={"clientStateId": 334199, "limit": 1})
    u = (r.json().get("users") or [{}])[0]
    print("lead sample", u.get("id"), u.get("name"), "clientStateId=", u.get("clientStateId"))
