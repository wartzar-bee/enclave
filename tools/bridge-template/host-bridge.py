#!/usr/bin/env python3
"""
tools/bridge-template/host-bridge.py — runs ON THE HOST. Copy this to start a new bridge.

A BRIDGE gives a containerised agent a capability the container cannot have itself: hardware (GPU,
microphone), a logged-in application, an OS-level tool, or anything that must run as the human's
user. The host half is a small stdlib HTTP service; the container half is a thin client that POSTs
to it over `host.docker.internal`.

This template is a WORKING bridge. It exposes one trivial capability — `/sysinfo`, which reports
facts only the host knows — so you can install it, prove the whole path end to end, and then replace
the capability with your own. Nothing here is pseudo-code.

  Host:       python3 host-bridge.py          (or via launchd — see host-bridge-setup.sh)
  Container:  python3 bridge.py --health

TO MAKE THIS YOUR BRIDGE
  1. Pick a name and a free port (see docs/BRIDGES.md → Port registry). Replace TEMPLATE/template.
  2. Replace `_sysinfo()` with your capability, and `/sysinfo` with your endpoint.
  3. Declare in /health whether the capability is actually USABLE right now (see below).
  4. Copy host-bridge-setup.sh, adjust the label/port/paths, run it once on the host.

READ THIS BEFORE YOU WRITE THE CAPABILITY — a bridge is a deliberate hole in the container
boundary. Code here runs on the host, as the user, outside every sandbox the agent is under. The
container's isolation is not a limit on you; you are the limit on you.

  * TOKEN-GATE EVERY MUTATING ENDPOINT. `/health` may stay open (probes read it); nothing else.
  * TAKE NO PATH YOU DO NOT VALIDATE. `{"path": "../../.ssh/id_rsa"}` is the whole exploit. If you
    accept a path, resolve it and require it to sit under a declared root.
  * NEVER SHELL OUT TO A STRING THE CALLER CONTROLS. Build argv lists; no `shell=True`.
  * READ ONLY THE SECRET THAT IS YOURS. A bridge reads `.secrets/<name>-bridge.env` for its own
    token. Reaching into another credential file is how one capability becomes every capability.
  * RETURN ERRORS, DO NOT RAISE THEM. An agent mid-task must get `{"ok": false, "error": ...}`; a
    stack trace to a dead socket tells it nothing and it will retry forever.
"""
import json
import os
import platform
import shutil
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Name this bridge. Everything else — token var, secret file, launchd label — derives from it.
NAME = os.environ.get("TEMPLATE_BRIDGE_NAME", "template")
PORT = int(os.environ.get("TEMPLATE_BRIDGE_PORT", "18190"))
TOKEN = os.environ.get("TEMPLATE_BRIDGE_TOKEN", "")
# The host repo root, so relative paths from the container resolve to real host paths.
REPO = os.environ.get("TEMPLATE_BRIDGE_REPO",
                      os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))


def _capability_ready():
    """Is the underlying capability USABLE right now — not merely "is this process alive"?

    Report the dependency, not the daemon. A bridge whose /health says `ok` while its model file is
    missing or its CLI is not installed teaches an agent that the capability works, and it will keep
    calling and keep failing. Answer the question the agent actually has.
    """
    return {"python": platform.python_version(), "has_curl": bool(shutil.which("curl"))}


def _sysinfo(req):
    """THE CAPABILITY. Replace this. It returns things only the host can know.

    Note the shape: takes the parsed request dict, returns a plain dict, raises nothing the handler
    cannot turn into a clean JSON error.
    """
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "cpu": platform.processor() or platform.machine(),
        "repo": REPO,
        "echo": req.get("echo", ""),
    }


def _safe_host_path(p):
    """Resolve a caller-supplied path and REFUSE anything outside the repo root.

    Kept in the template because every bridge that touches files needs it and the naive version is
    always wrong: `os.path.join(REPO, p)` happily accepts `../../.ssh/id_rsa`, and a check written
    against the pre-resolution string misses symlinks. Resolve first, compare after.
    """
    full = os.path.realpath(p if os.path.isabs(p) else os.path.join(REPO, p))
    root = os.path.realpath(REPO)
    if not (full == root or full.startswith(root + os.sep)):
        raise PermissionError(f"path escapes the declared root: {p}")
    return full


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass                                   # quiet; the process manager captures stdout

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self):
        # An unset token means "not configured yet" and is allowed ONLY so the template runs before
        # setup. Your bridge should refuse to start without one — see host-bridge-setup.sh.
        return (not TOKEN) or self.headers.get("Authorization", "") == f"Bearer {TOKEN}"

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"ok": False, "error": "not found"})
        # /health is the contract every consumer relies on: is this bridge up, what is it, and can
        # it actually do the thing. Keep it cheap — probes call it on every agent boot.
        self._send(200, {"ok": True, "bridge": NAME, "port": PORT,
                         "capability": _capability_ready()})

    def do_POST(self):
        if not self._auth_ok():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if self.path == "/sysinfo":
                return self._send(200, {"ok": True, **_sysinfo(req)})
            return self._send(404, {"ok": False, "error": "not found"})
        except PermissionError as e:
            self._send(403, {"ok": False, "error": str(e)})
        except (KeyError, ValueError) as e:
            self._send(400, {"ok": False, "error": f"bad request: {e}"})
        except Exception as e:                 # never let a task-holding agent hang on a traceback
            self._send(500, {"ok": False, "error": str(e)})


if __name__ == "__main__":
    print(f"{NAME}-bridge on :{PORT}  token={'set' if TOKEN else 'UNSET (dev only)'}", flush=True)
    # 0.0.0.0 so Docker's host.docker.internal can reach it. On a shared or untrusted network bind
    # 127.0.0.1 instead and publish to the container another way — the token is your only gate.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
