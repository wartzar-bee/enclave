#!/usr/bin/env python3
"""
tools/bridge-template/bridge.py — runs IN THE CONTAINER. The agent-facing half of the bridge.

Stdlib only, on purpose: this runs inside an agent image you do not control the dependency list of,
and a bridge client that needs `pip install` is a bridge that silently does not work.

Usage:
  python3 bridge.py --health
  python3 bridge.py --sysinfo [--echo "hello"]

WHY THIS FILE EXISTS AT ALL. The agent could POST to the bridge with curl. It shouldn't: then every
prompt has to carry the host, the port, the token path and the error handling, and each agent
reinvents them slightly differently. A CLI is a capability an agent can be TOLD about in one line.
Make the failure output actionable — the message below names the exact one-time host command,
because an agent that reads "connection refused" will try again, and an agent that reads "run this
script on the host" will escalate correctly and stop burning ticks.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

NAME = "template"
HOST = os.environ.get("TEMPLATE_BRIDGE_HOST", "host.docker.internal")
PORT = os.environ.get("TEMPLATE_BRIDGE_PORT", "18190")
BASE = f"http://{HOST}:{PORT}"

# Token: environment first, else the secret file the host setup script generated. The container
# reads it through the read-only .secrets mount — it is never baked into the image.
TOKEN = os.environ.get("TEMPLATE_BRIDGE_TOKEN", "")
if not TOKEN:
    _sec = os.path.join(os.path.dirname(__file__), "..", "..", ".secrets", f"{NAME}-bridge.env")
    try:
        with open(_sec) as fh:
            for line in fh:
                if line.startswith("TEMPLATE_BRIDGE_TOKEN="):
                    TOKEN = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass

_SETUP_HINT = (
    f"{NAME} bridge is DOWN at {BASE}.\n"
    f"  ONE-TIME HOST ACTION: bash <repo>/tools/bridge-template/host-bridge-setup.sh\n"
    f"  Already installed? Restart it:  launchctl kickstart -k gui/$(id -u)/org.enclave.{NAME}-bridge\n"
    f"This is a HOST capability — it cannot be fixed from inside the container."
)


def call(path, payload=None, timeout=120):
    """POST (or GET for /health) and return the parsed body. Exits 1 with a fix, never hangs."""
    url = BASE + path
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    req.add_header("Content-Type", "application/json")
    if TOKEN:
        req.add_header("Authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # A bridge error is a REAL answer — surface it rather than treating it as "bridge down".
        try:
            return json.loads(e.read() or b"{}")
        except Exception:
            return {"ok": False, "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError):
        print(_SETUP_HINT, file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description=f"{NAME} bridge client (runs in the container)")
    ap.add_argument("--health", action="store_true", help="is the bridge up and the capability usable")
    ap.add_argument("--sysinfo", action="store_true", help="the example capability — replace me")
    ap.add_argument("--echo", default="", help="text echoed back by --sysinfo")
    a = ap.parse_args()

    if a.health:
        print(json.dumps(call("/health"), indent=2))
        return 0
    if a.sysinfo:
        out = call("/sysinfo", {"echo": a.echo})
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
