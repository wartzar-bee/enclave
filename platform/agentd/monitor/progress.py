#!/usr/bin/env python3
"""progress.py — L4 WORK-PROGRESS verdicts: does each tick move the CURRENT GOAL forward?

The gap this closes (2026-08-22): L2's scorecard classifies writes (product/tooling/self_state) and
L3's diagnostics watches resources — but product-writes are not progress. Observed on stoneforge:
three ticks of audio re-theming and font swaps all counted `product` while every one of the six
art defects the operator had just named sat untouched, and the agent then declared "un-gated work
EXHAUSTED". Nothing in the stack compared the ACTIONS TAKEN to the DIRECTIVE GIVEN. This module
does exactly that, from data that already exists, zero-LLM, pure functions + a small CLI.

Config, not code — `state/progress-config.json` in the agent home:
  {
    "focus": ["work/**/apps/three-goats-tale/**"],   # globs the current directive's work lands in
    "goal_file": "state/qa/3goats/REF-DIFF-*.md",    # newest match is the goal's own scorecard
    "goal_metric": "DIFFERS",                        # count of lines containing this = open items
    "stall_ticks": 3                                 # consecutive non-forward ticks => stalled
  }
The goal file is one the DIRECTIVE itself defined (stoneforge's REF-DIFF acceptance step), so the
agent and the monitor read the same truth. Goal reached = metric count 0.

Verdicts per tick (one of, in priority order):
  forward   — product writes landed inside `focus`, or the goal metric DROPPED since last look
  off-goal  — product writes landed but none inside `focus`, while the goal metric is > 0
  quiet     — no product writes this tick (self_state/memory only)
Rolled up: `stalled` when the last `stall_ticks` verdicts contain no `forward`, plus root-cause
ATTRIBUTION for the "understand why" half: no-ticks → pacing/infra; quiet+blocked marker → what it
says it waits on; off-goal → the paths that landed vs the focus globs.

Usage:
  progress.py <agent-home> [-n 12] [--json]      # verdicts + rollup for the last n scorecard ticks
  progress.py --selftest
"""
import fnmatch
import glob as globmod
import json
import os
import pathlib
import re
import sys


def load_config(home):
    """Missing/garbled config is LOUD (None), never a default that silently grades everything ok."""
    try:
        d = json.loads((pathlib.Path(home) / "state" / "progress-config.json").read_text())
        return d if isinstance(d, dict) and d.get("focus") else None
    except Exception:
        return None


def goal_open_count(home, cfg):
    """Count open items in the NEWEST goal file. Returns (count, path, matched) — or None if no file.

    `matched` says the metric FOUND something, which is not the same as the count being > 0. It exists
    because exact-substring matching failed in the worst direction: a goal file written with
    `**DIFFERS**` inside other emphasis matched nothing, counted 0, and 0 means goal_reached means
    GREEN. A formatting slip read as success. Caller must never treat 0 as "reached" on the strength of
    the count alone.

    Matching is therefore forgiving: markdown emphasis stripped, case-insensitive, whole-word. The
    metric describes the OPERATOR'S prose, and prose drifts — the parser bends, or the signal lies.
    """
    pat = cfg.get("goal_file")
    if not pat:
        return None
    hits = sorted(globmod.glob(str(pathlib.Path(home) / pat)), key=os.path.getmtime)
    if not hits:
        return None
    token = cfg.get("goal_metric", "DIFFERS")
    try:
        text = pathlib.Path(hits[-1]).read_text(errors="replace")
    except OSError:
        return None
    bare = re.sub(r"[*_`~]", "", token).strip().lower()
    rx = re.compile(r"(?<![0-9a-z])" + re.escape(bare) + r"(?![0-9a-z])")
    n = 0
    for l in text.splitlines():
        if l.lstrip().startswith("#"):
            continue
        if rx.search(re.sub(r"[*_`~]", "", l).lower()):
            n += 1
    return n, hits[-1], n > 0


