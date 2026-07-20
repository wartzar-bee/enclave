"""diagnostics — the Agent Profiler (Dashboard Diagnostics tab, Phase A).

The moat of Enclave isn't *running* autonomous agents — it's **debugging** them. This module
turns the per-tick telemetry we already log (`home/state/usage.jsonl`) into answers to the
question an operator actually asks: "why is this agent slow / expensive / stuck?".

Every usage record carries:
  ts · reason · model · input · output · cache_read · cache_write · cost_usd · duration_s · turns · rc · subtype

From that alone (NO in-container runtime change) we derive:
  • Context size + growth   = input + cache_read + cache_write  (the context-explosion diagnostic)
  • Cache hit %             = cache_read / (input + cache_read + cache_write)
  • Cost / duration / turns / tokens per tick, with rolling averages
  • Week-over-week TRENDS   (recent 7d vs the prior 7d; falls back to recent-half vs earlier-half)
  • An ANOMALIES engine     (context grew Nx · prompt growing every tick · duration doubled ·
                             cost spike · re-cache churn · wake-frequency spike · recent failures)
  • A HEALTH score          (🟢🟡🟠🔴 + the single most important reason)
  • An HONESTY panel        (process success from rc/subtype; semantic "did the work pass?" is
                             Unknown for unsupervised agents — we never fake a quality score)

HONESTY RULE (extends "don't invent metrics" to diagnosis): a cause is asserted only when the
signal is unambiguous; otherwise we return evidence + the *possible* cause + a confidence level.
A confidently-wrong diagnosis is worse than none.

Phase C (needs in-container instrumentation → image rebuild) would add per-tool/model latency,
retries, tool-failures, delegation count, files-modified, compaction events. Those are surfaced
as explicit "pending telemetry" placeholders in the UI, never faked here.

Pure stdlib, no deps. `compute()` is a pure function of (records, now) so it is trivially testable.
"""
import os, json, pathlib, statistics
from datetime import datetime, timezone

DAY = 86400
# A tick's prompt-side context (what gets re-sent every turn) — the number that explodes.
def _context(r):     return _i(r, "input") + _i(r, "cache_read") + _i(r, "cache_write")
def _tokens(r):      return _context(r) + _i(r, "output")
def _cache_pct(r):
    c = _context(r)
    return (_i(r, "cache_read") / c) if c else 0.0

def _i(r, k):
    """A record field as a number, tolerant of nulls/strings/missing."""
    v = r.get(k)
    if v is None:
        return 0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


# Subtypes that are an INTENTIONAL stop, not a failure. error_max_turns = the tick hit its
# MAX_TURNS budget (rc=1) and stopped on purpose — it did productive work and resumes next tick.
# Counting it as a "failed tick" makes a deliberate cost cap read as breakage (false Degraded).
_EXPECTED_STOPS = {"error_max_turns"}

# Subtypes that mean the tick ENDED CLEANLY. The Claude path writes "success"; the BRAIN=api/local
# path (local_agent.py _finish) writes "ok". Accepting only "success" made every healthy api-brain
# tick count as failed — a live pod (9/10 ticks rc=0 subtype=ok) rendered as red "Failing /
# PROCESS SUCCESS 0%" on the console (found + fixed 2026-07-20, dashboard truth review T1).
_SUCCESS_SUBTYPES = {"success", "ok"}


def _tick_failed(r):
    """True only for a REAL process failure — excludes intentional MAX_TURNS caps."""
    if (r.get("subtype") or "success") in _EXPECTED_STOPS:
        return False
    return _i(r, "rc") != 0 or (r.get("subtype") or "success") not in _SUCCESS_SUBTYPES


def parse_ts(s):
    """ISO-8601 ('2026-06-25T20:14:44Z') or epoch seconds → epoch float. None on failure."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    try:
        t = str(s).replace("Z", "+00:00")
        return datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp() \
            if "+" not in t else datetime.fromisoformat(t).timestamp()
    except Exception:
        return None


def load_usage(home):
    """Read all parseable usage.jsonl records for an agent home, oldest-first."""
    p = pathlib.Path(home) / "state" / "usage.jsonl"
    out = []
    if not p.exists():
        return out
    for line in p.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    out.sort(key=lambda r: parse_ts(r.get("ts")) or 0)
    return out


def _trend(recent, baseline):
    """Percent change recent-vs-baseline, or None when there isn't enough history to judge."""
    if baseline is None or recent is None:
        return None
    if baseline == 0:
        return None
    return round((recent - baseline) / baseline * 100.0, 1)


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return (sum(xs) / len(xs)) if xs else None


