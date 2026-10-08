#!/usr/bin/env bash
# Deploy verified source, preserving runtime state. Optional explicit model switch.
set -euo pipefail
HOST="${DEPLOY_HOST:-botnastya}"
REMOTE="${DEPLOY_DIR:-/home/almalinux/BotNastya}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
[[ "$REMOTE" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Unsupported DEPLOY_DIR'; exit 1; }
cd "$ROOT"
REVISION="$(git rev-parse HEAD)"
MODEL="${DEPLOY_OPENAI_MODEL:-}"
[[ "$MODEL" =~ ^[a-zA-Z0-9._-]*$ ]] || { echo 'Unsupported model name'; exit 1; }
FILES=(bot.py attendance_monitor.py runtime_state.py conversation_memory.py dialog_policy.py usage_reporting.py test_pre_deploy_smoke.py test_reliability.py test_conversation.py test_debt_notifications.py test_incidents.py)
ARCHIVE="$(mktemp "${TMPDIR:-/tmp}/botnastya-release.XXXXXX")"
trap 'rm -f "$ARCHIVE"' EXIT
tar -czf "$ARCHIVE" "${FILES[@]}"
REVISION="$REVISION-$(shasum -a 256 "$ARCHIVE" | cut -c1-12)"
STAGE="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" 'mktemp -d /tmp/botnastya-release.XXXXXXXX')"
[[ "$STAGE" =~ ^/tmp/botnastya-release\.[a-zA-Z0-9]+$ ]] || exit 1
scp -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "$HOST:$STAGE/release.tar.gz"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" bash -s -- "$REMOTE" "$STAGE" "$REVISION" "$MODEL" <<'REMOTE_SCRIPT'
set -euo pipefail
REMOTE="$1"
STAGE="$2"
REVISION="$3"
MODEL="${4:-}"
PYTHON="$REMOTE/venv/bin/python3"
FILES=(bot.py attendance_monitor.py runtime_state.py conversation_memory.py dialog_policy.py usage_reporting.py test_pre_deploy_smoke.py test_reliability.py test_conversation.py test_debt_notifications.py test_incidents.py)
cd "$STAGE"
tar -xzf release.tar.gz
"$PYTHON" -m py_compile "${FILES[@]}"
"$PYTHON" test_pre_deploy_smoke.py > smoke.log 2>&1 || { cat smoke.log; exit 1; }
"$PYTHON" -m unittest -v test_reliability test_conversation test_debt_notifications test_incidents > reliability.log 2>&1 || { cat reliability.log; exit 1; }
tail -n 2 smoke.log
tail -n 4 reliability.log

BACKUP="$REMOTE/.deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-${REVISION:0:12}"
mkdir -p "$BACKUP"
if [[ -n "$MODEL" ]]; then cp -p "$REMOTE/.env" "$BACKUP/.env"; fi
for file in "${FILES[@]}"; do
    if [[ -f "$REMOTE/$file" ]]; then
        cp -p "$REMOTE/$file" "$BACKUP/$file"
    else
        touch "$BACKUP/$file.missing"
    fi
done
rollback() {
    trap - ERR
    echo "Deploy failed; restoring $BACKUP" >&2
    for file in "${FILES[@]}"; do
        if [[ -f "$BACKUP/$file.missing" ]]; then
            rm -f "$REMOTE/$file"
        else
            cp -p "$BACKUP/$file" "$REMOTE/$file"
        fi
    done
    if [[ -n "$MODEL" ]]; then cp -p "$BACKUP/.env" "$REMOTE/.env"; fi
    sudo -n systemctl restart bot.service
    exit 1
}
trap rollback ERR
for file in "${FILES[@]}"; do
    install -m 644 "$STAGE/$file" "$REMOTE/$file.deploy-new"
    mv "$REMOTE/$file.deploy-new" "$REMOTE/$file"
done
if [[ -n "$MODEL" ]]; then
    "$PYTHON" - "$REMOTE/.env" "$MODEL" <<'PY'
import os, re, sys
from pathlib import Path
path = Path(sys.argv[1])
text = path.read_text()
line = 'OPENAI_MODEL=' + sys.argv[2]
pattern = r'^\s*(?:export\s+)?OPENAI_MODEL\s*=.*$'
text = re.sub(pattern, line, text, flags=re.M) if re.search(pattern, text, re.M) else text.rstrip() + '\n' + line + '\n'
tmp = path.with_name('.env.deploy-new')
tmp.write_text(text)
tmp.chmod(path.stat().st_mode)
os.replace(tmp, path)
PY
fi
sudo -n systemctl restart bot.service
healthy=0
for attempt in $(seq 1 15); do
    if curl --fail --silent --max-time 2 http://127.0.0.1:8000/health | "$PYTHON" -c 'import json,sys; assert json.load(sys.stdin) == {"status": "ok"}' 2>/dev/null; then
        healthy=1
        break
    fi
    sleep 1
done
[[ "$healthy" == 1 ]]
systemctl is-active --quiet bot.service
printf '%s\n' "$REVISION" > "$REMOTE/.deployed-revision"
trap - ERR
echo "Deployed: $REVISION"
echo "Backup: $BACKUP"
echo 'Service: active; /health: ok'
cd "$REMOTE"
sha256sum "${FILES[@]}"
rm -rf "$STAGE"
REMOTE_SCRIPT
