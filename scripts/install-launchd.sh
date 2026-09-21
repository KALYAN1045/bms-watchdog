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
    <string>bot</string>
  </array>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- otherwise Python buffers stdout to the log file and it stays empty -->
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>StandardOutPath</key><string>$HERE/logs/watchdog.log</string>
  <key>StandardErrorPath</key><string>$HERE/logs/watchdog.err</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLISTEOF

DOMAIN="gui/$(id -u)"

# bootout/bootstrap is the modern pair; `load` leaves RunAtLoad unfired on
# recent macOS, which silently gives you an installed service that never runs
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
sleep 2                     # bootout is async; bootstrapping too soon gives EIO
launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
# RunAtLoad does not reliably fire on bootstrap, so start it explicitly
launchctl kickstart "$DOMAIN/$LABEL" 2>/dev/null || true

sleep 4
if launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q "state = running"; then
  echo "Installed and running: $LABEL"
else
  echo "WARNING: $LABEL installed but not running. Check logs/watchdog.err" >&2
fi
echo "  logs:     tail -f $HERE/logs/watchdog.log"
echo "  stop:     launchctl bootout gui/\$(id -u)/$LABEL"
echo "  restart:  bash scripts/install-launchd.sh"
