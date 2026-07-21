#!/usr/bin/env python3
"""fleet_guardian.py — off-Opus fleet uptime guardian (framework primitive, 2026-07-21).

One-shot: verify every DECLARED-watch pod is running; auto-restart any that exited (docker compose
up -d agent from its fleet dir, which preserves override mounts — the enclave CLI restart drops them),
and record it. Runs under launchd StartInterval=60 so it survives its own crashes and needs NO Opus in
the loop (the 136M-burn rule). Complementary to fleet_monitor: the MONITOR watches an up pod's BEHAVIOUR
(wander/stall/bloat); the GUARDIAN watches a pod's EXISTENCE — the gap that let 4 pods sit DEAD for
26 minutes when a stray self-stop timer killed them.

DECLARE-then-DIFF, no hardcoded pod list: the watch-set is manifest agents with `watch: true` (opt-in →
a deliberately-stopped pod is never resurrected). A `fleet/<pod>/.guardian-off` file also excludes one.
It also flags manifest-registration drift (a fleet dir with no manifest entry → #7 "shown as standalone").

Paths are env-configurable so the framework ships no deployment constants:
  ENCLAVE_FLEET_ROOT (default <repo>/fleet) · ENCLAVE_MANIFEST (default ~/.config/enclave/fleet.json) ·
  GUARDIAN_ESC_LOG (default <fleet>/studio/home/state/escalations.log).

Run:  python3 fleet_guardian.py            (one-shot; launchd calls this every 60s)
      python3 fleet_guardian.py --install  (write + load the launchd plist)
"""
import argparse
import json
import os
import subprocess
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLEET_ROOT = os.environ.get("ENCLAVE_FLEET_ROOT", os.path.join(_REPO, "fleet"))
ROOT = os.path.dirname(FLEET_ROOT)
MANIFEST = os.environ.get("ENCLAVE_MANIFEST", os.path.expanduser("~/.config/enclave/fleet.json"))
LOG = os.path.join(ROOT, "reports", "fleet-guardian.log")
STATE = os.path.join(ROOT, "reports", "fleet-guardian-state.json")
STUDIO_ESC = os.environ.get("GUARDIAN_ESC_LOG",
                            os.path.join(FLEET_ROOT, "studio", "home", "state", "escalations.log"))
LABEL = "org.enclave.fleetguardian"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")


def _watched_pods():
    """Watch-set is DECLARED, not hardcoded: manifest agents with supervision `watch: true` (OPT-IN, so a
    deliberately-stopped pod like stoneforge is never resurrected — the failure mode is 'unwatched', never
    'wrongly restarted'). A `.guardian-off` file in the pod dir is a runtime override that also excludes it.
    Empty/unreadable manifest → watch nothing (fail safe, never guess a pod list)."""
    try:
        agents = json.loads(open(MANIFEST).read()).get("agents", {})
    except Exception:
        return []
    return [aid for aid, a in agents.items()
            if a.get("watch") is True
            and not os.path.exists(os.path.join(FLEET_ROOT, aid, ".guardian-off"))]


def _registration_drift():
    """#7 fix: a fleet/<pod> dir with a compose file but NO manifest entry silently shows as
    'standalone' and is never supervised. Flag it (declare-don't-guess — don't auto-invent a manager)."""
    try:
        known = set(json.loads(open(MANIFEST).read()).get("agents", {}))
    except Exception:
        return []
    unreg = []
    if os.path.isdir(FLEET_ROOT):
        for d in sorted(os.listdir(FLEET_ROOT)):
            pd = os.path.join(FLEET_ROOT, d)
            has_compose = any(os.path.exists(os.path.join(pd, f))
                              for f in ("docker-compose.yml", "docker-compose.yaml"))
            if has_compose and d not in known and not d.endswith("-chat"):
                unreg.append(d)
    return unreg


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(msg):
    line = f"{_now()} {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")
    return line


def _status(pod):
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Status}}", pod],
                           capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() or "absent"
    except Exception:
        return "unknown"


def _restart(pod):
    d = os.path.join(FLEET_ROOT, pod)
    try:
        r = subprocess.run(["docker", "compose", "up", "-d", "agent"], cwd=d,
                           capture_output=True, text=True, timeout=120)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-200:]
    except Exception as e:
        return False, str(e)


def _escalate(pod, detail):
    # Surface a genuine down-and-recovered event to the studio decision queue / monitor.
    try:
        with open(STUDIO_ESC, "a") as f:
            f.write(f"{_now()} ESCALATE :: [fleet-guardian] {pod} was DOWN and auto-restarted "
                    f"by the guardian — {detail}. If this repeats, something is killing the pod "
                    f"(OOM, a stray stop-timer, or a crash loop) — investigate the cause.\n")
    except OSError:
        pass


def check(install_note=False):
    state = {}
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE))
        except Exception:
            state = {}
    events = []
    watched = _watched_pods()
    if not watched:
        print(f"{_now()} [guardian] no pods declared watch:true in manifest — watching nothing (fail-safe)")
        return 0
    for pod in watched:
        st = _status(pod)
        if st == "running":
            state[pod] = {"status": "running", "ts": _now()}
            continue
        # down → heal
        ok, detail = _restart(pod)
        msg = _log(f"[guardian] {pod} was '{st}' → restart {'OK' if ok else 'FAILED'} :: {detail}")
        events.append(msg)
        _escalate(pod, f"was '{st}', restart {'OK' if ok else 'FAILED'}")
        state[pod] = {"status": "restarted" if ok else "restart-failed", "was": st, "ts": _now()}
    # #7: flag fleet dirs missing from the manifest (escalate only NEW ones — no re-spam)
    unreg = _registration_drift()
    prev_unreg = set(state.get("_unregistered", []))
    for d in unreg:
        if d not in prev_unreg:
            with open(STUDIO_ESC, "a") as f:
                f.write(f"{_now()} ESCALATE :: [fleet-guardian] '{d}' has a compose file but NO manifest "
                        f"entry — it shows as 'standalone' and is UNSUPERVISED. Register it in "
                        f"{os.path.basename(MANIFEST)} (manager + watch) so it's tracked.\n")
    state["_unregistered"] = unreg
    with open(STATE, "w") as f:
        json.dump(state, f, indent=2)
    if events:
        print("\n".join(events))
    else:
        print(f"{_now()} [guardian] all {len(watched)} watched pods running")
    return 0


def install():
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>python3</string>
    <string>{os.path.abspath(__file__)}</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>ENCLAVE_FLEET_ROOT</key><string>{FLEET_ROOT}</string>
    <key>ENCLAVE_MANIFEST</key><string>{MANIFEST}</string>
    <key>GUARDIAN_ESC_LOG</key><string>{STUDIO_ESC}</string>
  </dict>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>{os.path.join(ROOT, "reports", "fleet-guardian.out")}</string>
  <key>StandardErrorPath</key><string>{os.path.join(ROOT, "reports", "fleet-guardian.err")}</string>
</dict></plist>
"""
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "w") as f:
        f.write(plist)
    subprocess.run(["launchctl", "unload", PLIST], capture_output=True)
    r = subprocess.run(["launchctl", "load", PLIST], capture_output=True, text=True)
    print(f"installed {PLIST}\nload rc={r.returncode} {r.stderr.strip()}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--install", action="store_true")
    a = ap.parse_args()
    sys.exit(install() if a.install else check())
