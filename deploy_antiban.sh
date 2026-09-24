#!/bin/bash
# Деплой антибана с Mac на сервер (нужен SSH-доступ).
# Использование: bash deploy_antiban.sh
set -eu
# Prefer SSH Host alias from ~/.ssh/config (Host botnastya → almalinux@82.115.48.37).
HOST="${DEPLOY_HOST:-botnastya}"
REMOTE="${DEPLOY_DIR:-/home/almalinux/BotNastya}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "=== upload ==="
scp -o ConnectTimeout=10 \
  "$ROOT/bot.py" \
  "$ROOT/attendance_monitor.py" \
  "$ROOT/runtime_state.py" \
  "$ROOT/test_pre_deploy_smoke.py" \
  "$ROOT/.env.example" \
  "$ROOT/go_safe_start.sh" \
  "$HOST:$REMOTE/"

echo "=== remote safe-start ==="
ssh -o ConnectTimeout=10 "$HOST" "cd $REMOTE && bash go_safe_start.sh"

echo "=== done ==="