def _windows(records, now):
    """Split into (recent, baseline) for week-over-week trends.

    Primary: recent = last 7d, baseline = the 7d before that (real calendar comparison).
    Fallback (sparse/young agents): later-half vs earlier-half, flagged so the UI says "vs earlier"
    rather than implying a true weekly comparison. None when there's too little history.
    """
    ts = [parse_ts(r.get("ts")) or now for r in records]
    recent = [r for r, t in zip(records, ts) if t >= now - 7 * DAY]
    baseline = [r for r, t in zip(records, ts) if now - 14 * DAY <= t < now - 7 * DAY]
    if len(baseline) >= 2 and len(recent) >= 2:
        return recent, baseline, "week"
    if len(records) >= 4:
        mid = len(records) // 2
        return records[mid:], records[:mid], "split"
    return records, [], "cold"


def _metric_block(recent, baseline, fn, window):
    """value (recent avg) + latest + trend% for one metric."""
    r_avg = _avg([fn(r) for r in recent])
    b_avg = _avg([fn(r) for r in baseline]) if baseline else None
    latest = fn(recent[-1]) if recent else None
    return {
        "avg": r_avg, "latest": latest,
        "trend_pct": _trend(r_avg, b_avg) if window != "cold" else None,
        "window": window,
    }


def _anomalies(records, now, window):
    """Detect the classic agent-debugging failure modes. Each anomaly carries its own evidence +
    a confidence level; we only name a *cause* when the signal is unambiguous."""
    out = []
    if len(records) < 3:
        return out
    latest = records[-1]
    prior = records[:-1]
    recent_n = prior[-10:] if len(prior) >= 3 else prior  # rolling baseline excluding latest

    def add(sev, key, title, evidence, confidence, cause=None, fix=None):
        out.append({"severity": sev, "key": key, "title": title, "evidence": evidence,
                    "confidence": confidence, "cause": cause, "fix": fix})

    # 1) Context explosion — the headline diagnostic. Latest prompt-side context vs the rolling avg.
    ctx_now = _context(latest)
    ctx_base = _avg([_context(r) for r in recent_n]) or 0
    if ctx_base > 0 and ctx_now >= 2.0 * ctx_base and ctx_now > 200_000:
        ratio = ctx_now / ctx_base
        # monotonic climb over the tail ⇒ high confidence it's a real growth trend, not one spike
        tail = [_context(r) for r in records[-5:]]
        mono = all(b >= a for a, b in zip(tail, tail[1:]))
        add("high" if ratio >= 3 else "med", "context_explosion",
            f"Context {ratio:.1f}× larger than recent average",
            f"context (input+cache) {_human(ctx_now)} vs ~{_human(ctx_base)} avg",
            "high" if mono else "med",
            cause=("the prompt is replaying an ever-larger history each tick" if mono else None),
            fix="compact memory / trim auto-loaded files; check for an unbounded log or inbox")

    # 1b) Context grew sharply WEEK-over-WEEK and/or sits very large every tick. The reviewer's
    #     StoneForge case (18k→142k): a sustained plateau, not a one-tick spike, so it needs its
    #     own check — it's the dominant cost driver and the #1 thing to debug.
    #     Gate on the CURRENT (latest) context, not the window average — an agent whose latest
    #     tick has dropped back to a small prompt is healthy NOW regardless of its history, and
    #     flagging it would be a confidently-wrong diagnosis.
    rec, base, win = _windows(records, now)
    ctx_recent = _avg([_context(r) for r in rec]) or 0
    ctx_base_w = _avg([_context(r) for r in base]) if base else None
    grew = _trend(ctx_recent, ctx_base_w)  # % week-over-week (None if no baseline)
    big = ctx_now > 1_000_000
    if not any(a["key"] == "context_explosion" for a in out) and ctx_now > 500_000 \
            and (big or (grew or 0) >= 150):
        sev = "high" if (ctx_now > 2_000_000 and (grew or 0) >= 150) else "med"
        ev = f"~{_human(ctx_now)} tokens this tick" + (
            f", {'up' if grew >= 0 else 'down'} {abs(grew):.0f}% vs {'last week' if win == 'week' else 'earlier'}"
            if grew is not None else "")
        # Diagnose the DRIVER from telemetry, not a guess. Most of the context is usually cache_read; if
        # the latest tick also ran many tool calls, the prompt is accumulating ACROSS a long tool-heavy
        # tick (re-read each turn) — NOT static files, so "trim memory/inbox" is the wrong fix.
        rt_l = latest.get("runtime") if isinstance(latest.get("runtime"), dict) else {}
        tcalls = int(rt_l.get("tool_calls") or 0)
        cr_share = (_i(latest, "cache_read") / ctx_now) if ctx_now else 0
        if tcalls >= 25 and cr_share >= 0.7:
            c_cause = (f"the prompt accumulates across a long, tool-heavy tick (~{tcalls} tool calls this "
                       "tick), re-read from cache each turn — this is the driver, not auto-loaded files")
            c_fix = ("reduce tool calls per tick (batch shell commands), delegate heavy build/test work to "
                     "the off-Opus worker, or scope ticks smaller. Trimming memory/inbox won't help — "
                     "they're a tiny fraction of this context.")
        else:
            c_cause = "context has grown sharply over time" if (grew or 0) >= 150 else None
            c_fix = "compact memory / trim auto-loaded files & inbox; this is the main cost driver"
        add(sev, "context_explosion", "Large context being re-sent every tick", ev,
            "high" if grew is not None else "med", cause=c_cause, fix=c_fix)

    # 2) Prompt growing EVERY tick — slow context creep (distinct from a single spike).
    tail = [_context(r) for r in records[-6:]]
    if len(tail) >= 5:
        incs = sum(1 for a, b in zip(tail, tail[1:]) if b > a)
        if incs >= len(tail) - 1 and tail[-1] > 1.4 * (tail[0] or 1):
            add("med", "prompt_creep", "Prompt size grows on nearly every tick",
                f"context climbed {_human(tail[0])} → {_human(tail[-1])} over {len(tail)} ticks",
                "high", cause="monotonic context growth",
                fix="something is accumulating in the prompt (memory/log/inbox not being trimmed)")

    # 3) Duration spike.
    d_now = _i(latest, "duration_s")
    d_base = _avg([_i(r, "duration_s") for r in recent_n]) or 0
    if d_base > 0 and d_now >= 2.0 * d_base and d_now > 120:
        add("med", "duration_spike", "Tick took much longer than usual",
            f"{_dur(d_now)} vs ~{_dur(d_base)} avg", "med",
            fix="check the log for a stuck tool call, retry loop, or a large batch of work")

    # 4) Cost spike.
    c_now = _i(latest, "cost_usd")
    c_base = _avg([_i(r, "cost_usd") for r in recent_n]) or 0
    if c_base > 0 and c_now >= 2.5 * c_base and c_now > 0.5:
        add("med", "cost_spike", "Tick cost well above the recent average",
            f"${c_now:.2f} vs ~${c_base:.2f} avg", "med",
            cause=("driven by the larger context" if ctx_now >= 2 * (ctx_base or 1) else None),
            fix="usually downstream of context growth or an Opus tier on a routine tick")

    # 5) Re-cache churn — high cache_write share means the cache keeps getting invalidated/rebuilt.
    cw = _i(latest, "cache_write")
    ctx = _context(latest)
    if ctx > 300_000 and cw / ctx > 0.25:
        add("low", "cache_churn", "High cache-write share (cache being rebuilt)",
            f"cache_write {_human(cw)} = {cw / ctx * 100:.0f}% of context", "med",
            fix="the prompt prefix is changing between ticks; stabilise the system/header content")

    # 6) Wake-frequency spike — inter-tick gaps shrinking sharply.
    gaps = []
    tss = [parse_ts(r.get("ts")) for r in records if parse_ts(r.get("ts"))]
    for a, b in zip(tss, tss[1:]):
        gaps.append(b - a)
    if len(gaps) >= 6:
        recent_gap = statistics.median(gaps[-3:])
        base_gap = statistics.median(gaps[:-3])
        if base_gap > 0 and recent_gap < 0.4 * base_gap and base_gap - recent_gap > 120:
            add("low", "wake_spike", "Waking up more often than before",
                f"~{_dur(recent_gap)} between ticks vs ~{_dur(base_gap)} before", "med",
                fix="check for a tight retry/heartbeat loop or a flood of directives")

    # 8) Tool failures (Phase C — only when runtime telemetry is present). A run of failing tools is
    #    a concrete "stuck" signal the process-level rc can't see (a tick can exit 0 with failed tools).
    rt_recent = [r.get("runtime") for r in records[-8:] if isinstance(r.get("runtime"), dict)]
    if rt_recent:
        # Failure rate over a TIGHT recent window (last 5 with-runtime ticks) so a fresh burst isn't
        # diluted by older clean ticks.
        rt_fail = [r.get("runtime") for r in records[-5:] if isinstance(r.get("runtime"), dict)]
        fails = sum(int(rt.get("tool_failures") or 0) for rt in rt_fail)
        calls = sum(int(rt.get("tool_calls") or 0) for rt in rt_fail)
        if fails >= 3 and calls and fails / calls >= 0.2:
            add("med", "tool_failures", "Tools are failing repeatedly",
                f"{fails} tool failures across the last {len(rt_fail)} ticks ({fails * 100 // calls}% of calls)",
                "high", fix="open Logs (Raw) for the failing tool calls — often a bad path, perm, or arg")
        # 9) Compaction churn — repeated auto-compaction means the context keeps overflowing the window.
        compacts = sum(int(rt.get("compactions") or 0) for rt in rt_recent)
        if compacts >= 2:
            add("med", "compaction_churn", "Context auto-compacted repeatedly",
                f"{compacts} compaction events in the last {len(rt_recent)} ticks",
                "high", cause="the context window keeps filling up",
                fix="trim auto-loaded files / memory so the prompt fits without compaction")

    # 7) Recent failures (process-level: rc!=0 or non-success subtype).
    failed = [r for r in records[-10:] if _tick_failed(r)]
    if failed:
        sev = "high" if len(failed) >= 3 else "med"
        # A 'no_result' subtype = the tick produced no final result, almost always because it hit its
        # time limit and was killed (the loop recovers next tick). Name that specifically — it's a
        # too-much-work-per-tick signal, not a crash to grep for.
        timeouts = sum(1 for r in failed if (r.get("subtype") or "") == "no_result")
        if timeouts:
            f_cause = (f"{timeouts} tick(s) ended with no result — the tick hit its time limit and was "
                       "killed (the loop recovers on the next tick)")
            f_fix = ("one tick is doing too much — break the work into smaller ticks or delegate the heavy "
                     "build/test loop off-Opus; open Logs (Raw) for the killed tick to see where it stalled")
        else:
            f_cause = None
            f_fix = "open Logs for the failing ticks to see the error"
        add(sev, "failures", f"{len(failed)} failed tick(s) in the last {min(10, len(records))}",
            "non-zero rc or non-success subtype", "high", cause=f_cause, fix=f_fix)

    sev_order = {"high": 0, "med": 1, "low": 2}
    out.sort(key=lambda a: sev_order.get(a["severity"], 3))
    return out


