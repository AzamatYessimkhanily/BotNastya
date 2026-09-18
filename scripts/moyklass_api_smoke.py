#!/usr/bin/env python3
"""Quick check that MoyKlass API works from this machine (uses .env)."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("MOYKLASS_API_KEY", "").strip()
BASE = "https://api.moyklass.com/v1/company"


def main() -> int:
    if not API_KEY:
        print("FAIL: MOYKLASS_API_KEY missing in .env")
        return 1
    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{BASE}/auth/getToken", json={"apiKey": API_KEY})
        r.raise_for_status()
        token = r.json().get("accessToken")
        if not token:
            print("FAIL: no accessToken", r.text[:200])
            return 1
        headers = {"x-access-token": token}
        users = client.get(f"{BASE}/users", headers=headers, params={"limit": 1, "sort": "id", "sortDirection": "desc"})
        users.raise_for_status()
        payload = users.json()
        sample = (payload.get("users") or [{}])[0]
        print("OK MoyKlass API")
        print(json.dumps({
            "token": "yes",
            "sample_user": {k: sample.get(k) for k in ("id", "name", "phone", "clientStateId", "filials")},
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
