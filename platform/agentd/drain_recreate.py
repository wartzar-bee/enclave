#!/usr/bin/env python3
"""
drain_recreate.py — recreate a pod at a TICK BOUNDARY, not mid-tick.

`docker compose up -d --build` (what `enclave update` / a redeploy does) SIGKILLs whatever the
container is doing. Agent hot state (work.json, memory index) is written truncate-in-place with no
fsync, so a recreate that lands mid-tick can tear it — and a torn work.json reads back as an EMPTY
queue, parking the agent on a full backlog. Until the durability work lands (statefile.py, Phase 1),
the safe way to ship a code/image change to a LIVE pod is to drain first:

  1. pause the pod (write state/paused) so no NEW tick starts;
  2. wait, bounded, for the CURRENT tick to finish (reuse fleet._state's host-side "working"
     detector — the same one the console badge uses, with its orphaned-start guard);
  3. recreate (docker compose up -d [--build] [--force-recreate]);
  4. restore the prior pause state (leave it paused only if the operator had already paused it).

Stdlib only; runs on the host (needs the docker CLI). Use per pod when deploying this branch —
especially the Dockerfile change, which needs a rebuild. Deploy is the operator's step.

Usage:
  drain_recreate.py --dir <deployment-dir> [--build] [--force-recreate] [--timeout 2700] [--poll 5]
  drain_recreate.py --selftest
"""
import argparse, calendar, os, pathlib, subprocess, sys, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import fleet  # stdlib-only; provides _agent_home / _env


def _tick_running(home, now=None):
    """Pause-AGNOSTIC 'a tick is in progress' check, read straight from runner.log markers — the SAME
    logic fleet._state uses but WITHOUT its pause override. Using fleet._state here was a real bug: it
    reports tick='paused' the instant the drain writes the pause file, so the wait loop saw 'not
    working' immediately and the recreate landed mid-tick — the exact tearing this module exists to
    prevent. `now` is injectable for tests."""
    try:
        lines = (pathlib.Path(home) / "logs" / "runner.log").read_text(errors="ignore").splitlines()[-1500:]
    except Exception:
        return False   # no log / can't read → nothing to wait for
    last_start = last_end = ""
    for l in lines:
        if "tick start" in l:
            last_start = l[:20]
        elif "tick end" in l or "tick TIMED OUT" in l:
            last_end = l[:20]
    if not (last_start and last_start > last_end):
        return False
    try:   # an orphaned 'tick start' (tick died without an end marker) must NOT latch working forever
        st = calendar.timegm(time.strptime(last_start[:19], "%Y-%m-%dT%H:%M:%S"))
        max_tick = int(os.environ.get("TICK_TIMEOUT", "2400")) + 600
        if ((now if now is not None else time.time()) - st) > max_tick:
            return False
    except Exception:
        pass
    return True


def recreate_cmd(dep_dir, build=False, force_recreate=False):
    """The compose command, built as data so it is unit-testable without docker."""
    cmd = ["docker", "compose", "--project-directory", str(dep_dir), "up", "-d"]
    if build:
        cmd.append("--build")
    if force_recreate:
        cmd.append("--force-recreate")
    return cmd


def drain_and_recreate(dep_dir, build=False, force_recreate=False, timeout=2700, poll=5,
                       _clock=time.time, _sleep=time.sleep, _run=subprocess.run):
    dep_dir = pathlib.Path(dep_dir).expanduser().resolve()
    env = {}
    try:
        env = fleet._env(str(dep_dir)) or {}
    except Exception:
        pass
    aid = env.get("AGENT_ID") or dep_dir.name
    home = fleet._agent_home(aid, str(dep_dir)) or (dep_dir / "home")
    home = pathlib.Path(home)
    paused = home / "state" / "paused"
    was_paused = paused.exists()
    we_paused = False

    print(f"[drain] {aid}: home={home}")
    try:
        if not home.is_dir():
            print(f"[drain] WARN home not found; recreating without drain")
        else:
            if not was_paused:
                paused.parent.mkdir(parents=True, exist_ok=True)
                paused.write_text(f"PAUSED for drain-recreate {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n")
                we_paused = True
                print(f"[drain] paused (no new ticks will start)")
            # wait for the ALREADY-RUNNING tick to finish (pause-agnostic detector, not fleet._state)
            deadline = _clock() + timeout
            waited = 0
            while _tick_running(home, now=_clock()):
                if _clock() >= deadline:
                    print(f"[drain] WARN tick still running after {timeout}s — proceeding anyway "
                          f"(a wedged tick would otherwise block deploy forever)")
                    break
                if waited == 0:
                    print(f"[drain] a tick is in progress — waiting for the boundary (≤{timeout}s)…")
                _sleep(poll)
                waited += poll
            else:
                if waited:
                    print(f"[drain] tick finished after ~{waited}s")

        cmd = recreate_cmd(dep_dir, build=build, force_recreate=force_recreate)
        print(f"[drain] {' '.join(cmd)}")
        rc = _run(cmd).returncode
        print(f"[drain] recreate rc={rc}")
        if rc == 0:   # an intentional recreate clears the guardian's do-not-restart flag
            try:
                osf = home / "state" / ".operator-stopped"
                if osf.exists():
                    osf.unlink()
            except Exception:
                pass
        return rc
    finally:
        # ALWAYS restore prior pause state — even if the recreate raised or the operator interrupted;
        # only unpause what WE paused, never a pod the operator had deliberately paused.
        if we_paused and paused.exists():
            try:
                paused.unlink()
                print(f"[drain] unpaused (restored to running)")
            except OSError as e:
                print(f"[drain] WARN could not remove pause file: {e}")
        elif was_paused:
            print(f"[drain] left paused (it was paused before)")


