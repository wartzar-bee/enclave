#!/usr/bin/env python3
"""Hermetic tests for agentloop.py — the wake decision + post-tick pacing (previously untested
liveness core), including the 2026-07-04 `blocked` state (fix #7: a blocked agent used to re-fire
paid continuous ticks forever — 8 back-to-back Opus WAIT ticks on stoneforge — because the only
park state was `idle` and the no-status default treats any open work.json item as workable).
Run: python3 test_agentloop.py
"""
import json, os, pathlib, sys, tempfile, time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from agentloop import Loop, due


# ── due(): the pure wake decision ───────────────────────────────────────────
def test_due_defer_window_blocks_everything():
    assert due(now=100, next_heartbeat=0, inbox_changed=True, comms_pending=True, defer_until=200) is None


def test_due_priority_comms_then_inbox_then_heartbeat():
    assert due(100, 0, True, True, 0) == "comms"
    assert due(100, 0, True, False, 0) == "inbox"
    assert due(100, 50, False, False, 0) == "heartbeat"
    assert due(100, 200, False, False, 0) is None


# ── _after(): post-tick pacing ──────────────────────────────────────────────
def make_loop(d):
    os.environ.setdefault("INTERVAL_SECONDS", "1800")
    lp = Loop(d)
    lp.interval = 1800
    lp.cont_cooldown = 600
    lp.min_cooldown = 300
    lp.log = lambda m: None
    return lp


def write_status(d, obj):
    (pathlib.Path(d) / "state").mkdir(parents=True, exist_ok=True)
    (pathlib.Path(d) / "state" / "tick-status.json").write_text(json.dumps(obj))


def test_after_blocked_parks_at_interval_and_writes_marker():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        write_status(d, {"status": "blocked", "waiting_on": "operator answer req-x"})
        t = time.time()
        lp._after(0)
        assert lp.next_heartbeat >= t + lp.interval - 2, (lp.next_heartbeat, t)   # parked, not continuous
        m = json.loads((pathlib.Path(d) / "state" / ".blocked").read_text())
        assert m["waiting_on"] == "operator answer req-x" and m["since"] >= int(t) - 2, m


def test_after_blocked_since_survives_repeat_blocks():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        bm = pathlib.Path(d) / "state" / ".blocked"
        (pathlib.Path(d) / "state").mkdir(parents=True, exist_ok=True)
        bm.write_text(json.dumps({"since": 12345, "waiting_on": "key"}))
        write_status(d, {"status": "blocked", "waiting_on": "key"})
        lp._after(0)
        assert json.loads(bm.read_text())["since"] == 12345    # original block time kept


def test_after_blocked_without_dependency_still_parks_but_no_marker():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        write_status(d, {"status": "blocked"})
        t = time.time()
        lp._after(0)
        assert lp.next_heartbeat >= t + lp.interval - 2       # still parked (cost-safe)
        assert not (pathlib.Path(d) / "state" / ".blocked").exists()   # but unnamed → not marked


def test_after_continue_clears_marker_and_paces_cooldown():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        bm = pathlib.Path(d) / "state" / ".blocked"
        (pathlib.Path(d) / "state").mkdir(parents=True, exist_ok=True)
        bm.write_text(json.dumps({"since": 1, "waiting_on": "x"}))
        write_status(d, {"status": "continue"})
        t = time.time()
        lp._after(0)
        assert not bm.exists()
        assert t + lp.min_cooldown - 2 <= lp.next_heartbeat <= t + lp.cont_cooldown + 2


def test_after_deferred_holds_baseline_and_backs_off():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        lp.cap_retry = 600
        t = time.time()
        lp._after(75)                                          # SKIP_RC
        assert lp.defer_until >= t + 600 - 2


def test_after_idle_parks_at_interval():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        write_status(d, {"status": "idle"})
        t = time.time()
        lp._after(0)
        assert lp.next_heartbeat >= t + lp.interval - 2


def test_after_no_status_empty_queue_idles():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        t = time.time()
        lp._after(0)                                           # no tick-status, no work.json
        assert lp.next_heartbeat >= t + lp.interval - 2


def test_after_no_status_open_work_continues():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        (pathlib.Path(d) / "work.json").write_text(json.dumps([{"id": "t", "status": "doing"}]))
        t = time.time()
        lp._after(0)
        assert lp.next_heartbeat <= t + lp.cont_cooldown + 2   # continuous, not parked


def test_after_session_clear_drops_warm_session_id():
    with tempfile.TemporaryDirectory() as d:
        lp = make_loop(d)
        sid = pathlib.Path(d) / "state" / "work-session.id"
        (pathlib.Path(d) / "state").mkdir(parents=True, exist_ok=True)
        sid.write_text("abc")
        write_status(d, {"status": "continue", "session": "clear"})
        lp._after(0)
        assert not sid.exists()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n}/{n} passed")
