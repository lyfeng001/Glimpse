#!/bin/sh
# Keep the glimpse hub running in the background on macOS (starts at login, restarts if it dies).
#   scripts/install_launchd.sh            install + start
#   scripts/install_launchd.sh uninstall  stop + remove
# Note: this writes ~/Library/LaunchAgents/dev.glimpse.hub.plist (outside the project directory).
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=dev.glimpse.hub
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ "$1" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed"
  exit 0
fi
mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p -m 700 "$DIR/data"
chmod 700 "$DIR/data"
# the log can contain session names and paths: keep it private like everything else in data/
touch "$DIR/data/hub.log"
chmod 600 "$DIR/data/hub.log"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$DIR/glimpse</string><string>hub</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$DIR/data/hub.log</string>
  <key>StandardErrorPath</key><string>$DIR/data/hub.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed: $PLIST (log: $DIR/data/hub.log)"
