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

echo "=== smoke tests ==="
/home/almalinux/BotNastya/venv/bin/python3 test_pre_deploy_smoke.py

sudo systemctl restart bot.service
sleep 2
sudo systemctl is-active bot.service

echo "=== client lesson_start_hours ==="
curl -s -X POST "$CLIENT_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"lesson_start_hours\",\"object\":{\"userId\":10466998,\"classId\":458952,\"date\":\"$TODAY\",\"beginTime\":\"23:59\"},\"time\":$TS}"
echo

echo "=== client payment_new ==="
curl -s -X POST "$CLIENT_URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"payment_new\",\"object\":{\"userId\":10466998},\"time\":$TS}"
echo

echo "=== employee tests ==="
bash test_employee_deploy.sh

echo "=== logs ==="
journalctl -u bot.service -n 25 --no-pager | grep -E 'отправлено|пропуск|ТЕСТ|error|ERROR' || true
