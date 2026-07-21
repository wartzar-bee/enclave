#!/usr/bin/env python3
"""Hermetic tests for tick_feeder.py's injection decision (next_injection) — the graduated
budget warnings + the 2026-07-04 turn-cap wrap-up (57 forgepod ticks / $111 died at
error_max_turns with the truncated work re-derived next tick; the wrap-up injection banks the
work before the guillotine). Run: python3 test_tick_feeder.py
"""
import sys, pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from tick_feeder import next_injection


def fresh():
    return {"w1": False, "w2": False, "stop": False, "turnwrap": False}


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
