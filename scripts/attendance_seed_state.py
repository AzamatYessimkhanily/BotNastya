#!/usr/bin/env python3
"""Одноразовый seed attendance_state.json без WhatsApp (анти-бэклог перед продом)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["ATTENDANCE_SEED_ONLY"] = "1"
os.environ["ATTENDANCE_DRY_RUN"] = "0"
os.environ["ATTENDANCE_MONITOR_ENABLED"] = "1"

import bot as bot_mod  # noqa: E402
import attendance_monitor as am  # noqa: E402


async def main() -> None:
    am.ATTENDANCE_SEED_ONLY = True
    am.ATTENDANCE_DRY_RUN = False
    am._load_state()
    before_m, before_d = len(am._missed_sent), len(am._debt_sent)

    async def _noop(*_a, **_k):
        raise RuntimeError("seed must not dispatch WhatsApp")

    # window_sec влияет только на «не пришёл»; долг всё равно смотрит DEBT_LOOKBACK_MIN.
    # Не расширяем окно missed на часы — иначе seed часами долбит API.
    stats = await am.attendance_tick(
        crm=bot_mod.crm,
        base_url=bot_mod.MOYKLASS_BASE_URL,
        dispatch_client_message=_noop,
        get_trainer_phone=lambda **kw: bot_mod.crm.get_client_trainer_phone(**kw),
        get_admin_phone=bot_mod.crm.get_client_admin_phone,
        user_is_mailing_client=bot_mod.crm.user_is_mailing_client,
        get_user_phone=bot_mod.crm.get_user_phone_by_id,
        window_sec=None,
    )
    print("seed stats:", stats)
    print(
        f"state missed {before_m}->{len(am._missed_sent)} "
        f"debt {before_d}->{len(am._debt_sent)}"
    )
    print("SEED_OK")


if __name__ == "__main__":
    asyncio.run(main())
