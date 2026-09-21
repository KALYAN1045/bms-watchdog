#!/bin/bash
# Install the watchdog as a macOS background service (starts at login,
# restarts if it dies). Run it again after editing watchlist.yaml.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.bmswatch.watchdog"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$HERE/logs"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$HERE/scripts/run.sh</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$HERE/logs/watchdog.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/watchdog.err</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed and started: $LABEL"
echo "  logs:     tail -f $HERE/logs/watchdog.log"
echo "  stop:     launchctl unload $PLIST"
echo "  restart:  bash scripts/install-launchd.sh"
