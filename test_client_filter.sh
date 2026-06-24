#!/bin/bash
set -eu
cd /home/almalinux/BotNastya
set -a; source .env; set +a
TS=$(date +%s)
URL="http://127.0.0.1:8000/moyklass-webhook/${MOYKLASS_WEBHOOK_SECRET}"
echo "=== payment: real Client user 3284384 ==="
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"payment_new\",\"object\":{\"userId\":3284384},\"time\":$TS}"
echo
echo "=== payment: lead user 10173071 (should skip) ==="
curl -s -X POST "$URL" -H 'Content-Type: application/json' \
  -d "{\"event\":\"payment_new\",\"object\":{\"userId\":10173071},\"time\":$TS}"
echo
journalctl -u bot.service -n 15 --no-pager | grep -E 'payment_new|Клиент|отправлено|ТЕСТ'