def _health(records, anomalies):
    """🟢🟡🟠🔴 from telemetry alone + the single most important reason. This is the *telemetry*
    verdict; the rail also factors reachability/up-state, which the console already knows."""
    if not records:
        return {"level": "unknown", "label": "No telemetry", "reason": "no usage records yet"}
    highs = [a for a in anomalies if a["severity"] == "high"]
    meds = [a for a in anomalies if a["severity"] == "med"]
    if any(a["key"] == "failures" and a["severity"] == "high" for a in anomalies):
        top = next(a for a in anomalies if a["key"] == "failures")
        return {"level": "red", "label": "Failing", "reason": top["title"]}
    if highs:
        return {"level": "orange", "label": "Degraded", "reason": highs[0]["title"]}
    if meds:
        return {"level": "yellow", "label": "Watch", "reason": meds[0]["title"]}
    if anomalies:
        return {"level": "yellow", "label": "Watch", "reason": anomalies[0]["title"]}
    return {"level": "green", "label": "Healthy", "reason": "no anomalies in recent telemetry"}


def _runtime_summary(records, window_n=20):
    """Aggregate the Phase-C `runtime` blocks (per-tool latency/failures, files-modified, delegations,
    compactions, skills) across the recent window. Returns {available: False} when no tick carries a
    runtime block (older records / older image) so the UI shows 'pending telemetry' honestly."""
    rts = [(r, r.get("runtime")) for r in records[-window_n:] if isinstance(r.get("runtime"), dict)]
    if not rts:
        return {"available": False}
    n = len(rts)
    tools = {}
    skills = {}
    tot = {"tool_calls": 0, "tool_failures": 0, "files_modified": 0, "delegations": 0, "compactions": 0}
    for _, rt in rts:
        for k in tot:
            tot[k] += int(rt.get(k) or 0)
        for name, t in (rt.get("tools") or {}).items():
            d = tools.setdefault(name, {"calls": 0, "fails": 0, "ms": 0, "max_ms": 0, "timed": 0})
            d["calls"] += int(t.get("n") or 0)
            d["fails"] += int(t.get("fail") or 0)
            d["ms"] += int(t.get("ms") or 0)
            d["max_ms"] = max(d["max_ms"], int(t.get("max_ms") or 0))
            if t.get("ms"):
                d["timed"] += int(t.get("n") or 0)
        for sk, c in (rt.get("skills") or {}).items():
            skills[sk] = skills.get(sk, 0) + int(c or 0)
    tool_rows = []
    for name, d in tools.items():
        tool_rows.append({"tool": name, "calls": d["calls"], "fails": d["fails"],
                          "avg_ms": int(d["ms"] / d["timed"]) if d["timed"] else None,
                          "max_ms": d["max_ms"] or None})
    tool_rows.sort(key=lambda r: r["calls"], reverse=True)
    return {
        "available": True,
        "ticks_with_data": n,
        "avg_tool_calls": round(tot["tool_calls"] / n, 1),
        "avg_tool_failures": round(tot["tool_failures"] / n, 2),
        "avg_files_modified": round(tot["files_modified"] / n, 1),
        "total_delegations": tot["delegations"],
        "total_compactions": tot["compactions"],
        "tools": tool_rows,
        "skills": skills,
    }


