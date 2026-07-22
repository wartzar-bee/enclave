#!/usr/bin/env bash
# tools/bridge-template/host-bridge-setup.sh — runs ON THE HOST, once.
#
# Generates the shared token, installs a launchd agent that keeps the bridge alive, and smoke-tests
# it. Copy this next to your own bridge and change NAME/PORT.
#
#   bash <repo>/tools/bridge-template/host-bridge-setup.sh
#
# WHY A PROCESS MANAGER AND NOT `python3 host-bridge.py &`. A bridge that dies when the terminal
# closes, or after a reboot, fails in the worst possible way: the agent sees "connection refused",
# concludes the capability does not exist, and routes around it — usually by escalating to the human
# or by doing something worse. KeepAlive is not polish, it is what makes the capability real.
#
# LINUX: there is no launchd. Install the equivalent systemd --user unit instead (Restart=always,
# WantedBy=default.target) and keep everything else identical — token, port, /health smoke test.
set -uo pipefail

NAME="${BRIDGE_NAME:-template}"
PORT="${TEMPLATE_BRIDGE_PORT:-18190}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LABEL="org.enclave.${NAME}-bridge"
LA="$HOME/Library/LaunchAgents"
PLIST="$LA/$LABEL.plist"
SEC="$REPO/.secrets/${NAME}-bridge.env"
mkdir -p "$LA" "$REPO/.secrets"

# ── 1. shared bearer token ────────────────────────────────────────────────
# Generated here, never committed: .secrets/ is gitignored and this file is chmod 600. The container
# reads the SAME file through its read-only mount, which is why both halves agree without the token
# ever being baked into an image or passed on a command line.
if [ ! -f "$SEC" ]; then
  echo "TEMPLATE_BRIDGE_TOKEN=$(openssl rand -hex 24)" > "$SEC"
  chmod 600 "$SEC"
  echo "==> generated $SEC"
fi
TOKEN="$(grep '^TEMPLATE_BRIDGE_TOKEN=' "$SEC" | cut -d= -f2-)"
if [ -z "$TOKEN" ]; then echo "no token in $SEC — delete it and re-run"; exit 1; fi

# ── 2. dependencies ───────────────────────────────────────────────────────
# Install what YOUR capability needs here (brew formula, venv, model download) and FAIL LOUDLY if it
# is missing. A bridge that starts without its dependency reports healthy and breaks at call time,
# which is the hardest failure for an agent to diagnose.
command -v python3 >/dev/null 2>&1 || { echo "python3 required"; exit 1; }

# ── 3. launchd plist ──────────────────────────────────────────────────────
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$REPO/tools/bridge-template/host-bridge.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>TEMPLATE_BRIDGE_NAME</key><string>$NAME</string>
    <key>TEMPLATE_BRIDGE_PORT</key><string>$PORT</string>
    <key>TEMPLATE_BRIDGE_TOKEN</key><string>$TOKEN</string>
    <key>TEMPLATE_BRIDGE_REPO</key><string>$REPO</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO/.${NAME}-bridge.log</string>
  <key>StandardErrorPath</key><string>$REPO/.${NAME}-bridge.log</string>
</dict></plist>
PLISTEOF

# ── 4. bootstrap + smoke-test ─────────────────────────────────────────────
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 2

echo "==> health check:"
if curl -fsS "http://127.0.0.1:$PORT/health"; then
  echo ""; echo "==> ${NAME}-bridge is UP on :$PORT"
else
  echo ""; echo "  WARNING: not responding — tail -f $REPO/.${NAME}-bridge.log"
fi

cat <<EOF

Installed $LABEL on :$PORT
  • Logs:    tail -f $REPO/.${NAME}-bridge.log
  • Restart: launchctl kickstart -k gui/$(id -u)/$LABEL
  • Stop:    launchctl bootout gui/$(id -u)/$LABEL
  • From the CONTAINER: python3 tools/bridge-template/bridge.py --health

Next: grant it to an agent. A bridge nobody mounted is a bridge nobody has — see docs/BRIDGES.md
      → "Granting a bridge to an agent". The capability existing is not the same as the agent having it.
EOF
