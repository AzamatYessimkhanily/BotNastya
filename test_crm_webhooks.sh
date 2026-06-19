#!/bin/bash
# ============================================================
# CRM webhook integration test — BotNastya
# Запускать после: MOYKLASS_WEBHOOK_TEST_PHONE установлен на сервере
# Все сообщения придут на TEST_PHONE с префиксом [ТЕСТ CRM]
#
# Использование:
#   chmod +x test_crm_webhooks.sh
#   ./test_crm_webhooks.sh
# ============================================================

SERVER="http://82.115.48.37:8000"
SECRET="1c110b8a-6020-4e31-8eff-3612aa0c55ef"
USER_ID=10461950        # Шера — тестовый аккаунт заказчика
TODAY=$(date +%Y-%m-%d)
TOMORROW=$(date -d "tomorrow" +%Y-%m-%d 2>/dev/null || date -v+1d +%Y-%m-%d)
FUTURE_TIME="23:30"     # Время в будущем — чтобы stale-check не пропустил

CLIENT_URL="$SERVER/moyklass-webhook/$SECRET"
EMPLOYEE_URL="$SERVER/moyklass-webhook-employee/$SECRET"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

pass() { echo -e "${GREEN}✅ $1${NC}"; }
info() { echo -e "${YELLOW}▶  $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; }

send() {
    local label="$1"
    local url="$2"
    local body="$3"
    info "$label"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$url" \
        -H "Content-Type: application/json" \
        -d "$body")
    if [ "$code" = "200" ]; then
        pass "HTTP $code — проверь WhatsApp"
    else
        fail "HTTP $code"
    fi
    echo ""
    sleep 2   # пауза чтобы не захлестнуть Green API
}

echo ""
echo "======================================================"
echo "  CRM WEBHOOK TEST  |  тест-телефон получает всё"
echo "======================================================"
echo "  Сервер : $SERVER"
echo "  UserID : $USER_ID (Шера)"
echo "  Сегодня: $TODAY  |  Завтра: $TOMORROW"
echo "======================================================"
echo ""

# ----------------------------------------------------------
# БЛОК 1 — CLIENT WEBHOOK (клиентские уведомления)
# ----------------------------------------------------------
echo "--- КЛИЕНТСКИЕ УВЕДОМЛЕНИЯ ---"
echo ""

# 1. payment_new
send "1/17 payment_new — оплата получена" \
    "$CLIENT_URL" \
    "{\"event\":\"payment_new\",\"object\":{\"userId\":$USER_ID,\"summa\":30000,\"date\":\"$TODAY\"},\"time\":$(date +%s)}"

# 2. sub_end_days
send "2/17 sub_end_days — абонемент заканчивается" \
    "$CLIENT_URL" \
    "{\"event\":\"sub_end_days\",\"object\":{\"userId\":$USER_ID,\"endDate\":\"2026-07-15\"},\"time\":$(date +%s)}"

# 3. sub_days_next_payment
send "3/17 sub_days_next_payment — предстоящий платёж" \
    "$CLIENT_URL" \
    "{\"event\":\"sub_days_next_payment\",\"object\":{\"userId\":$USER_ID,\"remindDate\":\"2026-07-10\",\"remindSumm\":30000},\"time\":$(date +%s)}"

# 4. sub_lesson_in_debt
send "4/17 sub_lesson_in_debt — занятие в долг" \
    "$CLIENT_URL" \
    "{\"event\":\"sub_lesson_in_debt\",\"object\":{\"userId\":$USER_ID,\"lessonId\":99999},\"time\":$(date +%s)}"

# 5. sub_lessons_left
send "5/17 sub_lessons_left — мало занятий в абонементе" \
    "$CLIENT_URL" \
    "{\"event\":\"sub_lessons_left\",\"object\":{\"userId\":$USER_ID,\"visitCount\":10,\"visitedCount\":8},\"time\":$(date +%s)}"

# 6. lesson_start (alias — CRM шлёт это событие вместо lesson_start_hours)
#    ВАЖНО: включаем userId + дату/время СЕГОДНЯ в будущем, иначе stale-check пропустит
send "6/17 lesson_start — сегодня занятие (alias CRM)" \
    "$CLIENT_URL" \
    "{\"event\":\"lesson_start\",\"object\":{\"userId\":$USER_ID,\"date\":\"$TODAY\",\"beginTime\":\"$FUTURE_TIME\"},\"time\":$(date +%s)}"

# 7. lesson_start_hours — стандартный формат ТЗ
send "7/17 lesson_start_hours — занятие через N часов" \
    "$CLIENT_URL" \
    "{\"event\":\"lesson_start_hours\",\"object\":{\"userId\":$USER_ID,\"date\":\"$TODAY\",\"beginTime\":\"$FUTURE_TIME\"},\"time\":$(date +%s)}"

# 8. lesson_start_days — занятие завтра (stale не сработает — дата в будущем)
send "8/17 lesson_start_days — занятие через N дней" \
    "$CLIENT_URL" \
    "{\"event\":\"lesson_start_days\",\"object\":{\"userId\":$USER_ID,\"date\":\"$TOMORROW\"},\"time\":$(date +%s)}"

# 9. class_start_hours
#    ⚠️  ВНИМАНИЕ: в реальном payload от MoyKlass нет date/beginTime для этого события.
#    Без них stale-check всегда пропускает (fail-closed). В этом тесте передаём вручную.
#    В продакшне нужно либо: MoyKlass сам шлёт date, либо доработать enrich для classId.
send "9/17 class_start_hours — старт группы сегодня [⚠️ см. комментарий]" \
    "$CLIENT_URL" \
    "{\"event\":\"class_start_hours\",\"object\":{\"userId\":$USER_ID,\"date\":\"$TODAY\",\"beginTime\":\"$FUTURE_TIME\"},\"time\":$(date +%s)}"

# 10. class_start_days
send "10/17 class_start_days — старт группы через N дней" \
    "$CLIENT_URL" \
    "{\"event\":\"class_start_days\",\"object\":{\"userId\":$USER_ID,\"beginDate\":\"$TOMORROW\"},\"time\":$(date +%s)}"

# 11. lesson_mark_set — оценка за домашнее задание
send "11/17 lesson_mark_set (home) — оценка за ДЗ" \
    "$CLIENT_URL" \
    "{\"event\":\"lesson_mark_set\",\"object\":{\"userId\":$USER_ID,\"type\":\"home\",\"value\":5},\"time\":$(date +%s)}"

# 12. lesson_mark_set — оценка за урок
send "12/17 lesson_mark_set (lesson) — оценка за урок" \
    "$CLIENT_URL" \
    "{\"event\":\"lesson_mark_set\",\"object\":{\"userId\":$USER_ID,\"type\":\"lesson\",\"value\":4},\"time\":$(date +%s)}"

# 13. join_new — новая запись в группу
send "13/17 join_new — запись в группу создана" \
    "$CLIENT_URL" \
    "{\"event\":\"join_new\",\"object\":{\"userId\":$USER_ID,\"classId\":341820,\"statusId\":1},\"time\":$(date +%s)}"

# 14. join_changed_state statusId=2 — подтверждена
send "14/17 join_changed_state (2=подтверждена)" \
    "$CLIENT_URL" \
    "{\"event\":\"join_changed_state\",\"object\":{\"userId\":$USER_ID,\"classId\":341820,\"statusId\":2},\"time\":$(date +%s)}"

# 15. join_changed_state statusId=3 — завершена
send "15/17 join_changed_state (3=завершена)" \
    "$CLIENT_URL" \
    "{\"event\":\"join_changed_state\",\"object\":{\"userId\":$USER_ID,\"classId\":341820,\"statusId\":3},\"time\":$(date +%s)}"

# 16. join_changed_state statusId=4 — отменена
send "16/17 join_changed_state (4=отменена)" \
    "$CLIENT_URL" \
    "{\"event\":\"join_changed_state\",\"object\":{\"userId\":$USER_ID,\"classId\":341820,\"statusId\":4},\"time\":$(date +%s)}"

# 17. user_consecutive_visit_missed_2 — 2 пропуска подряд
send "17/17 user_consecutive_visit_missed_2 — пропуски" \
    "$CLIENT_URL" \
    "{\"event\":\"user_consecutive_visit_missed_2\",\"object\":{\"userId\":$USER_ID,\"classId\":341820},\"time\":$(date +%s)}"

# user_birthday — день рождения (отдельно: у Шеры сегодня может не быть ДР, но шаблон проверим)
send "BONUS user_birthday — поздравление" \
    "$CLIENT_URL" \
    "{\"event\":\"user_birthday\",\"object\":{\"userId\":$USER_ID,\"phone\":\"+77769969251\"},\"time\":$(date +%s)}"

# ----------------------------------------------------------
# БЛОК 2 — НЕГАТИВНЫЕ ТЕСТЫ (ничего не должно прийти)
# ----------------------------------------------------------
echo "--- НЕГАТИВНЫЕ ТЕСТЫ (WhatsApp не должен приходить) ---"
echo ""

# Неизвестное событие → лог "нет шаблона", HTTP 200, без отправки
send "NEG-1 unknown_event — нет шаблона, нет отправки" \
    "$CLIENT_URL" \
    "{\"event\":\"unknown_event\",\"object\":{\"userId\":$USER_ID},\"time\":$(date +%s)}"

# join_changed_state с неизвестным statusId → нет шаблона
send "NEG-2 join_changed_state statusId=99 — нет шаблона" \
    "$CLIENT_URL" \
    "{\"event\":\"join_changed_state\",\"object\":{\"userId\":$USER_ID,\"statusId\":99},\"time\":$(date +%s)}"

# Прошедшее занятие — stale-check должен пропустить
send "NEG-3 lesson_start прошедший (beginTime 00:01) — skip" \
    "$CLIENT_URL" \
    "{\"event\":\"lesson_start\",\"object\":{\"userId\":$USER_ID,\"date\":\"$TODAY\",\"beginTime\":\"00:01\"},\"time\":$(date +%s)}"

# Неверный секрет → 404
info "NEG-4 неверный секрет → 404"
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$SERVER/moyklass-webhook/wrong-secret" \
    -H "Content-Type: application/json" \
    -d "{\"event\":\"payment_new\",\"object\":{\"userId\":$USER_ID}}")
if [ "$code" = "404" ]; then
    pass "HTTP $code — защита работает"
else
    fail "HTTP $code — ожидали 404"
fi
echo ""

# ----------------------------------------------------------
# БЛОК 3 — EMPLOYEE WEBHOOK (уведомления сотрудникам)
# ----------------------------------------------------------
echo "--- УВЕДОМЛЕНИЯ СОТРУДНИКАМ (тоже идут на TEST_PHONE) ---"
echo ""

send "EMP-1 lesson_changed — изменение занятия" \
    "$EMPLOYEE_URL" \
    "{\"event\":\"lesson_changed\",\"object\":{\"userId\":$USER_ID,\"date\":\"$TODAY\",\"beginTime\":\"$FUTURE_TIME\"},\"init\":{\"managerId\":227675},\"time\":$(date +%s)}"

send "EMP-2 lesson_record_new — новая запись" \
    "$EMPLOYEE_URL" \
    "{\"event\":\"lesson_record_new\",\"object\":{\"userId\":$USER_ID},\"init\":{\"managerId\":227675},\"time\":$(date +%s)}"

send "EMP-3 payment_new — оплата от ученика" \
    "$EMPLOYEE_URL" \
    "{\"event\":\"payment_new\",\"object\":{\"userId\":$USER_ID,\"summa\":30000},\"init\":{\"managerId\":227675},\"time\":$(date +%s)}"

send "EMP-4 sub_lesson_in_debt — долг ученика" \
    "$EMPLOYEE_URL" \
    "{\"event\":\"sub_lesson_in_debt\",\"object\":{\"userId\":$USER_ID},\"init\":{\"managerId\":227675},\"time\":$(date +%s)}"

# ----------------------------------------------------------
# ИТОГ
# ----------------------------------------------------------
echo "======================================================"
echo "  ГОТОВО"
echo "  Клиентских тестов : 20 (17 событий + BONUS + 3 негативных)"
echo "  Employee тестов   : 4"
echo ""
echo "  Ожидаемый результат:"
echo "  - 18 сообщений [ТЕСТ CRM] на тест-номер"
echo "  - NEG-1/2/3 — WhatsApp НЕ пришёл"
echo "  - NEG-4 — HTTP 404"
echo "  - EMP-1..4 — сообщения [ТЕСТ CRM] на тест-номер"
echo ""
echo "  ⚠️  class_start_hours (#9): в продакшне без date/beginTime"
echo "     в payload всегда будет пропускаться. Нужен отдельный фикс."
echo "======================================================"