def _human(n):
    n = float(n or 0)
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}k"
    return f"{int(n)}"


def _dur(s):
    s = float(s or 0)
    if s >= 60:
        return f"{s / 60:.1f}m"
    return f"{s:.0f}s"


def compute(records, now=None, series_n=60, inspect_n=25):
    """The whole Diagnostics payload from a list of usage records. Pure: no I/O.

    `now` defaults to the latest record's timestamp (so trends are stable in tests/replays);
    pass an explicit epoch to anchor windows to wall-clock instead.
    """
    records = [r for r in records if isinstance(r, dict)]
    if now is None:
        tss = [parse_ts(r.get("ts")) for r in records if parse_ts(r.get("ts"))]
        now = max(tss) if tss else 0

    total = len(records)
    if total == 0:
        return {"cold": True, "ticks_total": 0,
                "health": {"level": "unknown", "label": "No telemetry",
                           "reason": "this agent hasn't logged any ticks yet"},
                "anomalies": [], "metrics": {}, "series": {"labels": []},
                "honesty": {}, "inspect": []}

    recent, baseline, window = _windows(records, now)
    anomalies = _anomalies(records, now, window)
    health = _health(records, anomalies)

    metrics = {
        "context": _metric_block(recent, baseline, _context, window),
        "cost":    _metric_block(recent, baseline, lambda r: _i(r, "cost_usd"), window),
        "duration": _metric_block(recent, baseline, lambda r: _i(r, "duration_s"), window),
        "tokens":  _metric_block(recent, baseline, _tokens, window),
        "turns":   _metric_block(recent, baseline, lambda r: _i(r, "turns"), window),
        "cache_pct": _metric_block(recent, baseline, _cache_pct, window),
        "output":  _metric_block(recent, baseline, lambda r: _i(r, "output"), window),
    }

    tail = records[-series_n:]
    series = {
        "labels": [_label(r) for r in tail],
        "context": [_context(r) for r in tail],
        "cache_read": [_i(r, "cache_read") for r in tail],
        "cache_write": [_i(r, "cache_write") for r in tail],
        "input": [_i(r, "input") for r in tail],
        "output": [_i(r, "output") for r in tail],
        "cost": [round(_i(r, "cost_usd"), 4) for r in tail],
        "duration": [round(_i(r, "duration_s"), 1) for r in tail],
        "turns": [_i(r, "turns") for r in tail],
    }

    failed = [r for r in records if _tick_failed(r)]
    capped = sum(1 for r in records if (r.get("subtype") or "") in _EXPECTED_STOPS)
    honesty = {
        "ticks_total": total,
        "ticks_failed": len(failed),
        "ticks_capped": capped,   # hit MAX_TURNS on purpose — counted as success, surfaced separately
        "process_success_pct": round((total - len(failed)) / total * 100.0, 1),
        "verification": "Unknown",
        "verification_note": ("semantic 'did the work pass?' is only tracked for supervised "
                              "agents (the off-Opus verify-gate); this is process success only"),
    }

    inspect = [_inspect_row(r) for r in records[-inspect_n:]][::-1]  # newest first
    runtime = _runtime_summary(records)

    # Whatever the runtime block now provides is no longer "pending". What remains genuinely needs
    # signals the stream doesn't carry (discrete model/API call timing, queue wait, a work-done verdict).
    pending = ["model latency", "queue wait", "retry count", "useful-vs-waiting split"]
    if not runtime.get("available"):
        pending = ["per-tool latency", "tool failures", "delegation count", "files modified",
                   "memory-compaction events", "skill usage"] + pending

    return {
        "cold": window == "cold",
        "window": window,
        "ticks_total": total,
        "health": health,
        "anomalies": anomalies,
        "metrics": metrics,
        "series": series,
        "honesty": honesty,
        "inspect": inspect,
        "runtime": runtime,
        "pending_telemetry": pending,
    }


