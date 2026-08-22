"""Hermetic tests for diagnostics.compute — no I/O, fixed `now`, synthetic ticks.

Run: python3 test_diagnostics.py   (exits non-zero on any failure)
"""
import diagnostics as D

DAY = 86400
T0 = 1_700_000_000  # fixed anchor; never use wall-clock so trends are deterministic


def tick(t, ctx=50_000, out=8_000, cost=0.3, dur=120, turns=20, rc=0, subtype="success",
         reason="heartbeat", model="claude-sonnet-4-6"):
    """A usage record. ctx is split input/cache the way real ticks look (tiny input, big cache_read)."""
    input_, cache_read = 100, max(0, ctx - 100 - 1000)
    return {"ts": t, "reason": reason, "model": model, "input": input_, "output": out,
            "cache_read": cache_read, "cache_write": 1000, "cost_usd": cost,
            "duration_s": dur, "turns": turns, "rc": rc, "subtype": subtype}


def keys(d):
    return {a["key"] for a in d["anomalies"]}


def check(name, cond):
    if not cond:
        print(f"FAIL: {name}")
        check.failed += 1
    else:
        print(f"ok: {name}")
check.failed = 0


# --- cold start: no data --------------------------------------------------------------------
d = D.compute([], now=T0)
check("empty -> cold + unknown health", d["cold"] and d["health"]["level"] == "unknown"
      and d["ticks_total"] == 0)

# --- too-few ticks: no false anomalies ------------------------------------------------------
d = D.compute([tick(T0), tick(T0 + 100)], now=T0 + 100)
check("2 ticks -> no anomalies", d["anomalies"] == [])

# --- healthy steady agent -------------------------------------------------------------------
recs = [tick(T0 + i * DAY, ctx=60_000, cost=0.3, dur=120) for i in range(16)]
d = D.compute(recs, now=T0 + 15 * DAY)
check("steady -> green", d["health"]["level"] == "green")
check("steady -> no anomalies", d["anomalies"] == [])
check("steady -> 100% process success", d["honesty"]["process_success_pct"] == 100.0)
check("steady -> week window", d["window"] == "week")

