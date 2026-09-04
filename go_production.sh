#!/bin/bash
set -eu
cd /home/almalinux/BotNastya
TS=$(date +%s)
sed -i 's/^MOYKLASS_WEBHOOK_TEST_PHONE=.*/MOYKLASS_WEBHOOK_TEST_PHONE=/' .env
sed -i "s/^MOYKLASS_WEBHOOK_ENABLED_SINCE=.*/MOYKLASS_WEBHOOK_ENABLED_SINCE=${TS}/" .env
# После ручного go_production — снять прогрев можно позже; safe defaults на всякий.
grep -q '^WA_RATE_LIMIT_ENABLED=' .env || echo 'WA_RATE_LIMIT_ENABLED=1' >> .env
grep -q '^WA_SKIP_OLD_INCOMING=' .env || echo 'WA_SKIP_OLD_INCOMING=1' >> .env
echo "=== .env CRM ==="
grep -E '^(MOYKLASS_|WA_|FOLLOWUP_|LEAD_POLL)' .env || true
sudo systemctl restart bot.service
sleep 2
sudo systemctl is-active bot.service
