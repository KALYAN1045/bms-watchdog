#!/bin/bash
# Ship this folder to a Linux server and run it there as a systemd service.
#
#   bash scripts/deploy.sh ubuntu@your-server-ip
#
# Safe to re-run: it syncs changes and restarts the service.
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${2:-bms-watchdog}"
if [[ -z "$TARGET" ]]; then
  echo "usage: bash scripts/deploy.sh user@host [remote-dir]" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

[[ -f watchlist.yaml ]] || { echo "no watchlist.yaml — configure it first" >&2; exit 1; }
[[ -f .env ]]          || { echo "no .env — run watch.py telegram-setup first" >&2; exit 1; }

echo "==> syncing to $TARGET:$REMOTE_DIR"
rsync -az --delete \
  --exclude venv/ --exclude .git/ --exclude logs/ --exclude state.json \
  --exclude .chrome-profile/ --exclude __pycache__/ --exclude '*.pyc' \
  ./ "$TARGET:$REMOTE_DIR/"

echo "==> installing and starting"
ssh -t "$TARGET" "cd $REMOTE_DIR && bash scripts/install-systemd.sh"

echo
echo "Done. Watch it with:"
echo "  ssh $TARGET 'journalctl -u bms-watchdog -f'"
