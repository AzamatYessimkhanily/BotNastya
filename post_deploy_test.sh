#!/bin/bash
set -eu
cd /home/almalinux/BotNastya
set -a
source .env
set +a
SECRET="${MOYKLASS_WEBHOOK_SECRET:?no secret}"
TS=$(date +%s)
TODAY=$(date +%Y-%m-%d)
CLIENT_URL="http://127.0.0.1:8000/moyklass-webhook/${SECRET}"
EMP_URL="http://127.0.0.1:8000/moyklass-webhook-employee/${SECRET}"

echo "=== client payment_new ==="
curl -s -X POST "$CLIENT_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"payment_new\",\"object\":{\"userId\":10466998},\"time\":$TS}"
echo

echo "=== client lesson_start_hours (no link - empty comment) ==="
curl -s -X POST "$CLIENT_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"lesson_start_hours\",\"object\":{\"userId\":10466998,\"classId\":458952,\"date\":\"$TODAY\",\"beginTime\":\"23:59\"},\"time\":$TS}"
echo

echo "=== employee user_birthday ==="
curl -s -X POST "$EMP_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"user_birthday\",\"object\":{\"userId\":10466998},\"init\":{\"managerId\":227675},\"time\":$TS}"
echo

echo "=== employee lesson_start (5 min) ==="
IN5=$(date -d '+5 minutes' '+%Y-%m-%d %H:%M')
D5=$(echo $IN5 | cut -d' ' -f1)
T5=$(echo $IN5 | cut -d' ' -f2)
curl -s -X POST "$EMP_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"lesson_start\",\"object\":{\"userId\":10466998,\"lessonId\":1,\"date\":\"$D5\",\"beginTime\":\"$T5\"},\"init\":{\"managerId\":227675},\"time\":$TS}"
echo

echo "=== employee lesson_changed ==="
curl -s -X POST "$EMP_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"lesson_changed\",\"object\":{\"lessonId\":1,\"date\":\"$TODAY\",\"beginTime\":\"18:00\",\"classId\":458952},\"init\":{\"managerId\":227675},\"time\":$TS}"
echo

echo "=== send results ==="
journalctl -u bot.service -n 40 --no-pager | grep -E 'отправлено|ТЕСТОВЫЙ|пропуск|error|ERROR|неактивен' || true
