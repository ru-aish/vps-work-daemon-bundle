#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-/opt/vps-work}"
PY_BIN="${PY_BIN:-python3}"
SUDO=""

if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "Run as root or install sudo." >&2
    exit 1
  fi
fi

install_pkgs() {
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y
    $SUDO apt-get install -y python3 python3-venv python3-pip dnsutils iproute2 curl git rsync
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y python3 python3-pip python3-virtualenv bind-utils iproute curl git rsync
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y python3 python3-pip python3-virtualenv bind-utils iproute curl git rsync
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm python python-pip bind iproute2 curl git rsync
  elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper --non-interactive install python3 python3-pip python3-virtualenv bind-utils iproute2 curl git rsync
  elif command -v apk >/dev/null 2>&1; then
    $SUDO apk add --no-cache python3 py3-pip py3-virtualenv bind-tools iproute2 curl git rsync
  else
    echo "Unsupported Linux package manager. Install python3, venv, pip, dig, ip manually." >&2
    exit 1
  fi
}

mkdir -p "$APP_DIR"
cd "$APP_DIR"

if ! command -v "$PY_BIN" >/dev/null 2>&1 || ! "$PY_BIN" -m venv -h >/dev/null 2>&1; then
  install_pkgs
else
  install_pkgs || true
fi

if [ ! -d .venv ]; then
  "$PY_BIN" -m venv .venv
fi

cat > "$APP_DIR/app/config.json" << 'JSON'
{
  "mode_switch": 1,
  "api_base": "",
  "smtp_timeout_sec": 8,
  "helo_host": "localhost",
  "mail_from": "noreply@localhost"
}
JSON

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd not found. Run manually: cd $APP_DIR/app && $APP_DIR/.venv/bin/python -m daemon_v2.cloud_daemon"
  exit 0
fi

$SUDO tee /etc/systemd/system/vps-work-cloud-daemon.service >/dev/null <<SERVICE
[Unit]
Description=VPS Work Cloud Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR/app
Environment=ADV_VERIFIER_HOME=$APP_DIR/app
Environment=CLOUD_DAEMON_HOST=0.0.0.0
Environment=CLOUD_DAEMON_PORT=8788
ExecStart=$APP_DIR/.venv/bin/python -m daemon_v2.cloud_daemon
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
SERVICE

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now vps-work-cloud-daemon.service
$SUDO systemctl --no-pager --full status vps-work-cloud-daemon.service || true

echo "Cloud daemon installed and running on :8788"
