#!/bin/bash
set -eu
cd /home/almalinux/BotNastya
TS=$(date +%s)
sed -i 's/^MOYKLASS_WEBHOOK_TEST_PHONE=.*/MOYKLASS_WEBHOOK_TEST_PHONE=/' .env
sed -i "s/^MOYKLASS_WEBHOOK_ENABLED_SINCE=.*/MOYKLASS_WEBHOOK_ENABLED_SINCE=${TS}/" .env
echo "=== .env CRM ==="
grep -E '^MOYKLASS_' .env
sudo systemctl restart bot.service
sleep 2
sudo systemctl is-active bot.service
