# Handoff: что сделал Claude в сессии 19.06.2026

## Контекст

Проект: BotNastya — WhatsApp-бот шахматной школы GMCA (Python 3, FastAPI, один файл `bot.py`).
Ветка разработки: `feature/crm-scenario-notifications`.
Сервер: `almalinux@82.115.48.37`, systemd `bot.service`, порт 8000.

До этой сессии: Cursor реализовал CRM-интеграцию (MoyKlass → WhatsApp). 19.06 произошёл инцидент — рассылка 14 клиентам. Фиксы были задеплоены вручную на сервер, но не закоммичены в git.

---

## Что сделано в этой сессии (хронологически)

### 1. Анализ кода по ТЗ

Проведён полный аудит `bot.py` (2019 строк) против `TZ_CRM_NOTIFICATIONS.md`. Все 14 событий покрыты шаблонами, структура обработки соответствует ТЗ. Найден баг `class_start_hours` (см. ниже).

### 2. Коммит незакоммиченных фиксов инцидента (commit `10d4a79`)

**Что было не закоммичено (~469 строк diff):**
- `from datetime import date` + `from zoneinfo import ZoneInfo` — импорты для timezone
- `_SCHOOL_TZ = ZoneInfo("Asia/Almaty")` — таймзона школы
- `MOYKLASS_HTTP_TIMEOUT = 30.0` — таймаут всех CRM API вызовов
- `MOYKLASS_WEBHOOK_ENABLED_SINCE` — env-var для защиты от старых вебхуков
- **Рефактор `get_user_by_id`** (критичный баг из инцидента):
  - До: `get_user_phone_by_id` делал fallback `users[0].phone` без проверки id → отправка на чужой номер
  - После: выделена отдельная функция `get_user_by_id(user_id)`, fallback итерирует список и проверяет `user.id == user_id`
- **`_normalize_begin_time`**: `"10:00:00"` → `"10:00"` (до этого `strptime("%H:%M")` падал)
- **`_scheduled_event_is_past`**: fail-closed когда нет date/beginTime для today-событий
- **`enrich_webhook_object_context`** вызывается ДО skip-проверки в `handle_moyklass_webhook`
- `try/except` на обоих webhook-хендлерах → всегда возвращают 200 → MoyKlass не делает ретраи

**Файлы:** `bot.py`, `.env.example` (документация новых env-vars).

### 3. Баг `class_start_hours` — найден и исправлен (commit `b1f17eb`)

**Проблема:** Событие `class_start_hours` от MoyKlass содержит только `classId` — без `date` и `beginTime` в payload. В `_scheduled_event_is_past`:
```python
if not date_str:
    return event in _TODAY_LESSON_EVENTS  # True → всегда пропускалось
```
`class_start_hours` в `_TODAY_LESSON_EVENTS` → событие всегда скипалось в продакшне.

**Фикс 1** — `enrich_webhook_object_context` теперь тоже обрабатывает `classId`:
```python
# bot.py строки ~1061-1079
if obj.get("classId") is not None and not enriched.get("beginTime"):
    # GET /classes/{classId} → enriched.setdefault("beginTime", ...)
```

**Фикс 2** — `_scheduled_event_is_past` (строки ~1826-1838):
```python
if not date_str:
    if event == "class_start_hours":
        # По определению «сегодня». Берём beginTime из enrich.
        # Без beginTime — fail-closed (не отправляем).
        if not time_str:
            return True
        try:
            scheduled = datetime.strptime(f"{today.isoformat()} {time_str}", "%Y-%m-%d %H:%M")
            return scheduled < now_local
        except ValueError:
            return True
    return event in _TODAY_LESSON_EVENTS
```

### 4. Curl-тест скрипт (commit `b1f17eb`, исправлен в `c14739f`)

Создан `test_crm_webhooks.sh` — 24 теста:
- 17 клиентских событий + BONUS `user_birthday`
- NEG-1: `unknown_event` → нет отправки
- NEG-2: `join_changed_state` statusId=99 → нет шаблона
- NEG-3: `lesson_start` с `beginTime=00:01` → skip (прошедшее)
- NEG-4: неверный секрет → HTTP 404
- EMP-1..4: события сотрудников

**Важно:** тест использует `userId=10466998` (Азамат Тест), НЕ `userId=10461950` (Шера).
Причина: userId 10461950 — это аккаунт менеджера/администратора, не клиента. `GET /users/10461950` возвращает `{"code": ...}` без полей пользователя. `GET /users/10466998` возвращает полный профиль с `phone='77053795459'`.

### 5. Деплой на сервер (SSH)

Сервер оказался на ветке `main`, а не `feature/crm-scenario-notifications`. Git-credentials на сервере не настроены (нет возможности `git pull`). Bot.py на сервере содержит CRM-код, задеплоенный вручную ранее (с коммитами `main` + ручные правки поверх).