def _label(r):
    """Short axis label: 'MM-DD HH:MM'."""
    t = parse_ts(r.get("ts"))
    if not t:
        return "?"
    return datetime.fromtimestamp(t, timezone.utc).strftime("%m-%d %H:%M")


def _inspect_row(r):
    return {
        "ts": r.get("ts"),
        "reason": r.get("reason"),
        "model": r.get("model"),
        "input": _i(r, "input"),
        "output": _i(r, "output"),
        "cache_read": _i(r, "cache_read"),
        "cache_write": _i(r, "cache_write"),
        "context": _context(r),
        "cache_pct": round(_cache_pct(r) * 100, 1),
        "cost_usd": round(_i(r, "cost_usd"), 4),
        "duration_s": round(_i(r, "duration_s"), 1),
        "turns": _i(r, "turns"),
        "rc": _i(r, "rc"),
        "subtype": r.get("subtype"),
    }


def from_home(home, now=None):
    """Convenience: read an agent's usage.jsonl and compute the payload."""
    return compute(load_usage(home), now=now)


if __name__ == "__main__":  # quick manual check: python3 diagnostics.py <home-dir>
    import sys
    home = sys.argv[1] if len(sys.argv) > 1 else "."
    print(json.dumps(from_home(home), indent=2))


# ── L2 work-product block (analytics plan; consumed by console /api/diagnostics) ───────────────
def workproduct(home, window=20):
    """Aggregate state/tick-scorecard.jsonl into the Diagnostics work-product panel. Returns
    {available: False} when the pod has no scorecard yet. All fields externally computed —
    this is the panel that finally answers the honesty gap ('green while producing nothing')."""
    import json as _json
    import pathlib as _pl
    f = _pl.Path(home) / "state" / "tick-scorecard.jsonl"
    try:
        lines = f.read_text(errors="replace").splitlines()[-window:]
    except OSError:
        return {"available": False}
    recs = []
    for ln in lines:
        try:
            recs.append(_json.loads(ln))
        except Exception:
            continue
    if not recs:
        return {"available": False}
    scored = [r for r in recs if r.get("config") == "ok"]
    prod_ticks = sum(1 for r in scored if (r.get("writes", {}).get("product") or 0) > 0)
    streak = 0
    for r in reversed(scored):
        if (r.get("writes", {}).get("product") or 0) > 0:
            break
        streak += 1
    churn = {}
    for r in recs:
        for p, n in (r.get("churn") or {}).items():
            churn[p] = churn.get(p, 0) + n
    served = [r for r in scored if r.get("serves_observed") is not None]
    return {
        "available": True,
        "window": len(recs),
        "blind": any(r.get("config") == "missing" for r in recs[-3:]),
        "product_rate": round(prod_ticks / len(scored), 2) if scored else None,
        "product_ticks": prod_ticks,
        "scored": len(scored),
        "zero_product_streak": streak,
        "top_churn": (max(churn.items(), key=lambda kv: kv[1]) if churn else None),
        "directive_service": (round(sum(1 for r in served if r["serves_observed"]) / len(served), 2)
                              if served else None),
        "plumbing_writes": sum((r.get("writes", {}).get("tooling") or 0)
                               + (r.get("writes", {}).get("self_state") or 0) for r in scored),
    }