# --- CONTEXT EXPLOSION: small last week, huge this week (observed in a long-running build pod) ----------------
last_week = [tick(T0 + i * (DAY // 2), ctx=80_000, cost=0.3) for i in range(8)]   # ~80k
this_week = [tick(T0 + 8 * DAY + i * (DAY // 4), ctx=3_500_000, cost=2.2) for i in range(8)]  # ~3.5M
d = D.compute(last_week + this_week, now=T0 + 10 * DAY)
check("explosion -> context_explosion anomaly", "context_explosion" in keys(d))
ce = next(a for a in d["anomalies"] if a["key"] == "context_explosion")
check("explosion -> high severity", ce["severity"] == "high")
check("explosion -> health orange/red", d["health"]["level"] in ("orange", "red"))
check("explosion -> trend reported", d["metrics"]["context"]["trend_pct"] and
      d["metrics"]["context"]["trend_pct"] > 500)

# --- RESOLVED explosion: huge history but latest tick is small -> NOT flagged ----------------
recovered = last_week + this_week[:-1] + [tick(T0 + 9 * DAY + 5000, ctx=90_000, cost=0.3)]
d = D.compute(recovered, now=T0 + 10 * DAY)
check("recovered -> no context_explosion (latest is small)", "context_explosion" not in keys(d))

# --- DURATION + COST spike on the latest tick -----------------------------------------------
base = [tick(T0 + i * DAY, ctx=60_000, cost=0.3, dur=100) for i in range(12)]
spike = base + [tick(T0 + 12 * DAY, ctx=60_000, cost=1.6, dur=600)]
d = D.compute(spike, now=T0 + 12 * DAY)
check("spike -> duration_spike", "duration_spike" in keys(d))
check("spike -> cost_spike", "cost_spike" in keys(d))

# --- FAILURES surface + drop health ---------------------------------------------------------
withfail = [tick(T0 + i * DAY, ctx=60_000) for i in range(8)] + \
           [tick(T0 + 8 * DAY, rc=1, subtype="error"),
            tick(T0 + 9 * DAY, rc=1, subtype="error"),
            tick(T0 + 10 * DAY, rc=1, subtype="error")]
d = D.compute(withfail, now=T0 + 10 * DAY)
check("failures -> anomaly", "failures" in keys(d))
check("failures(3) -> red health", d["health"]["level"] == "red")
check("failures -> process_success < 100", d["honesty"]["process_success_pct"] < 100)

# --- PROMPT CREEP: monotonic growth each tick (no single 2x jump) ----------------------------
creep = [tick(T0 + i * (DAY // 4), ctx=int(700_000 * (1.12 ** i)), cost=0.4) for i in range(8)]
d = D.compute(creep, now=T0 + 2 * DAY)
check("creep -> prompt_creep or context_explosion present",
      "prompt_creep" in keys(d) or "context_explosion" in keys(d))

# --- series + inspector shape ---------------------------------------------------------------
d = D.compute(base, now=T0 + 11 * DAY)
check("series labels align with context", len(d["series"]["labels"]) == len(d["series"]["context"]))
check("inspect newest-first", d["inspect"][0]["ts"] >= d["inspect"][-1]["ts"])
check("pending_telemetry advertised", "model latency" in d["pending_telemetry"])

# --- ts parsing: ISO and epoch both work ----------------------------------------------------
check("parse ISO Z", abs(D.parse_ts("2026-06-25T20:14:44Z") - 1782418484) < 2)
check("parse epoch", D.parse_ts(1782418484) == 1782418484.0)
check("parse junk -> None", D.parse_ts("not-a-date") is None)

# --- Phase C: runtime block aggregation -----------------------------------------------------
def rt_tick(t, calls=4, fails=0, files=1, deleg=0, compacts=0, tools=None, skills=None):
    r = tick(t)
    r["runtime"] = {"tool_calls": calls, "tool_failures": fails, "files_modified": files,
                    "delegations": deleg, "compactions": compacts,
                    "tools": tools or {"Bash": {"n": calls, "fail": fails, "ms": 800 * calls, "max_ms": 1200}},
                    "skills": skills or {}}
    return r

rtrecs = [rt_tick(T0 + i * DAY, calls=4, fails=0, files=1) for i in range(10)]
d = D.compute(rtrecs, now=T0 + 9 * DAY)
check("runtime available when blocks present", d["runtime"]["available"] is True)
check("runtime avg_tool_calls", d["runtime"]["avg_tool_calls"] == 4.0)
check("runtime per-tool latency computed", d["runtime"]["tools"][0]["avg_ms"] == 800)
check("pending drops covered items when runtime present", "per-tool latency" not in d["pending_telemetry"])
check("pending still lists genuinely-missing", "model latency" in d["pending_telemetry"])

# backward compat: no runtime block anywhere
d2 = D.compute([tick(T0 + i * DAY) for i in range(6)], now=T0 + 5 * DAY)
check("runtime unavailable on old records", d2["runtime"]["available"] is False)
check("pending lists per-tool latency when no runtime", "per-tool latency" in d2["pending_telemetry"])

# tool-failure anomaly
failrecs = [rt_tick(T0 + i * DAY, calls=5, fails=0) for i in range(6)] + \
           [rt_tick(T0 + 6 * DAY, calls=5, fails=3), rt_tick(T0 + 7 * DAY, calls=4, fails=2)]
d = D.compute(failrecs, now=T0 + 7 * DAY)
check("tool_failures anomaly fires", "tool_failures" in keys(d))

# compaction-churn anomaly
comprecs = [rt_tick(T0 + i * DAY) for i in range(6)] + \
           [rt_tick(T0 + 6 * DAY, compacts=1), rt_tick(T0 + 7 * DAY, compacts=2)]
d = D.compute(comprecs, now=T0 + 7 * DAY)
check("compaction_churn anomaly fires", "compaction_churn" in keys(d))

# mixed old+new records don't crash, runtime still available
mixed = [tick(T0 + i * DAY) for i in range(4)] + [rt_tick(T0 + (4 + i) * DAY) for i in range(4)]
d = D.compute(mixed, now=T0 + 7 * DAY)
check("mixed old+new records -> runtime available", d["runtime"]["available"] is True)

print()
if check.failed:
    print(f"{check.failed} FAILED")
    raise SystemExit(1)
print("ALL PASS")

# ── context bloat is measured PER TURN, not per tick (2026-07-21) ────────────────────────────
# Three pods escalated "context is bloating" every few ticks while their per-turn context sat flat
# at 19k-59k: total-per-tick is turns x context, so any tick past ~15 turns crossed the threshold.
# An alarm that fires on "did a lot of work" gets ignored, and the whole point of today was a real
# signal lost behind untrustworthy alarms. Both directions are pinned here.
def _ctxrec(now, i, ctx, turns):
    return {"ts": now - (20 - i) * 3600, "turns": turns, "cache_read": ctx, "input": 0,
            "cache_write": 0, "output": 500, "rc": 0, "subtype": "success", "cost_usd": 1.0,
            "duration_s": 60, "model": "claude-opus-4-8"}


def test_context_bloat_is_per_turn():
    import time as _t
    now = _t.time()
    healthy = [_ctxrec(now, i, 35_000 * t, t) for i, t in
               enumerate([8, 12, 9, 15, 11, 10, 14, 9, 12, 52])]
    keys = {a["key"] for a in D.compute(healthy, now)["anomalies"]}
    assert "context_explosion" not in keys, "a long tick at flat per-turn context must NOT alarm"
    bloated = [_ctxrec(now, i, pt * 12, 12) for i, pt in
               enumerate([20_000] * 8 + [90_000, 160_000])]
    anoms = [a for a in D.compute(bloated, now)["anomalies"] if a["key"] == "context_explosion"]
    assert anoms, "a per-turn prompt climbing 20k->160k MUST still alarm"
    assert "/turn" in anoms[0]["evidence"], "evidence should state the per-turn number"
    print("ok: context bloat measured per turn (long ticks silent, real bloat still caught)")


test_context_bloat_is_per_turn()


def test_epoch_bounded_context_is_not_bloat():
    """Adaptive ticks: per-turn context climbing toward CTX_EPOCH_TOKENS (default 140k) inside an
    epoch is the feeder's designed boundary, not bloat — only ABOVE the cap (+25% headroom) may
    context_explosion fire for records carrying `inc`. Legacy rows (no `inc`) keep the old rule."""
    import time as _t
    now = _t.time()

    def erec(i, ctx, turns, inc=1):
        r = _ctxrec(now, i, ctx, turns)
        if inc is not None:
            r["inc"] = inc
        return r

    designed = [erec(i, 400_000, 8) for i in range(8)] + [erec(8, 1_100_000, 9)]  # ~122k/turn
    keys = {a["key"] for a in D.compute(designed, now)["anomalies"]}
    assert "context_explosion" not in keys, "epoch increment under the boundary must NOT alarm"
    runaway = [erec(i, 400_000, 8) for i in range(8)] + [erec(8, 2_000_000, 10)]  # 200k/turn
    assert any(a["key"] == "context_explosion" for a in D.compute(runaway, now)["anomalies"]), \
        "per-turn context ABOVE the epoch cap MUST still alarm (feeder failed to cut)"
    legacy = [erec(i, 200_000, 8, inc=None) for i in range(8)] + [erec(8, 1_400_000, 10, inc=None)]
    assert any(a["key"] == "context_explosion" for a in D.compute(legacy, now)["anomalies"]), \
        "legacy (non-epoch) rows keep the old per-turn rule"
    print("ok: epoch-bounded context not flagged; runaway + legacy still caught")


test_epoch_bounded_context_is_not_bloat()


# ── a pod doing TOO LITTLE must alarm (2026-08-22) ───────────────────────────────────────────
# Every other detector fires on something getting bigger/slower/costlier; wake_spike even alarms on
# waking MORE often. Nothing detected under-work, so stoneforge sat at CONTINUOUS_COOLDOWN=2400 —
# 60s of work per 40min — looking green on every dashboard for weeks.
def _dutyrec(now, i, dur, gap):
    return {"ts": now - (10 - i) * gap, "turns": 10, "cache_read": 20_000, "input": 0,
            "cache_write": 0, "output": 500, "rc": 0, "subtype": "success", "cost_usd": 0.5,
            "duration_s": dur, "model": "claude-opus-4-8"}


def test_idle_pod_is_detected():
    import time as _t
    now = _t.time()
    idle = [_dutyrec(now, i, 60, 2400) for i in range(10)]      # 1-min ticks, 40-min gaps
    a = [x for x in D.compute(idle, now)["anomalies"] if x["key"] == "idle_pod"]
    assert a, "a pod working 2.8% of wall-clock MUST alarm"
    assert a[0]["severity"] == "high"
    assert "duty cycle" in a[0]["evidence"]
    assert "compose" in (a[0]["fix"] or ""), "the fix must point at compose FIRST — it beats agent.env"

    busy = [_dutyrec(now, i, 600, 900) for i in range(10)]      # 10-min ticks, 15-min gaps
    keys = {x["key"] for x in D.compute(busy, now)["anomalies"]}
    assert "idle_pod" not in keys, "a genuinely working pod must stay silent"

    short = [_dutyrec(now, i, 60, 60) for i in range(3)]        # too little history to judge
    keys = {x["key"] for x in D.compute(short, now)["anomalies"]}
    assert "idle_pod" not in keys, "under 5 ticks / 1h of span it must not guess"

    # A DELIBERATE daily cadence is not an idle pod. INTERVAL_SECONDS=86400 is a shipped default, and
    # a once-a-day 2-minute tick is 0.14% duty — lower than stoneforge's. Duty cycle alone cannot
    # tell the two apart, so the detector is gated on the pod re-firing on a WORK cadence.
    daily = [_dutyrec(now, i, 120, 86400) for i in range(10)]
    keys = {x["key"] for x in D.compute(daily, now)["anomalies"]}
    assert "idle_pod" not in keys, "a daily-heartbeat pod must NOT be called idle"
    print("ok: idle pods alarm, busy pods stay silent, thin history stays silent")


test_idle_pod_is_detected()
