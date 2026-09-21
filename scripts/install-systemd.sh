#!/bin/bash
# Run this ON the server. Installs the watchdog as a systemd service:
# starts at boot, restarts on crash, logs to journalctl.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=/etc/systemd/system/bms-watchdog.service
USER_NAME="$(id -un)"

cd "$HERE"

if [[ ! -f .env ]];          then echo "missing .env"          >&2; exit 1; fi
if [[ ! -f watchlist.yaml ]]; then echo "missing watchlist.yaml" >&2; exit 1; fi

echo "==> python venv"
command -v python3 >/dev/null || { sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-venv; }
[[ -d venv ]] || python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q -r requirements.txt

echo "==> checking this server can actually reach BookMyShow"
set -a; . ./.env; set +a
./venv/bin/python watch.py doctor

echo "==> installing service"
sudo tee "$SERVICE" >/dev/null <<UNIT
[Unit]
Description=BookMyShow ticket watchdog
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$HERE
EnvironmentFile=$HERE/.env
Environment=BMS_DESKTOP=0
ExecStart=$HERE/venv/bin/python $HERE/watch.py bot
Restart=always
RestartSec=15
# it is a tiny process; keep it that way
MemoryMax=512M

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now bms-watchdog
sleep 3
sudo systemctl --no-pager --lines=15 status bms-watchdog || true

echo
echo "Running. Useful commands:"
echo "  journalctl -u bms-watchdog -f      # live logs"
echo "  sudo systemctl restart bms-watchdog"
echo "  sudo systemctl stop bms-watchdog"