**Что сделано на сервере:**
1. Применён Python-патч `/tmp/patch_bot.py` — добавил фикс `class_start_hours` в `bot.py` на сервере (бэкап: `bot.py.bak.patch`)
2. Обновлён `.env`:
   - `MOYKLASS_WEBHOOK_TEST_PHONE=+77769969251` (тест-режим: всё только на номер Шеры)
   - `MOYKLASS_WEBHOOK_ENABLED_SINCE=1781853451` (новый timestamp, защита от старых вебхуков)
3. Перезапущен `bot.service` → `active (running)`
4. Запущен тест-скрипт

### 6. Результат тестов на сервере

Все 24 теста прошли. По логам — 18 реальных отправок на `77769969251@c.us`:

```
moyklass-webhook: ТЕСТОВЫЙ РЕЖИМ — вместо ['77053795459'] отправляем только на +77769969251
moyklass-webhook: отправлено 77769969251@c.us
```

Негативные тесты:
- `unknown_event` → `нет шаблона, пропускаем` ✅
- `join_changed_state statusId=99` → `нет шаблона, пропускаем` ✅
- `lesson_start beginTime=00:01` → `пропущен (занятие в прошлом)` ✅
- Неверный секрет → HTTP 404 ✅

---

## Текущее состояние

### Git (local)
```
feature/crm-scenario-notifications @ c14739f
  c14739f fix: use correct test userId in curl test script
  b1f17eb fix: class_start_hours stale-check always skipped + curl test script
  10d4a79 fix: incident post-mortem — stale webhook guard, userId lookup, timeouts
  c97120f feat: employee webhooks, client disable flag, and CRM test mode  ← был до сессии
```

### Сервер `.env` (после сессии)
```
MOYKLASS_CLIENT_WEBHOOKS_ENABLED=0        ← клиентские WhatsApp выкл (kill-switch)
MOYKLASS_WEBHOOK_TEST_PHONE=+77769969251  ← тест-режим ВКЛЮЧЁН
MOYKLASS_WEBHOOK_ENABLED_SINCE=1781853451 ← обновлён при рестарте
```

### Сервер — git vs working tree
**Важно:** сервер на `main`, но `bot.py` содержит ручные правки поверх main (CRM-код + патч из этой сессии). Эти изменения НЕ в git на сервере. Git-credentials не настроены → `git pull` не работает.

---

## Что нужно сделать дальше

### Приоритет 1 — Подтвердить WhatsApp-сообщения
Шера должна проверить ~22 сообщения с `[ТЕСТ CRM]` на номере +77769969251. Если тексты Ok → переходим к включению.

### Приоритет 2 — Включить реальные уведомления
На сервере в `.env`:
```bash
# Убрать тест-режим:
MOYKLASS_WEBHOOK_TEST_PHONE=

# Включить клиентские:
MOYKLASS_CLIENT_WEBHOOKS_ENABLED=1

# Обновить ENABLED_SINCE перед рестартом:
date +%s  # вставить результат ниже
MOYKLASS_WEBHOOK_ENABLED_SINCE=<новый_timestamp>
```
Затем `sudo systemctl restart bot.service`.

### Приоритет 3 — Синхронизировать git на сервере
Сервер на `main`, а весь CRM-код — в `feature/crm-scenario-notifications`. Варианты:
- Настроить git credentials на сервере и переключиться на feature-ветку
- Или после merge feature→main сделать `git pull origin main` на сервере

### Приоритет 4 — Merge в main (после ОК заказчика)
Ветка `feature/crm-scenario-notifications` → PR → merge в `main`.

### Приоритет 5 — KZ шаблоны (отложено)
Казахские варианты всех шаблонов + логика определения языка (см. раздел 3 ТЗ).

---

## Ключевые места в коде

| Что | Где |
|---|---|
| Env-vars CRM | `bot.py:33-42` |
| `enrich_webhook_object_context` (classId фикс) | `bot.py:1034-1081` |
| `_NOTIFICATION_TEMPLATES` | `bot.py:1608-1623` |
| `build_notification_message` | `bot.py:1626-1651` |
| `_scheduled_event_is_past` (class_start_hours фикс) | `bot.py:1799-1843` |
| `handle_moyklass_webhook` (клиенты) | `bot.py:1916-1980` |
| `handle_moyklass_webhook_employee` (сотрудники) | `bot.py:1983-2019` |
| Тест-скрипт | `test_crm_webhooks.sh` |

## Известные риски (не блокеры)

1. `send_whatsapp()` не проверяет ответ Green API — тихие сбои доставки
2. `chat_history` in-memory — контекст ТИП 9 теряется при рестарте
3. `lesson_start` / `class_start_hours` без userId — до 200 получателей (сейчас kill-switch защищает)
4. Сервер на `main` с ручными правками — рассинхрон с git