def newest_proposal(home, cfg):
    """The agent's own PROPOSAL file (its self-graded verdict) vs the operator's DISPOSAL (goal_file).

    Returns (path, pending) where pending means the newest proposal is NEWER than the goal file — i.e.
    the agent has graded itself and no human has ruled on it yet. Self-clears the moment the operator
    edits the goal file, so it needs no cursor and no 'seen' state of its own.

    Configured via progress-config "proposal_glob". Absent = the agent has no proposal step; not a fault.
    """
    pat = cfg.get("proposal_glob")
    if not pat:
        return None, False
    hits = globmod.glob(str(pathlib.Path(home) / pat))
    if not hits:
        return None, False
    newest = max(hits, key=os.path.getmtime)
    goal = cfg.get("goal_file")
    ghits = sorted(globmod.glob(str(pathlib.Path(home) / goal)), key=os.path.getmtime) if goal else []
    if not ghits:                       # no disposal file at all -> an ungraded proposal IS pending
        return newest, True
    try:
        return newest, os.path.getmtime(newest) > os.path.getmtime(ghits[-1])
    except OSError:
        return newest, False


def _in_focus(path, focus):
    # scorecard paths are agent-relative or absolute; match on both the path and its tail
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch("/" + path, g) for g in focus)


def tick_verdict(record, focus, goal_open, prev_goal_open):
    """PURE per-tick verdict. `record` is one tick-scorecard.jsonl row."""
    writes = record.get("writes") or {}
    product = int(writes.get("product") or 0)
    paths = [p for p in (record.get("product_paths") or []) if not p.endswith((".d.ts",))]
    hit = [p for p in paths if _in_focus(p, focus)]
    if goal_open is not None and prev_goal_open is not None and goal_open < prev_goal_open:
        return "forward"
    if product > 0 and hit:
        return "forward"
    if product > 0:
        return "off-goal"
    return "quiet"


def attribution(home, verdicts, records):
    """Root-cause hint for the newest non-forward state — WHY is it not moving?"""
    home = pathlib.Path(home)
    if not records:
        return ("no-ticks", "no scorecard records — the loop is not firing: check state/paused, then "
                            "pacing (CONTINUOUS_COOLDOWN/MIN_COOLDOWN in docker-compose*.yml BEATS "
                            "agent.env), then the container itself")
    if verdicts and verdicts[-1] == "forward":
        return ("moving", "latest tick moved the goal forward")
    blocked = home / "state" / ".blocked"
    if blocked.exists():
        try:
            why = json.loads(blocked.read_text()).get("waiting_on", "?")
        except Exception:
            why = "?"
        return ("blocked-declared", f"agent declares itself blocked on: {why} — verify that "
                                    f"dependency is still real before accepting it")
    if verdicts and verdicts[-1] == "off-goal":
        return ("directive-mismatch", "product landed OUTSIDE the focus globs while the goal is open "
                                      "— the agent is working, on the wrong thing; re-read its brief "
                                      "vs the directive")
    return ("idle-or-self-state", "ticks fire but write no product — check open approvals it may be "
                                  "waiting on, inbox directive age, and the idle_pod anomaly (pacing)")


def compute(home, n=12, advance_cursor=True):
    """advance_cursor=False = a PURE read. fleet_monitor polls this every cycle; if those polls
    moved the cursor they would consume the goal-DROP credit before the agent's own tick could be
    graded with it, and the CLI would then never show the forward verdict. Pollers pass False."""
    home = pathlib.Path(home)
    cfg = load_config(home)
    if cfg is None:
        return {"config": "missing", "note": "write state/progress-config.json {focus:[...], "
                                             "goal_file, goal_metric} — UNCONFIGURED, not passing"}
    records = []
    sc = home / "state" / "tick-scorecard.jsonl"
    if sc.exists():
        for l in sc.read_text(errors="replace").splitlines()[-n:]:
            try:
                records.append(json.loads(l))
            except Exception:
                pass
    goal = goal_open_count(home, cfg)
    goal_n, goal_f, matched_now = (goal if goal else (None, None, False))
    # Per-tick goal history is not recorded, so the metric-drop credit applies to the NEWEST tick
    # only (we compare against the previous invocation via a tiny cursor file).
    cur = home / "state" / ".progress-cursor.json"
    prev_goal, metric_seen = None, False
    try:
        _c = json.loads(cur.read_text())
        prev_goal, metric_seen = _c.get("goal_open"), bool(_c.get("metric_seen"))
    except Exception:
        pass
    # "the metric matched at some point" is sticky. Without it, 0 is ambiguous between GOAL MET and
    # METRIC BROKEN — and the ambiguity resolved to green, which is the wrong way for a detector to fail.
    metric_ok = metric_seen or matched_now
    verdicts = []
    for i, r in enumerate(records):
        last = i == len(records) - 1
        verdicts.append(tick_verdict(r, cfg["focus"], goal_n if last else None,
                                     prev_goal if last else None))
    if advance_cursor:
        try:
            cur.write_text(json.dumps({"goal_open": goal_n, "metric_seen": metric_ok}))
        except OSError:
            pass
    stall_n = int(cfg.get("stall_ticks", 3))
    stalled = len(verdicts) >= stall_n and "forward" not in verdicts[-stall_n:]
    why = attribution(home, verdicts, records)
    prop_f, prop_pending = newest_proposal(home, cfg)
    return {"config": "ok", "goal_open": goal_n, "goal_file": goal_f,
            "verdicts": verdicts, "stalled": stalled,
            # goal_reached requires the metric to have PROVEN it can match. A metric that never
            # matched anything is not a met goal; it is an instrument reading zero because it is broken.
            "goal_reached": bool(goal_n == 0 and metric_ok),
            "metric": "ok" if metric_ok else "unmatched",
            "cause": why[0], "detail": why[1],
            "proposal_file": prop_f, "proposal_pending": prop_pending}