def _selftest():
    fails = []
    def ck(n, c):
        if not c: fails.append(n)

    # recreate_cmd builds the right compose invocation
    ck("cmd-basic", recreate_cmd("/x") == ["docker", "compose", "--project-directory", "/x", "up", "-d"])
    ck("cmd-build", "--build" in recreate_cmd("/x", build=True))
    ck("cmd-force", "--force-recreate" in recreate_cmd("/x", force_recreate=True))

    import tempfile
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    now = calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%S"))

    # REGRESSION for the pause-mask bug: _tick_running must be True EVEN WITH the pause file present
    # (fleet._state would report 'paused' and mask it). No monkeypatch — a real runner.log + pause file.
    with tempfile.TemporaryDirectory() as tmp:
        home = pathlib.Path(tmp) / "home"
        (home / "logs").mkdir(parents=True); (home / "state").mkdir(parents=True)
        log = home / "logs" / "runner.log"
        log.write_text(f"{ts}Z — tick start\n")           # a tick in progress, no end marker yet
        (home / "state" / "paused").write_text("x")        # pause file present
        ck("working-despite-pause", _tick_running(home, now=now) is True)
        with log.open("a") as f: f.write(f"{ts}Z — tick end\n")
        ck("not-working-after-end", _tick_running(home, now=now) is False)
        # an orphaned start (older than the tick window) reads not-running
        old = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 99999))
        log.write_text(f"{old}Z — tick start\n")
        ck("orphaned-start-not-working", _tick_running(home, now=now) is False)

    # drain loop over a REAL log: the tick ends after 2 polls (sleep appends the end marker)
    with tempfile.TemporaryDirectory() as tmp:
        dep = pathlib.Path(tmp) / "pod"
        home = dep / "home"
        (home / "logs").mkdir(parents=True); (home / "state").mkdir(parents=True)
        log = home / "logs" / "runner.log"
        log.write_text(f"{ts}Z — tick start\n")
        state = {"t": float(now), "polls": 0}
        def clock(): return state["t"]
        def sleep(s):
            state["t"] += s; state["polls"] += 1
            if state["polls"] == 2:
                with log.open("a") as f: f.write(f"{ts}Z — tick end\n")
        calls = {"n": 0}
        def run(cmd, **k): calls["n"] += 1; return type("R", (), {"returncode": 0})()
        rc = drain_and_recreate(dep, build=True, timeout=100, poll=5,
                                _clock=clock, _sleep=sleep, _run=run)
        ck("drained-then-recreated", rc == 0 and calls["n"] == 1)
        ck("waited-for-real-boundary", state["polls"] >= 2)
        ck("unpaused-after", not (home / "state" / "paused").exists())

    # unpause happens even if the recreate RAISES (try/finally), and we paused it
    with tempfile.TemporaryDirectory() as tmp:
        dep = pathlib.Path(tmp) / "pod"
        (dep / "home" / "state").mkdir(parents=True); (dep / "home" / "logs").mkdir(parents=True)
        def boom(cmd, **k): raise RuntimeError("docker exploded")
        try:
            drain_and_recreate(dep, _clock=lambda: 0.0, _sleep=lambda s: None, _run=boom)
        except RuntimeError:
            pass
        ck("unpaused-even-on-recreate-error", not (dep / "home" / "state" / "paused").exists())

    # a pod already paused stays paused
    with tempfile.TemporaryDirectory() as tmp:
        dep = pathlib.Path(tmp) / "pod"
        (dep / "home" / "state").mkdir(parents=True); (dep / "home" / "logs").mkdir(parents=True)
        (dep / "home" / "state" / "paused").write_text("operator paused")
        drain_and_recreate(dep, _clock=lambda: 0.0, _sleep=lambda s: None,
                           _run=lambda cmd, **k: type("R", (), {"returncode": 0})())
        ck("left-paused-if-preexisting", (dep / "home" / "state" / "paused").exists())

    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Recreate a pod at a tick boundary (drain first).")
    ap.add_argument("--dir", help="deployment directory (compose project dir)")
    ap.add_argument("--build", action="store_true", help="pass --build (needed for a Dockerfile change)")
    ap.add_argument("--force-recreate", action="store_true", dest="force_recreate")
    ap.add_argument("--timeout", type=int, default=2700, help="max seconds to wait for the tick boundary")
    ap.add_argument("--poll", type=int, default=5)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.dir:
        ap.error("--dir is required (or use --selftest)")
    return drain_and_recreate(a.dir, build=a.build, force_recreate=a.force_recreate,
                              timeout=a.timeout, poll=a.poll)


if __name__ == "__main__":
    sys.exit(main())
