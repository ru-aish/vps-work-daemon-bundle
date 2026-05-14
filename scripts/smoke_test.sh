#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/opt/vps-work/app}"
PY_BIN="${PY_BIN:-python3}"

cd "$APP_DIR"
"$PY_BIN" -m py_compile daemon_v2/cloud_daemon.py daemon_v2/local_daemon.py dv_verifier.py

export ADV_VERIFIER_HOME="$APP_DIR"
export CLOUD_DAEMON_HOST=127.0.0.1
export CLOUD_DAEMON_PORT=18788

"$PY_BIN" -m daemon_v2.cloud_daemon >/tmp/vps_work_smoke.log 2>&1 &
PID=$!
trap 'kill $PID >/dev/null 2>&1 || true' EXIT

for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:18788/health" >/tmp/vps_work_health.json 2>/dev/null; then
    echo "Smoke OK: $(cat /tmp/vps_work_health.json)"
    exit 0
  fi
  sleep 0.5
done

echo "Smoke failed; log:" >&2
cat /tmp/vps_work_smoke.log >&2
exit 1
