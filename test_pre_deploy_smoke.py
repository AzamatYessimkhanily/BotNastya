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

    print("=== smoke: sanitizer forbidden phrases (C2) ===")
    san = bot.sanitize_bot_outgoing("Напишите ещё раз или обратитесь к менеджеру напрямую.")
    check("'обратитесь ... напрямую' вырезается", "напрямую" not in san.lower())
    san2 = bot.sanitize_bot_outgoing(
        "Произошла техническая ошибка. Свяжитесь с нами напрямую по телефону."
    )
    check("оба запрещённых предложения вырезаны",
          "напрямую" not in san2.lower() and "ошибк" not in san2.lower())
    legal = bot.sanitize_bot_outgoing(
        "Передаю заявку управляющему. Её контакт: +7 778 104 8197. Хорошего дня."
    )
    check("легальный handoff не тронут", "8197" in legal)
    check("аварийный fallback диалога без запрещёнки",
          "напрямую" not in bot._DIALOG_ERROR_FALLBACK.lower()
          and "ошибк" not in bot._DIALOG_ERROR_FALLBACK.lower())

    print("=== smoke: repair dangling tool_call (C1) ===")

    class _FakeToolCall:
        def __init__(self, tid):
            self.id = tid

    class _FakeAssistantWithToolCalls:
        def __init__(self):
            self.role = "assistant"
            self.content = None
            self.tool_calls = [_FakeToolCall("call_1")]

    rep_id = "71111111111@c.us"
    bot.chat_history[rep_id] = [
        {"role": "system", "content": "p"},
        {"role": "user", "content": "x"},
        _FakeAssistantWithToolCalls(),
    ]
    bot._repair_dangling_tool_calls(rep_id)
    check("висячий tool_call удалён из истории",
          all(not bot._message_tool_call_ids(m) for m in bot.chat_history[rep_id]))
    bot.chat_history[rep_id] = [
        {"role": "user", "content": "x"},
        _FakeAssistantWithToolCalls(),
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]
    bot._repair_dangling_tool_calls(rep_id)
    check("валидная пара tool_call сохранена", len(bot.chat_history[rep_id]) == 3)

    # Починка старого orphan не должна терять свежее сообщение клиента (хвост).
    bot.chat_history[rep_id] = [
        {"role": "system", "content": "p"},
        _FakeAssistantWithToolCalls(),          # orphan без tool-ответа
        {"role": "user", "content": "новый вопрос"},
    ]
    bot._repair_dangling_tool_calls(rep_id)
    check("хвост (user) сохранён при починке orphan",
          bot.chat_history[rep_id][-1].get("content") == "новый вопрос")
    check("orphan вычищен, история валидна",
          all(not bot._message_tool_call_ids(m) for m in bot.chat_history[rep_id]))
    bot.chat_history.pop(rep_id, None)

    # json.loads вернул валидный JSON, но не object → нормализуем в {}.
    import json as _json
    for raw in ("null", "[]", "5", '"x"'):
        parsed = _json.loads(raw)
        normalized = parsed if isinstance(parsed, dict) else {}
        check(f"не-object аргументы {raw!r} -> dict", isinstance(normalized, dict))

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

    print("=== smoke: mailing safety M1/M2/M4 ===")
    import asyncio

    # M4: сверка телефона по последним 10 цифрам
    m = bot.MoyKlassCRM._phone_tail_matches
    check("M4 совпадение 7/8 формат", m("87053795459", "7053795459"))
    check("M4 совпадение +7 формат", m("+7 705 379 5459", "7053795459"))
    check("M4 чужой номер отвергнут", not m("77051112233", "7053795459"))
    check("M4 нет телефона -> доверяем", m("", "7053795459"))

    crm = bot.crm
    orig_state = crm._get_client_state_id
    orig_class = crm._class_mailing_client_user_ids
    orig_headers = crm._get_headers
    orig_phone = crm.get_user_phone_by_id
    try:
        async def _none_state():
            return None
        crm._get_client_state_id = _none_state
        # M1: clientStateId не определён → broadcast отменён (fail-closed)
        check("M1 class fail-closed", asyncio.run(
            crm._class_mailing_client_user_ids(123, {})) == [])
        check("M1 lesson fail-closed", asyncio.run(
            crm._lesson_mailing_client_user_ids(123, {})) == [])

        # M2: > лимита получателей → рассылка отменена
        async def _ok_headers():
            return {"x-access-token": "t"}
        async def _many(class_id, headers):
            return list(range(bot.MAX_BROADCAST_RECIPIENTS + 5))
        async def _phone(uid):
            return f"7700000{uid:04d}"
        crm._get_headers = _ok_headers
        crm._class_mailing_client_user_ids = _many
        crm.get_user_phone_by_id = _phone
        over = asyncio.run(crm.resolve_phones_for_webhook(
            {"classId": 7}, "class_start_hours"))
        check("M2 превышение лимита -> пусто", over == [])

        async def _few(class_id, headers):
            return [101, 102, 103]
        crm._class_mailing_client_user_ids = _few
        under = asyncio.run(crm.resolve_phones_for_webhook(
            {"classId": 7}, "class_start_hours"))
        check("M2 в пределах лимита -> рассылаем", len(under) == 3)
    finally:
        crm._get_client_state_id = orig_state
        crm._class_mailing_client_user_ids = orig_class
        crm._get_headers = orig_headers
        crm.get_user_phone_by_id = orig_phone

    print("=== smoke: send_whatsapp delivery check (M3) ===")

    class _FakeResp:
        def __init__(self, status, body):
            self.status_code = status
            self._body = body
            self.text = str(body)

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    class _FakeClient:
        def __init__(self, resp=None, exc=None):
            self._resp = resp
            self._exc = exc

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if self._exc:
                raise self._exc
            return self._resp

    def _factory(resp=None, exc=None):
        return lambda *a, **k: _FakeClient(resp, exc)

    orig_httpx_client = bot.httpx.AsyncClient
    msg = "Тестовое сообщение для проверки доставки через Green API."
    try:
        bot.httpx.AsyncClient = _factory(resp=_FakeResp(200, {"idMessage": "BAE5"}))
        check("M3 успех (idMessage) -> True",
              asyncio.run(bot.send_whatsapp("77000000000@c.us", msg)) is True)
        bot.httpx.AsyncClient = _factory(resp=_FakeResp(466, "quota exceeded"))
        check("M3 non-200 -> False",
              asyncio.run(bot.send_whatsapp("77000000000@c.us", msg)) is False)
        bot.httpx.AsyncClient = _factory(resp=_FakeResp(200, {}))
        check("M3 200 без idMessage -> False",
              asyncio.run(bot.send_whatsapp("77000000000@c.us", msg)) is False)
        bot.httpx.AsyncClient = _factory(exc=RuntimeError("network down"))
        check("M3 сетевая ошибка -> False (не падает)",
              asyncio.run(bot.send_whatsapp("77000000000@c.us", msg)) is False)
    finally:
        bot.httpx.AsyncClient = orig_httpx_client

    print("=== smoke: new-lead admin notify (any source, dedup) ===")

    # source-параметр меняет заголовок-источник
    bot_src = bot.build_new_lead_admin_message(
        name="A", phone="7700", age="-", experience="-", preference="онлайн", wa_link="-")
    check("source по умолчанию = бот", "Бот оформил заявку." in bot_src)
    crm_src = bot.build_new_lead_admin_message(
        name="A", phone="7700", age="-", experience="-", preference="онлайн",
        wa_link="-", source="Новый лид в CRM.")
    check("source переопределяется", "Новый лид в CRM." in crm_src)

    # dedup-хелперы
    bot._notified_lead_users.clear()
    check("неизвестный лид не уведомлён", not bot._lead_already_notified(555))
    bot._mark_lead_notified(555)
    check("после пометки — уведомлён", bot._lead_already_notified(555))
    check("None безопасен", not bot._lead_already_notified(None))

    crm = bot.crm
    orig_notify = crm.notify_branch_manager_new_lead
    orig_get_user = crm.get_user_by_id
    try:
        calls = []
        async def _spy_notify(mgr_phone, mgr_name, **kw):
            calls.append(kw)
        async def _fake_user(uid):
            return {"id": uid, "name": "Иван", "phone": "77011112233", "filials": [37754]}
        crm.notify_branch_manager_new_lead = _spy_notify
        crm.get_user_by_id = _fake_user

        # Уже уведомлён ботом → вебхук пропускает (нет двойного уведомления)
        bot._notified_lead_users.clear()
        bot._mark_lead_notified(999)
        asyncio.run(bot._handle_new_lead_admin_notification("join_new", {"userId": 999}))
        check("dedup: повторно НЕ уведомляем", len(calls) == 0)

        # Лид не от бота → уведомляем управляющего, source = CRM
        bot._notified_lead_users.clear()
        asyncio.run(bot._handle_new_lead_admin_notification("join_new", {"userId": 1001}))
        check("новый лид -> 1 уведомление", len(calls) == 1)
        check("source = Новый лид в CRM", calls[0].get("source") == "Новый лид в CRM.")
        check("имя из CRM", calls[0].get("name") == "Иван")
        check("после вебхука лид помечен", bot._lead_already_notified(1001))
    finally:
        crm.notify_branch_manager_new_lead = orig_notify
        crm.get_user_by_id = orig_get_user
        bot._notified_lead_users.clear()

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
