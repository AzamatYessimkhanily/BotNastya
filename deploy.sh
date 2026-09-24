#!/usr/bin/env bash
# Deploy verified source without changing .env or runtime state.
set -euo pipefail
HOST="${DEPLOY_HOST:-botnastya}"
REMOTE="${DEPLOY_DIR:-/home/almalinux/BotNastya}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
[[ "$REMOTE" =~ ^/[a-zA-Z0-9_./-]+$ ]] || { echo 'Unsupported DEPLOY_DIR'; exit 1; }
cd "$ROOT"
REVISION="$(git rev-parse HEAD)"
FILES=(bot.py attendance_monitor.py runtime_state.py test_pre_deploy_smoke.py test_reliability.py)
ARCHIVE="$(mktemp "${TMPDIR:-/tmp}/botnastya-release.XXXXXX")"
trap 'rm -f "$ARCHIVE"' EXIT
tar -czf "$ARCHIVE" "${FILES[@]}"
STAGE="$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" 'mktemp -d /tmp/botnastya-release.XXXXXXXX')"
[[ "$STAGE" =~ ^/tmp/botnastya-release\.[a-zA-Z0-9]+$ ]] || exit 1
scp -o BatchMode=yes -o ConnectTimeout=10 "$ARCHIVE" "$HOST:$STAGE/release.tar.gz"
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" bash -s -- "$REMOTE" "$STAGE" "$REVISION" <<'REMOTE_SCRIPT'
set -euo pipefail
REMOTE="$1"
STAGE="$2"
REVISION="$3"
PYTHON="$REMOTE/venv/bin/python3"
FILES=(bot.py attendance_monitor.py runtime_state.py test_pre_deploy_smoke.py test_reliability.py)
cd "$STAGE"
tar -xzf release.tar.gz
"$PYTHON" -m py_compile "${FILES[@]}"
"$PYTHON" test_pre_deploy_smoke.py > smoke.log 2>&1 || { cat smoke.log; exit 1; }
"$PYTHON" -m unittest -v test_reliability > reliability.log 2>&1 || { cat reliability.log; exit 1; }
tail -n 2 smoke.log
tail -n 4 reliability.log

BACKUP="$REMOTE/.deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-${REVISION:0:12}"
mkdir -p "$BACKUP"
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
    sudo -n systemctl restart bot.service
    exit 1
}
trap rollback ERR
for file in "${FILES[@]}"; do
    install -m 644 "$STAGE/$file" "$REMOTE/$file.deploy-new"
    mv "$REMOTE/$file.deploy-new" "$REMOTE/$file"
done
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
