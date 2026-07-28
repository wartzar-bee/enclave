#!/usr/bin/env python3
"""Hermetic tests for tick_feeder.py's injection decision (next_injection) — the graduated
budget warnings + the 2026-07-04 turn-cap wrap-up (57 forgepod ticks / $111 died at
error_max_turns with the truncated work re-derived next tick; the wrap-up injection banks the
work before the guillotine). Run: python3 test_tick_feeder.py
"""
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from tick_feeder import next_injection, epoch_decision


def fresh():
    return {"w1": False, "w2": False, "stop": False, "turnwrap": False}


def _dec(**kw):
    """epoch_decision with lean-and-working defaults; tests override one lever at a time."""
    d = dict(subtype_ok=True, ctx_tokens=50_000, cost=1.0, soft=6.0, epoch_tokens=140_000,
             increments=2, max_increments=8, elapsed=600, wall_sec=5400,
             status={"status": "continue"}, has_work=True, cap_ok=True,
             stop_sent=False, handoff_fresh=False)
    d.update(kw)
    return epoch_decision(**d)


# ── epoch_decision: the continue/finalize/close matrix (2026-07-26 context epochs) ──
def test_epoch_lean_and_working_continues():
    a, r = _dec()
    assert a == "continue", (a, r)


def test_epoch_silent_status_with_open_work_continues():
    a, _ = _dec(status=None)
    assert a == "continue"


def test_epoch_occupancy_cap_ends_it_even_with_work():
    a, r = _dec(ctx_tokens=140_000)
    assert a == "finalize" and "context" in r
    a, _ = _dec(ctx_tokens=140_000, handoff_fresh=True)   # handoff already banked → no extra cycle
    assert a == "close"


def test_epoch_soft_budget_ends_it():
    a, r = _dec(cost=6.0)
    assert a == "finalize" and "soft" in r


def test_epoch_increment_cap_ends_it():
    a, _ = _dec(increments=8)
    assert a == "finalize"


def test_epoch_wall_ends_it():
    a, _ = _dec(elapsed=5400)
    assert a == "finalize"


def test_epoch_cap_floor_ends_it():
    a, r = _dec(cap_ok=False)
    assert a == "finalize" and "window" in r


def test_epoch_agent_idle_or_blocked_closes_without_wrap():
    assert _dec(status={"status": "idle"})[0] == "close"
    assert _dec(status={"status": "blocked", "waiting_on": "operator"})[0] == "close"


def test_epoch_session_clear_closes():
    a, r = _dec(status={"status": "continue", "session": "clear"})
    assert a == "close" and "clear" in r


def test_epoch_error_closes():
    assert _dec(subtype_ok=False)[0] == "close"


def test_epoch_stop_already_sent_closes():
    """The budget STOP ladder already forced the handoff — never inject more work after it."""
    assert _dec(stop_sent=True, cost=99)[0] == "close"


def test_epoch_no_work_closes():
    a, r = _dec(status=None, has_work=False)
    assert a == "close" and "no open work" in r


def test_epoch_full_context_never_buys_another_increment():
    """Stop conditions outrank work-remains: a declared continue at cap occupancy must not run on."""
    a, _ = _dec(ctx_tokens=999_999, status={"status": "continue"}, has_work=True)
    assert a in ("finalize", "close")


def test_quiet_below_soft():
    assert next_injection(cost=1.0, turn=5, soft=2.5, hard=4.0, max_turns=60, sent=fresh()) is None


def test_w1_at_soft_once():
    s = fresh()
    assert next_injection(2.5, 5, 2.5, 4.0, 60, s) == "w1"
    s["w1"] = True
    assert next_injection(2.6, 6, 2.5, 4.0, 60, s) is None       # dedup


def test_w2_at_60pct_between_soft_and_hard():
    s = fresh(); s["w1"] = True
    w2_at = 2.5 + (4.0 - 2.5) * 0.6
    assert next_injection(w2_at - 0.01, 5, 2.5, 4.0, 60, s) is None
    assert next_injection(w2_at, 5, 2.5, 4.0, 60, s) == "w2"


def test_stop_at_hard_beats_everything():
    s = fresh()
    assert next_injection(4.0, 48, 2.5, 4.0, 60, s) == "stop"    # not turnwrap, not w1


def test_turnwrap_at_80pct_of_max_turns():
    s = fresh()
    assert next_injection(0.5, 47, 2.5, 4.0, 60, s) is None      # 47 < 48
    assert next_injection(0.5, 48, 2.5, 4.0, 60, s) == "turnwrap"
    s["turnwrap"] = True
    assert next_injection(0.5, 55, 2.5, 4.0, 60, s) is None      # dedup


def test_turnwrap_disabled_without_max_turns():
    assert next_injection(0.5, 999, 2.5, 4.0, 0, fresh()) is None


def test_turnwrap_floor_for_tiny_caps():
    # 0.8*3 = 2.4 → floor keeps the trigger at turn ≥ 3, never mid-warm-up
    assert next_injection(0.0, 2, 2.5, 4.0, 3, fresh()) is None
    assert next_injection(0.0, 3, 2.5, 4.0, 3, fresh()) == "turnwrap"


def test_no_turnwrap_after_stop():
    s = fresh(); s["stop"] = True
    assert next_injection(5.0, 50, 2.5, 4.0, 60, s) is None


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n}/{n} passed")
