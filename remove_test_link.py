#!/usr/bin/env python3
"""Убрать тестовую Zoom-ссылку из комментария группы в MoyKlass."""
import os
import re
import sys

import httpx
from dotenv import load_dotenv

load_dotenv("/home/almalinux/BotNastya/.env")

API_KEY = os.getenv("MOYKLASS_API_KEY")
BASE = "https://api.moyklass.com/v1/company"
CLASS_ID = int(os.getenv("TEST_CLASS_ID", "458952"))
TEST_LINK = "https://zoom.us/j/TEST-BOTNASTYA-LINK"


def clean_comment(comment: str) -> str:
    text = (comment or "").strip()
    if not text:
        return ""
    text = text.replace(TEST_LINK, "")
    text = re.sub(r"https?://zoom\.us/j/TEST-BOTNASTYA-LINK", "", text, flags=re.I)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    return text


def main() -> int:
    if not API_KEY:
        print("MOYKLASS_API_KEY not set", file=sys.stderr)
        return 1

    with httpx.Client(timeout=30) as c:
        tok = c.post(f"{BASE}/auth/getToken", json={"apiKey": API_KEY}).json()["accessToken"]
        h = {"x-access-token": tok}

        cls = c.get(f"{BASE}/classes/{CLASS_ID}", headers=h).json()
        old = (cls.get("comment") or "").strip()
        new = clean_comment(old)

        print(f"classId={CLASS_ID} name={cls.get('name')!r}")
        print(f"old_comment={old!r}")
        print(f"new_comment={new!r}")

        if old == new:
            print("nothing to change")
            return 0

        resp = c.post(f"{BASE}/classes/{CLASS_ID}", headers=h, json={"comment": new})
        print(f"update -> HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:400], file=sys.stderr)
            return 1

        verify = c.get(f"{BASE}/classes/{CLASS_ID}", headers=h).json()
        print(f"verified_comment={verify.get('comment')!r}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
