#!/usr/bin/env python3
"""
tools/gcloud/bridge.py — runs IN THE CONTAINER.

Container client for the host's Google Cloud SDK, served by
tools/gcloud/host-bridge.py on host.docker.internal:18187. The container has no
gcloud and no Google credentials; this proxies gcloud/gsutil/bq to the Mac's
logged-in account. Stdlib-only (no requests), mirrors tools/transcribe/bridge.py.

Usage:
  python3 tools/gcloud/bridge.py --health
  python3 tools/gcloud/bridge.py <gcloud args...>
  python3 tools/gcloud/bridge.py --tool gsutil <gsutil args...>
  python3 tools/gcloud/bridge.py --tool bq <bq args...>

Examples:
  python3 tools/gcloud/bridge.py --health
  python3 tools/gcloud/bridge.py projects list
  python3 tools/gcloud/bridge.py config get-value project
  python3 tools/gcloud/bridge.py --tool gsutil ls gs://my-bucket

Everything after the recognized flags is passed verbatim as argv to the SDK CLI
on the host (executed without a shell — no injection). stdout/stderr/exit-code
are relayed transparently, so this script's own exit code matches gcloud's.

For a NATIVE feel, the setup also installs `gcloud`/`gsutil`/`bq` shims onto the
container PATH (see tools/gcloud/container-setup.sh) that just call this client.
"""
import os, sys, json, argparse, urllib.request, urllib.error

HOST = os.environ.get("GCLOUD_BRIDGE_HOST", "host.docker.internal")
PORT = os.environ.get("GCLOUD_BRIDGE_PORT", "18187")
BASE = f"http://{HOST}:{PORT}"

TOKEN = os.environ.get("GCLOUD_BRIDGE_TOKEN", "")
if not TOKEN:
    _sec = os.path.join(os.path.dirname(__file__), "..", "..", ".secrets", "gcloud-bridge.env")
    try:
        with open(_sec) as fh:
            for line in fh:
                if line.startswith("GCLOUD_BRIDGE_TOKEN="):
                    TOKEN = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass

_SETUP_HINT = (
    "gcloud bridge is down. ONE-TIME OPERATOR ACTION required:\n"
    "  Run on the Mac:  bash /path/to/workspace/tools/gcloud/host-bridge-setup.sh\n"
    "  Then authenticate once:  gcloud auth login   (and: gcloud config set project <id>)\n"
    "After that the container drives gcloud through the Mac autonomously."
)


def _req(method, path, data=None, timeout=900):
    h = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    if data is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.URLError as e:
        print(_SETUP_HINT, file=sys.stderr)
        print(f"(reason: {e})", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--health", action="store_true")
    ap.add_argument("--tool", default="gcloud", choices=["gcloud", "gsutil", "bq"])
    ap.add_argument("--timeout", type=int, default=600)
    a, rest = ap.parse_known_args()

    if a.health:
        print(json.dumps(_req("GET", "/health"), indent=2)); return

    if not rest:
        print("usage: bridge.py [--tool gcloud|gsutil|bq] <args...>  |  --health",
              file=sys.stderr)
        sys.exit(2)

    body = json.dumps({"tool": a.tool, "args": rest, "timeout": a.timeout}).encode()
    res = _req("POST", "/run", data=body, timeout=a.timeout + 30)

    # Relay output and exit code transparently.
    if res.get("stdout"):
        sys.stdout.write(res["stdout"])
    if res.get("stderr"):
        sys.stderr.write(res["stderr"])
    sys.exit(res.get("returncode", 1))


if __name__ == "__main__":
    main()
