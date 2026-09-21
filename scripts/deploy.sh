#!/bin/bash
# Ship this folder to a Linux server and start it under Docker.
#
#   bash scripts/deploy.sh user@your-server-ip
#
# Safe to re-run: it syncs changes and restarts the container.
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${2:-bms-watchdog}"
if [[ -z "$TARGET" ]]; then
  echo "usage: bash scripts/deploy.sh user@host [remote-dir]" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if [[ ! -f watchlist.yaml ]]; then echo "no watchlist.yaml — configure it first" >&2; exit 1; fi
if [[ ! -f .env ]];          then echo "no .env — run watch.py telegram-setup first" >&2; exit 1; fi

echo "==> installing Docker on $TARGET if needed"
ssh "$TARGET" 'command -v docker >/dev/null || {
    sudo apt-get update -qq &&
    sudo apt-get install -y -qq docker.io docker-compose-v2 &&
    sudo usermod -aG docker "$USER"; }'

echo "==> syncing project to $TARGET:$REMOTE_DIR"
rsync -az --delete \
  --exclude venv/ --exclude .git/ --exclude logs/ \
  --exclude .chrome-profile/ --exclude __pycache__/ --exclude '*.pyc' \
  ./ "$TARGET:$REMOTE_DIR/"

echo "==> building (first run pulls Chromium, takes a few minutes)"
ssh "$TARGET" "cd $REMOTE_DIR && sudo docker compose build"

echo "==> checking this server can actually reach BookMyShow"
if ! ssh "$TARGET" "cd $REMOTE_DIR && sudo docker compose run --rm watchdog python watch.py doctor"; then
  echo
  echo "doctor failed. If it says Cloudflare blocked the session, this host's IP"
  echo "is the problem — try another provider, or set a proxy in watchlist.yaml."
  exit 1
fi

echo "==> starting"
ssh "$TARGET" "cd $REMOTE_DIR && sudo docker compose up -d"
ssh "$TARGET" "cd $REMOTE_DIR && sudo docker compose ps"

echo
echo "Running. Useful commands:"
echo "  ssh $TARGET 'cd $REMOTE_DIR && sudo docker compose logs -f'"
echo "  ssh $TARGET 'cd $REMOTE_DIR && sudo docker compose restart'"
