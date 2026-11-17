#!/bin/bash
set -eu
cd /home/almalinux/BotNastya
set -a
source .env
set +a
SECRET="${MOYKLASS_WEBHOOK_SECRET:?no secret}"
TS=$(date +%s)
URL="http://127.0.0.1:8000/moyklass-webhook-employee/${SECRET}"
# TZ школы — Asia/Almaty; сервер в UTC. Берём дату/время в Almaty, как трактует их бот.
TODAY=$(TZ=Asia/Almaty date +%Y-%m-%d)

echo "=== user_birthday ==="
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"user_birthday\",\"object\":{\"userId\":10466998},\"init\":{\"managerId\":227675},\"time\":$TS}"
echo

echo "=== lesson_start_hours ==="
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"lesson_start_hours\",\"object\":{\"lessonId\":1,\"userId\":10466998,\"date\":\"$TODAY\",\"beginTime\":\"23:59\"},\"init\":{\"managerId\":227675},\"time\":$TS}"
echo

echo "=== lesson_changed ==="
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"lesson_changed\",\"object\":{\"lessonId\":1,\"date\":\"$TODAY\",\"beginTime\":\"18:00\",\"classId\":458952},\"init\":{\"managerId\":227675},\"time\":$TS}"
echo
