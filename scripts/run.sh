#!/bin/bash
# Wrapper used by launchd: load secrets from .env, then start the watchdog.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

if [[ -f .env ]]; then
  set -a; source .env; set +a
fi

exec ./venv/bin/python watch.py "${@:-run}"
