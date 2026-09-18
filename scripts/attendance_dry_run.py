#!/usr/bin/env python3
"""Быстрый dry-run монитора (нормальное окно тика, без WhatsApp)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ATTENDANCE_DRY_RUN"] = "1"
os.environ["ATTENDANCE_MONITOR_ENABLED"] = "1"
os.environ["ATTENDANCE_STATE_FILE"] = "/tmp/attendance_state_dry.json"

import bot as bot_mod  # noqa: E402
import attendance_monitor as am  # noqa: E402


async def main() -> None:
    am.ATTENDANCE_DRY_RUN = True
    am._missed_sent.clear()
    am._debt_sent.clear()
    would = []

    async def _dispatch(event, obj, message, phones):
        would.append({"event": event, "phones": phones, "message": message, "obj": obj})
        print(f"WOULD event={event} phones={phones} msg={message[:100]}")
        return {"status": "dry"}

    stats = await am.attendance_tick(
        crm=bot_mod.crm,
        base_url=bot_mod.MOYKLASS_BASE_URL,
        dispatch_client_message=_dispatch,
        get_trainer_phone=lambda **kw: bot_mod.crm.get_client_trainer_phone(**kw),
        get_admin_phone=bot_mod.crm.get_client_admin_phone,
        user_is_mailing_client=bot_mod.crm.user_is_mailing_client,
        get_user_phone=bot_mod.crm.get_user_phone_by_id,
        # Нормальное окно: только уроки, у которых порог 10 мин только что наступил.
        window_sec=None,
    )
    print("stats:", stats)
    print("would_send_count:", len(would))
    print("OK dry-run finished; parents NOT messaged")


if __name__ == "__main__":
    asyncio.run(main())