def _selftest():
    import tempfile
    ok = [True]

    def ck(name, cond):
        if not cond:
            ok[0] = False
            print(f"  FAIL: {name}")

    F = ["work/**/apps/three-goats-tale/**"]
    rec = lambda prod, paths: {"writes": {"product": prod}, "product_paths": paths}
    ck("focus hit => forward", tick_verdict(
        rec(2, ["work/web-sdk/apps/three-goats-tale/src/x.svelte"]), F, None, None) == "forward")
    ck("product elsewhere while goal open => off-goal", tick_verdict(
        rec(3, ["work/web-sdk/apps/emberfall/src/x.ts"]), F, 6, 6) == "off-goal")
    ck("goal metric dropped => forward even with zero writes", tick_verdict(
        rec(0, []), F, 4, 6) == "forward")
    ck("nothing => quiet", tick_verdict(rec(0, []), F, None, None) == "quiet")
    ck("generated .d.ts alone is not a focus hit", tick_verdict(
        rec(1, ["work/web-sdk/apps/three-goats-tale/.svelte-kit/types/src/routes/$types.d.ts"]),
        F, None, None) == "off-goal")

    with tempfile.TemporaryDirectory() as d:
        h = pathlib.Path(d)
        (h / "state").mkdir()
        ck("missing config is LOUD", compute(h)["config"] == "missing")
        (h / "state" / "progress-config.json").write_text(json.dumps(
            {"focus": F, "goal_file": "state/qa/REF-DIFF-*.md", "goal_metric": "DIFFERS",
             "stall_ticks": 2}))
        (h / "state" / "qa").mkdir(parents=True)
        (h / "state" / "qa" / "REF-DIFF-t1.md").write_text("goats: DIFFERS x\nlows: DIFFERS y\nframe: MATCHES\n")
        rows = [rec(3, ["work/web-sdk/apps/emberfall/a.ts"]), rec(0, [])]
        (h / "state" / "tick-scorecard.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
        out = compute(h)
        ck("goal counted from newest file", out["goal_open"] == 2)
        ck("off-goal then quiet => stalled", out["stalled"] is True)
        ck("attribution names the mismatch shape", out["cause"] in ("idle-or-self-state", "directive-mismatch"))
        (h / "state" / ".blocked").write_text(json.dumps({"waiting_on": "operator answer"}))
        ck("declared blocker surfaces verbatim", "operator answer" in compute(h)["detail"])
    print("progress selftest:", "OK" if ok[0] else "FAILED")
    return 0 if ok[0] else 1


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        raise SystemExit(2)
    n = int(sys.argv[sys.argv.index("-n") + 1]) if "-n" in sys.argv else 12
    out = compute(args[0], n=n)
    print(json.dumps(out, indent=None if "--json" in sys.argv else 2))
    # exception-shaped exit code for watchers: 0 fine, 3 stalled, 4 unconfigured
    raise SystemExit(4 if out.get("config") != "ok" else (3 if out.get("stalled") else 0))


if __name__ == "__main__":
    main()
