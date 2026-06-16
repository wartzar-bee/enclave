#!/usr/bin/env bash
# tools/gcloud/host-bridge-setup.sh — runs ON THE macOS HOST.
#
# Installs a launchd agent (org.enclave.gcloud-bridge) that keeps the Google
# Cloud SDK proxy (tools/gcloud/host-bridge.py) alive on :18187, reachable from
# the Docker container at host.docker.internal:18187.
#
# SAME PATTERN as tools/transcribe/host-bridge-setup.sh and tools/voice.
# The Mac already runs qmd(:18181), mlx(:8081), slot3d(:18183), browser(:18184),
# transcribe(:18185), voice(:18186). This adds gcloud(:18187).
#
# ONE-TIME OPERATOR SETUP (run on the Mac):
#   bash /path/to/workspace/tools/gcloud/host-bridge-setup.sh
#   gcloud auth login          # interactive, opens a browser -> <your-google-account>
#   gcloud config set project <PROJECT_ID>
#
# What it does:
#   1. Ensures the Google Cloud SDK (gcloud/gsutil/bq) is installed (brew cask)
#   2. Generates a shared bearer token in .secrets/gcloud-bridge.env
#   3. Writes a launchd plist (KeepAlive=true, RunAtLoad=true)
#   4. Bootstraps + starts the agent, smoke-tests /health
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${GCLOUD_BRIDGE_PORT:-18187}"
LABEL="org.enclave.gcloud-bridge"
LA="$HOME/Library/LaunchAgents"
PLIST="$LA/$LABEL.plist"
mkdir -p "$LA"

# ── 1. Google Cloud SDK ───────────────────────────────────────────────────
echo "==> Checking Google Cloud SDK (gcloud)..."
if ! command -v gcloud >/dev/null 2>&1; then
  # try common SDK install dirs before installing
  for d in /opt/homebrew/bin /opt/homebrew/share/google-cloud-sdk/bin \
           /usr/local/bin "$HOME/google-cloud-sdk/bin"; do
    [ -x "$d/gcloud" ] && export PATH="$d:$PATH" && break
  done
fi
if ! command -v gcloud >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "    installing google-cloud-sdk via Homebrew (cask)..."
    brew install --cask google-cloud-sdk
    for d in /opt/homebrew/share/google-cloud-sdk/bin /opt/homebrew/bin; do
      [ -x "$d/gcloud" ] && export PATH="$d:$PATH" && break
    done
  else
    echo "  Homebrew not found. Install the Google Cloud SDK manually:"
    echo "    https://cloud.google.com/sdk/docs/install-sdk  then re-run."
    exit 1
  fi
fi
GCLOUD_BIN="$(command -v gcloud)"
GCLOUD_DIR="$(dirname "$GCLOUD_BIN")"
echo "    $("$GCLOUD_BIN" --version 2>/dev/null | head -1)  ($GCLOUD_BIN)"

# ── 2. shared bearer token ────────────────────────────────────────────────
SEC="$REPO/.secrets/gcloud-bridge.env"
if [ ! -f "$SEC" ]; then
  mkdir -p "$REPO/.secrets"
  echo "GCLOUD_BRIDGE_TOKEN=$(openssl rand -hex 24)" > "$SEC"
  chmod 600 "$SEC"
  echo "==> generated $SEC"
fi
TOKEN="$(grep '^GCLOUD_BRIDGE_TOKEN=' "$SEC" | cut -d= -f2-)"

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
    <string>$REPO/tools/gcloud/host-bridge.py</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>GCLOUD_BRIDGE_PORT</key><string>$PORT</string>
    <key>GCLOUD_BRIDGE_TOKEN</key><string>$TOKEN</string>
    <key>PATH</key><string>$GCLOUD_DIR:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>$HOME</string>
    <key>CLOUDSDK_CONFIG</key><string>$HOME/.config/gcloud</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO/.gcloud-bridge.log</string>
  <key>StandardErrorPath</key><string>$REPO/.gcloud-bridge.log</string>
</dict></plist>
PLISTEOF

# ── 4. bootstrap + smoke-test ─────────────────────────────────────────────
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 2

echo "==> health check (host-local):"
if curl -fsS "http://127.0.0.1:$PORT/health"; then
  echo ""; echo "==> gcloud-bridge is UP on :$PORT"
else
  echo ""; echo "  WARNING: not responding yet — tail -f $REPO/.gcloud-bridge.log"
fi

echo ""
echo "==> Installed $LABEL on :$PORT"
ACCT="$("$GCLOUD_BIN" config get-value account 2>/dev/null)"
if [ -z "$ACCT" ] || [ "$ACCT" = "(unset)" ]; then
  echo "  • NOT AUTHENTICATED YET. Run:  gcloud auth login   (-> <your-google-account>)"
  echo "                            then: gcloud config set project <PROJECT_ID>"
else
  echo "  • authenticated as: $ACCT   project: $("$GCLOUD_BIN" config get-value project 2>/dev/null)"
fi
echo "  • Logs:    tail -f $REPO/.gcloud-bridge.log"
echo "  • Restart: launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "  • Stop:    launchctl bootout gui/$(id -u)/$LABEL"
echo "  • From the CONTAINER: python3 tools/gcloud/bridge.py --health"
