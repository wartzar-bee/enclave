#!/usr/bin/env python3
"""
supervision.py — liveness heartbeats for the host supervision daemons.

The fleet MONITOR already publishes a heartbeat, but the GUARDIAN, spawn-watcher and control-watcher
had none — and the console inferred "watcher running" from a DIRECTORY existing, which its own launcher
pre-creates, so the check was always true. That is the "host daemon tier silently unloads" failure: a
launchd daemon can be unloaded/crashed and nothing notices (1 of ~31 daemons was loaded once and no
surface showed it). Give each daemon a real heartbeat and a single staleness check.

Convention: every supervision heartbeat is `<reports>/<name>-heartbeat.json` in ONE reports dir that
the beaters and the checker both resolve identically — `ENCLAVE_REPORTS_DIR`, else
`<parent-of-ENCLAVE_FLEET_ROOT>/reports` (where the guardian already writes its log/state), else
`~/.enclave/reports`. Set ENCLAVE_REPORTS_DIR in the launchd plists to guarantee alignment.

beat() is fail-soft: a heartbeat write must NEVER crash the daemon it is reporting on.

Usage:
  supervision.py --check [--max-age 300]     # print stale/missing daemons; exit 1 if any stale
  supervision.py --beat <name>               # write one heartbeat (manual/testing)
  supervision.py --selftest
"""
import argparse, json, os, pathlib, sys, time
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import statefile

# the daemons this module tracks (the monitor publishes its own heartbeat separately)
DAEMONS = ("fleet-guardian", "spawn-watcher", "control-watcher")
# default staleness ceiling: a supervision daemon should beat far more often than this
DEFAULT_MAX_AGE = 300


def reports_dir():
    d = os.environ.get("ENCLAVE_REPORTS_DIR")
    if d:
        return pathlib.Path(d).expanduser()
    fr = os.environ.get("ENCLAVE_FLEET_ROOT")
    if fr:
        return pathlib.Path(fr).expanduser().parent / "reports"
    return pathlib.Path.home() / ".enclave" / "reports"


def beat(name, interval=None, now=None):
    """Write <reports>/<name>-heartbeat.json. Fail-soft: never raises (a heartbeat write must not take
    down the daemon). `now` is injectable for tests."""
    try:
        d = reports_dir()
        d.mkdir(parents=True, exist_ok=True)
        statefile.write_json(d / f"{name}-heartbeat.json",
                             {"name": name, "ts": int(now if now is not None else time.time()),
                              "pid": os.getpid(), "interval": interval},
                             trailing_newline=False)
    except Exception:
        pass


def stale(max_age=DEFAULT_MAX_AGE, now=None, dir=None, daemons=DAEMONS):
    """Return [(name, age_or_None)] for daemons whose heartbeat is MISSING (age None) or OLDER than
    max_age. Empty list = every tracked daemon is fresh."""
    now = int(now if now is not None else time.time())
    d = pathlib.Path(dir) if dir else reports_dir()
    out = []
    for name in daemons:
        f = d / f"{name}-heartbeat.json"
        try:
            ts = int(json.loads(f.read_text()).get("ts", 0))
            age = now - ts
            if age > max_age:
                out.append((name, age))
        except Exception:
            out.append((name, None))
    return out


def _selftest():
    import tempfile
    fails = []
    def ck(n, c):
        if not c: fails.append(n)

    with tempfile.TemporaryDirectory() as d:
        os.environ["ENCLAVE_REPORTS_DIR"] = d
        try:
            # nothing beaten yet → all three missing
            s0 = stale(max_age=300, now=1000)
            ck("all-missing", {n for n, _ in s0} == set(DAEMONS) and all(a is None for _, a in s0))

            beat("fleet-guardian", now=1000)
            beat("spawn-watcher", now=1000)
            beat("control-watcher", now=1000)
            # all fresh at the same instant
            ck("all-fresh", stale(max_age=300, now=1000) == [])
            # within the window
            ck("within-window", stale(max_age=300, now=1200) == [])
            # past the ceiling (400 > 300) all three are stale, with real ages reported
            s2 = stale(max_age=300, now=1400)
            ck("all-stale-past-ceiling", {n for n, _ in s2} == set(DAEMONS))
            ck("stale-reports-age", all(isinstance(a, int) and a == 400 for _, a in s2))
            # refresh one → only the other two stale
            beat("fleet-guardian", now=1400)
            names = {n for n, _ in stale(max_age=300, now=1500)}
            ck("refresh-clears-one", "fleet-guardian" not in names and "spawn-watcher" in names)
            # beat is fail-soft on an impossible dir
            os.environ["ENCLAVE_REPORTS_DIR"] = "/proc/nonexistent/cannot/write"
            beat("spawn-watcher")  # must not raise
            ck("beat-failsoft", True)
        finally:
            os.environ.pop("ENCLAVE_REPORTS_DIR", None)

    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Supervision-daemon heartbeats.")
    ap.add_argument("--check", action="store_true", help="report stale/missing daemons; exit 1 if any")
    ap.add_argument("--beat", metavar="NAME", help="write one heartbeat and exit")
    ap.add_argument("--max-age", type=int, default=DEFAULT_MAX_AGE)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if a.beat:
        beat(a.beat)
        return 0
    if a.check:
        bad = stale(max_age=a.max_age)
        if not bad:
            print(f"supervision: all {len(DAEMONS)} daemons fresh (< {a.max_age}s) in {reports_dir()}")
            return 0
        for name, age in bad:
            print(f"  STALE  {name}: " + ("no heartbeat file" if age is None else f"last beat {age}s ago"))
        print(f"\n{len(bad)} supervision daemon(s) stale/missing — check launchd (they may be unloaded).")
        return 1
    ap.error("give --check, --beat NAME, or --selftest")


if __name__ == "__main__":
    sys.exit(main())
