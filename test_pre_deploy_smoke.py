#!/usr/bin/env python3
"""Быстрая проверка логики перед деплоем — без API и без WhatsApp."""
import importlib.util
import os
import sys
from pathlib import Path

# Минимальные заглушки, чтобы import bot.py не падал без .env
os.environ.setdefault("OPENAI_API_KEY", "sk-smoke-test")
os.environ.setdefault("GREEN_API_ID", "0")
os.environ.setdefault("GREEN_API_TOKEN", "smoke")
os.environ.setdefault("MOYKLASS_API_KEY", "smoke")

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("bot", ROOT / "bot.py")
bot = importlib.util.module_from_spec(spec)
# bot.py запускает load_dotenv и FastAPI при импорте — это нормально для smoke-теста
spec.loader.exec_module(bot)


def check(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)
    print(f"  OK  {name}")


def main() -> int:
    print("=== smoke: handoff / ack ===")
    check("хорошо = ack", bot._is_pure_acknowledgment("Хорошо"))
    check("для себя != ack", not bot._is_pure_acknowledgment("Для себя"))
    check("вопрос != ack", not bot._is_pure_acknowledgment("Хорошо, а когда звонок?"))
    check("handoff detect", bot._is_handoff_message(
        "Спасибо. Передаю вашу заявку нашему управляющему — она свяжется с вами."
    ))
    check("short sanitize", "Управляющий" in bot.sanitize_bot_outgoing(
        "Ок", fallback="short"
    ))
    check("handoff sanitize not same on short mode", "Передаю вашу заявку" not in bot.sanitize_bot_outgoing(
        "Ок", fallback="short"
    ))

    print("=== smoke: online link ===")
    check("url from comment", bot._extract_online_link_from_comment(
        "Zoom: https://zoom.us/j/123456789 pass"
    ) == "https://zoom.us/j/123456789")
    check("no url", bot._extract_online_link_from_comment("Очная группа Аркада") is None)
    check("www prefix", bot._extract_online_link_from_comment(
        "www.meet.google.com/abc-defg-hij"
    ).startswith("https://"))

    msg = bot.build_notification_message(
        "lesson_start_hours",
        {"beginTime": "18:00:00", "onlineLink": "https://zoom.us/j/test"},
    )
    check("lesson link appended", msg and "Ссылка на урок: https://zoom.us/j/test" in msg)
    check("time normalized", msg and "18:00" in msg)

    msg_no_link = bot.build_notification_message(
        "lesson_start_hours",
        {"beginTime": "10:00"},
    )
    check("no link when absent", msg_no_link and "Ссылка на урок" not in msg_no_link)

    pay = bot.build_notification_message("payment_new", {"userId": 1})
    check("payment unchanged", pay and "Ссылка на урок" not in pay)

    emp = bot.build_employee_notification_message(
        "lesson_changed",
        {"date": "2026-06-22", "beginTime": "19:00:00", "onlineLink": "https://zoom.us/j/emp"},
    )
    check("employee lesson_changed link", emp and "https://zoom.us/j/emp" in emp)

    debt = bot.build_employee_notification_message("sub_lesson_in_debt", {"userName": "Тест"})
    check("employee debt no link", debt and "Ссылка на урок" not in debt)

    print("=== smoke: enrich class helper ===")
    enriched = {}
    bot.MoyKlassCRM._apply_class_enrichment(enriched, {
        "beginTime": "15:30:00",
        "comment": "https://meet.google.com/xyz",
    })
    check("apply class beginTime", enriched.get("beginTime") == "15:30:00")
    check("apply class link", enriched.get("onlineLink") == "https://meet.google.com/xyz")

    print("\nВсе smoke-тесты пройдены.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nFAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
