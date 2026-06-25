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

    print("=== smoke: handoff with tool-call object in history (regression) ===")

    class _FakeChatCompletionMessage:
        """Имитирует объект OpenAI без .get() — как реальный ChatCompletionMessage."""

        def __init__(self, content=None):
            self.role = "assistant"
            self.content = content
            self.tool_calls = []

    reg_chat_id = "70000000000@c.us"
    bot.chat_history[reg_chat_id] = [
        {"role": "system", "content": "prompt"},
        {"role": "user", "content": "Онлайн"},
        _FakeChatCompletionMessage(content=None),
        {"role": "tool", "name": "register_client_request", "content": "OK"},
    ]
    # До фикса здесь падало: 'ChatCompletionMessage' object has no attribute 'get'
    bot._mark_handoff_completed(reg_chat_id)
    check("mark_handoff не падает на объекте в истории", reg_chat_id in bot.handoff_completed)
    check("content из объекта истории безопасен",
          bot._history_message_content(_FakeChatCompletionMessage(content=None)) == "")
    check("content из dict истории безопасен",
          bot._history_message_content({"content": "abc"}) == "abc")
    bot.chat_history.pop(reg_chat_id, None)
    bot.handoff_completed.pop(reg_chat_id, None)

    print("=== smoke: new-lead admin notification ===")
    admin_msg = bot.build_new_lead_admin_message(
        name="Жасмин", phone="77001234567", age="10",
        experience="1 год", preference="онлайн",
        wa_link="https://wa.me/77001234567",
    )
    check("admin msg header", admin_msg.startswith("[НОВЫЙ ЛИД]"))
    check("admin msg has name", "Жасмин" in admin_msg)
    check("admin msg has phone", "77001234567" in admin_msg)
    check("admin msg has branch", "онлайн" in admin_msg)
    check("admin msg no raw placeholder", "{" not in admin_msg and "}" not in admin_msg)
    admin_msg_empty = bot.build_new_lead_admin_message(
        name="", phone="", age="", experience="", preference="", wa_link="",
    )
    check("admin msg empty -> dashes", admin_msg_empty.count("-") >= 5)

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

    # Регресс: отсутствующее поле не должно протекать сырым {плейсхолдером} клиенту.
    sub_missing = bot.build_notification_message("sub_end_days", {"userId": 1})
    check("no raw placeholder when endDate missing", sub_missing and "{" not in sub_missing and "}" not in sub_missing)
    sub_ok = bot.build_notification_message("sub_end_days", {"endDate": "2026-07-01"})
    check("endDate inserted", sub_ok and "2026-07-01" in sub_ok and "{" not in sub_ok)
    class_missing = bot.build_notification_message("class_start_days", {"userId": 1})
    check("class_start_days no raw placeholder", class_missing and "{" not in class_missing)
    emp_missing = bot.build_employee_notification_message("sub_end_days", {"userName": "Тест"})
    check("employee no raw placeholder", emp_missing and "{" not in emp_missing and "}" not in emp_missing)

    emp = bot.build_employee_notification_message(
        "lesson_changed",
        {"date": "2026-06-22", "beginTime": "19:00:00", "onlineLink": "https://zoom.us/j/emp"},
    )
    check("employee lesson_changed link", emp and "https://zoom.us/j/emp" in emp)
    check("employee lesson_changed text", emp and "графике занятий" in emp and "22.06.2026" in emp)

    emp_1h = bot.build_employee_notification_message(
        "lesson_start_hours",
        {"onlineLink": "https://zoom.us/j/t"},
    )
    check("employee 1h reminder", emp_1h and "через 1 час" in emp_1h and "zoom.us" in emp_1h)

    emp_5m = bot.build_employee_notification_message("lesson_start", {})
    check("employee 5m fallback no time", emp_5m and "через 5 минут" in emp_5m)

    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Almaty")
    in_5m = datetime.now(tz) + timedelta(minutes=5)
    emp_5m_timed = bot.build_employee_notification_message(
        "lesson_start",
        {"date": in_5m.strftime("%Y-%m-%d"), "beginTime": in_5m.strftime("%H:%M")},
    )
    check("employee 5m by time lesson_start", emp_5m_timed and "через 5 минут" in emp_5m_timed)

    in_1h = datetime.now(tz) + timedelta(hours=1)
    emp_1h_ls = bot.build_employee_notification_message(
        "lesson_start",
        {"date": in_1h.strftime("%Y-%m-%d"), "beginTime": in_1h.strftime("%H:%M")},
    )
    check("employee 1h by time lesson_start", emp_1h_ls and "через 1 час" in emp_1h_ls)

    bday = bot.build_employee_notification_message(
        "user_birthday", {"userName": "Азамат Тест"},
    )
    check("employee birthday", bday and "Азамат Тест" in bday)

    debt = bot.build_employee_notification_message("sub_lesson_in_debt", {"userName": "Тест"})
    check("employee debt no link", debt and "Ссылка на урок" not in debt)

    check("birthday requires client filter", "user_birthday" in bot._EVENTS_REQUIRE_ACTIVE_CLIENT)
    check("join_new exempt from client filter", "join_new" not in bot._EVENTS_REQUIRE_ACTIVE_CLIENT)

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
