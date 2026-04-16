#!/bin/bash
# Installs the Spitogatos scraper as a macOS launchd service.
# Runs every 30 minutes using your residential IP (bypasses Kasada).
#
# Usage:
#   REDIS_PASSWORD=yourpassword bash scripts/install_launchd.sh
#
# To uninstall:
#   launchctl unload ~/Library/LaunchAgents/com.aiopscenter.spitogatos.plist
#   rm ~/Library/LaunchAgents/com.aiopscenter.spitogatos.plist

set -euo pipefail

PLIST_NAME="com.aiopscenter.spitogatos"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$HOME/.venvs/playwright-test/bin/python3"
SCRIPT="$REPO_DIR/agents/monitor/spitogatos_mac.py"
LOG_DIR="$HOME/Library/Logs/aiopscenter"

# ── Validate ──────────────────────────────────────────────────────────────────
if [[ -z "${REDIS_PASSWORD:-}" ]]; then
  echo "ERROR: REDIS_PASSWORD env var is required."
  echo "Usage: REDIS_PASSWORD=yourpassword bash scripts/install_launchd.sh"
  exit 1
fi

if [[ ! -f "$PYTHON" ]]; then
  echo "ERROR: Python not found at $PYTHON"
  echo "Adjust the PYTHON variable in this script to your interpreter path."
  exit 1
fi

if [[ ! -f "$SCRIPT" ]]; then
  echo "ERROR: Scraper not found at $SCRIPT"
  exit 1
fi

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "$LOG_DIR"

# Unload existing job if running
if launchctl list | grep -q "$PLIST_NAME" 2>/dev/null; then
  echo "Unloading existing launchd job..."
  launchctl unload "$PLIST_PATH" 2>/dev/null || true
fi

# ── Write plist ───────────────────────────────────────────────────────────────
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_NAME}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SCRIPT}</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>VPS_TAILSCALE_IP</key>
    <string>100.113.88.103</string>
    <key>REDIS_PASSWORD</key>
    <string>${REDIS_PASSWORD}</string>
  </dict>

  <!-- Run immediately on load, then every 1800 seconds (30 min) -->
  <key>StartInterval</key>
  <integer>1800</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/spitogatos.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/spitogatos_err.log</string>

  <!-- Keep alive only on crash, not on normal exit -->
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
</dict>
</plist>
EOF

# ── Load ──────────────────────────────────────────────────────────────────────
launchctl load "$PLIST_PATH"

echo ""
echo "✅ Installed: $PLIST_NAME"
echo "   Script  : $SCRIPT"
echo "   Python  : $PYTHON"
echo "   Runs    : every 30 minutes (first run starting now)"
echo "   Logs    : $LOG_DIR/spitogatos.log"
echo ""
echo "Commands:"
echo "  Status : launchctl list | grep spitogatos"
echo "  Logs   : tail -f $LOG_DIR/spitogatos.log"
echo "  Stop   : launchctl unload $PLIST_PATH"
echo "  Start  : launchctl load $PLIST_PATH"
