#!/bin/sh
# Keep the gli hub running in the background on macOS (starts at login, restarts if it dies).
#   scripts/install_launchd.sh            install + start (a hub started by hand is stopped first)
#   scripts/install_launchd.sh uninstall  stop + remove
#   scripts/install_launchd.sh status     is it loaded and running
# Run it as yourself, not with sudo: a login agent belongs to your user (one written by root is never loaded).
# Note: this writes ~/Library/LaunchAgents/dev.glimpse.hub.plist (outside the project directory).
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=dev.glimpse.hub
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
if [ "$(id -u)" = 0 ]; then
  echo "run this as yourself, without sudo (a login agent is per user; root's would never start at your login)" >&2
  exit 1
fi
case "$1" in
  uninstall)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed"
    exit 0 ;;
  status)
    if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$LABEL" | grep -E "^[[:space:]]+(state|pid|last exit code) =" | sed 's/^[[:space:]]*/  /'
    else
      echo "not loaded"
    fi
    exit 0 ;;
esac
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p -m 700 "$DIR/data"
chmod 700 "$DIR/data"
# the log can contain session names and paths: keep it private like everything else in data/
touch "$DIR/data/hub.log"
chmod 600 "$DIR/data/hub.log"
# launchd starts the hub's Python directly, not ./gli (which does the same): started by launchd, /bin/sh is kept out
# of ~/Documents by macOS privacy protection ("Operation not permitted", launchd exit 78), Python is not
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "python3 not found: run gli setup first" >&2; exit 1; }
# launchd's own output (a crash, a Python traceback) goes outside ~/Documents: launchd refuses a log file there that
# it did not create itself (exit 78, seen 2026-10-26). The hub writes data/hub.log itself (--log).
OUT="$HOME/Library/Logs/glimpse-hub.log"
mkdir -p "$HOME/Library/Logs"
# an earlier file (e.g. written by root through sudo) is replaced, not written into
if ! rm -f "$PLIST"; then
  echo "cannot replace $PLIST: remove it first (sudo rm \"$PLIST\"), then run this again" >&2
  exit 1
fi
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>$DIR/hub/server.py</string><string>--log</string><string>$DIR/data/hub.log</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$OUT</string>
  <key>StandardErrorPath</key><string>$OUT</string>
</dict></plist>
EOF
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# a hub started by hand holds the port: the launchd one could not listen and would restart every 10 s
if pkill -f "$DIR/hub/server.py"; then
  echo "stopped the hub that was started by hand (sessions keep running; devices reconnect)"
  sleep 2
fi
launchctl bootstrap "$DOMAIN" "$PLIST"
sleep 3
echo "installed: $PLIST (log: $DIR/data/hub.log; launchd output: $OUT)"
launchctl print "$DOMAIN/$LABEL" | grep -E "^[[:space:]]+(state|pid) =" | sed 's/^[[:space:]]*/  /'
