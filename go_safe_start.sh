#!/bin/bash
# Безопасный прод-старт после антибана: без бэклога, без клиентских рассылок CRM,
# строгий warm-up rate-limit. Запускать НА СЕРВЕРЕ: bash go_safe_start.sh
set -eu
cd /home/almalinux/BotNastya
TS=$(date +%s)

ensure_env() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}=" .env 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '\n%s=%s\n' "$key" "$val" >> .env
  fi
}

# CRM: клиентам не спамим, тест-режим выкл, отсечка бэклога
ensure_env MOYKLASS_CLIENT_WEBHOOKS_ENABLED 0
ensure_env MOYKLASS_WEBHOOK_TEST_PHONE ""
ensure_env MOYKLASS_WEBHOOK_ENABLED_SINCE "$TS"
ensure_env LEAD_POLL_ENABLED 0

# Follow-up на прогреве выкл (не пишем «спящим» сами)
ensure_env FOLLOWUP_ENABLED 0

# Антибан / прогрев номера
ensure_env WA_RATE_LIMIT_ENABLED 1
ensure_env WA_WARMUP_MODE 1
ensure_env WA_MIN_INTERVAL_SEC 6
ensure_env WA_PER_CHAT_COOLDOWN_SEC 20
ensure_env WA_MAX_PER_HOUR 25
ensure_env WA_MAX_PER_DAY 80
ensure_env WA_FLOOD_PAUSE_SEC 300
ensure_env WA_SKIP_OLD_INCOMING 1
ensure_env WA_INCOMING_MAX_AGE_SEC 600

echo "=== .env anti-ban / CRM ==="
grep -E '^(MOYKLASS_CLIENT|MOYKLASS_WEBHOOK_|LEAD_POLL|FOLLOWUP_|WA_)' .env || true

echo "=== smoke ==="
./venv/bin/python3 test_pre_deploy_smoke.py

echo "=== restart ==="
sudo systemctl restart bot.service
sleep 3
sudo systemctl is-active bot.service

echo "=== anti-ban in logs ==="
journalctl -u bot.service -n 40 --no-pager | grep -E 'anti-ban|ENABLED_SINCE|rate_limit|warmup|Application startup|Uvicorn' || true

echo
echo "OK: бот в safe-start. Клиентские CRM-рассылки ВЫКЛ."
echo "Можно авторизовать номер в Green API — бот не раздует бэклог."
