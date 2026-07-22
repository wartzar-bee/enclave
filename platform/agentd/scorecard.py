#!/usr/bin/env python3
"""
scorecard.py — per-tick L2 WORK-PRODUCT scorecard.

The gap this closes: L1 telemetry (usage.jsonl) says the loop ran; nothing said whether the tick
produced PRODUCT or plumbing. One deployment ran 56 green L1 ticks whose entire output
was 33 rewrites of its own rollup — invisible to every existing metric. This collector runs in
`post_tick_shared` (every brain path), is zero-LLM, and appends one record per tick to
`state/tick-scorecard.jsonl`.

Design laws applied (plan §0):
- The pod never scores itself: classification comes from spec-driven globs + events/mtime, all
  computed by this harness code post-tick. The in-pod file is FEEDBACK; the authoritative copy is
  mirrored host-side by the P1 collector.
- LOUD WHEN BLIND: no `state/scorecard-config.json` (or empty kpi_artifacts) → `"product": null`
  + `"config": "missing"`, never 0. A null propagates to the digest as "UNCONFIGURED", not as a
  passing grade.
- All windows are TICK-denominated (churn: ≥3 same-path writes in ONE tick, or ≥5 across the last
  10 records, fires — the 33× day was detectable at rewrite #3).

Config (`state/scorecard-config.json`, written by the orchestrator / spawn_watcher from the spec):
  { "kpi_artifacts":   ["content/**/*.md", "/workspace/ideas/scout/*.md", ...],
    "tooling_paths":   ["bin/**", "work/**/*.py"],        # optional; defaults below
    "self_state_paths":["state/**", ...] }                # optional; defaults below
Globs are agent-dir-relative unless absolute (in-container paths).

CLI:
  scorecard.py <agent-dir> --t0 <epoch>     # score the tick that started at t0 (runtime.sh $NOW)
  scorecard.py <agent-dir> summary [-n 20]  # aggregate the last n records (digest/console helper)
  scorecard.py --selftest                   # fixtures replay a real recorded day
"""
import argparse, calendar, glob as globmod, json, os, pathlib, re, sys, tempfile, time


def _utc_epoch(ts):
    """Epoch from an ISO-UTC 'YYYY-MM-DDTHH:MM:SS[Z]' string. calendar.timegm, NOT mktime —
    mktime treats the struct as LOCAL and drifts an hour under DST."""
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None

DEFAULT_SELF_STATE = ["state/**", "work.json", "inbox.md", "logs/**", "*.log", "tick.txt"]
DEFAULT_MEMORY = ["memory/**", "skills/**"]
DEFAULT_TOOLING = ["bin/**"]
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit"}
CHURN_TICK_FIRE = 3      # same path written ≥3× within one tick → churn alarm
CHURN_W10_FIRE = 5       # …or ≥5× across the last 10 records


def _read_jsonl(path, tail=None):
    try:
        lines = pathlib.Path(path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    if tail:
        lines = lines[-tail:]
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _load_config(base):
    f = base / "state" / "scorecard-config.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _norm(base, path):
    """Normalize an event/glob path to a comparable absolute-ish string."""
    p = str(path)
    if not p.startswith("/"):
        p = str(base / p)
    return os.path.normpath(p)


PRUNE = {"node_modules", ".git", ".venv", "venv", "dist", "build", ".next", ".nuxt",
         ".cache", "__pycache__", ".pnpm-store", "target", ".pytest_cache", "site-packages"}


def _glob_rx(pat):
    """Glob -> regex. `**` spans directories, `*` and `?` never cross a `/`."""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:[^/]+/)*"); i += 3
        elif pat.startswith("**", i):
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return re.compile("^" + "".join(out) + "$")


def _iter_files(pattern):
    """Files matching a glob, never descending into dependency trees.

    "Bounded: globs are expected to be targeted" was an ASSUMPTION, not a guard, and it did not
    hold: stoneforge's configured `work/**/apps/**/src/**` walks node_modules, and glob(recursive=
    True) on it does not return in any useful time. That hung the studio's host-side status tool
    for >110s; the same pattern is scored here every tick, so the hazard is the framework's too.
    A scorer that hangs fails exactly like a scorer that reports zero — the tick looks unproductive."""
    if "**" not in pattern:
        for f in globmod.glob(pattern):
            if os.path.isfile(f):
                yield f
        return
    head = pattern.split("**", 1)[0]
    root = head if os.path.isdir(head) else (os.path.dirname(head.rstrip("/")) or ".")
    if not os.path.isdir(root):
        return
    rx = _glob_rx(pattern)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE]
        for f in filenames:
            p = os.path.join(dirpath, f)
            if rx.match(p):
                yield p


def _glob_matches(base, patterns, since=None):
    """Paths matched by patterns; when `since` is set, only files with mtime >= since-1.
    Dependency dirs are pruned (see _iter_files) — targeting is enforced, not assumed."""
    hits = set()
    for pat in patterns or []:
        root_pat = pat if pat.startswith("/") else str(base / pat)
        try:
            for m in _iter_files(root_pat):
                if not os.path.isfile(m):
                    continue
                if since is not None:
                    try:
                        if os.path.getmtime(m) < since - 1:
                            continue
                    except OSError:
                        continue
                hits.add(os.path.normpath(m))
        except Exception:
            continue
    return hits


def _match_any(base, path, patterns):
    from fnmatch import fnmatch
    p = _norm(base, path)
    for pat in patterns or []:
        rp = pat if pat.startswith("/") else str(base / pat)
        rp = os.path.normpath(rp).replace("**/", "*").replace("**", "*")   # fnmatch has no **
        if fnmatch(p, rp) or fnmatch(p, os.path.normpath(pat if pat.startswith("/") else str(base / pat))):
            return True
    return False


def collect(base, t0, now=None):
    """Build one scorecard record for the tick that started at epoch t0. Pure-ish (fs reads only)."""
    base = pathlib.Path(base)
    now = now or time.time()
    cfg = _load_config(base)
    kpi = (cfg or {}).get("kpi_artifacts") or []
    tooling = (cfg or {}).get("tooling_paths") or DEFAULT_TOOLING
    self_state = (cfg or {}).get("self_state_paths") or DEFAULT_SELF_STATE
    memory_pats = DEFAULT_MEMORY

    # 1) write events THIS tick (per-path multiplicity → intra-tick churn). events.jsonl ts = epoch int.
    ev_writes = {}
    for ev in _read_jsonl(base / "state" / "events.jsonl", tail=800):
        if ev.get("event") != "tool" or ev.get("tool") not in WRITE_TOOLS:
            continue
        ts = ev.get("ts") or 0
        if not isinstance(ts, (int, float)) or ts < t0:
            continue
        p = (ev.get("summary") or "").strip()
        if p:
            ev_writes[_norm(base, p)] = ev_writes.get(_norm(base, p), 0) + 1

    # 2) mtime sweep over the CONFIGURED globs (catches bash-redirect writes events can't see).
    touched = set(ev_writes)
    touched |= _glob_matches(base, kpi, since=t0)
    touched |= _glob_matches(base, tooling, since=t0)
    # self_state/memory only counted from events + a cheap state/ scan (bounded dirs):
    touched |= _glob_matches(base, ["state/*", "state/**/*"], since=t0)

    # 3) classify (precedence: product > memory > tooling > self_state > other)
    buckets = {"product": 0, "memory": 0, "tooling": 0, "self_state": 0, "other": 0}
    product_paths = []
    for p in sorted(touched):
        if p.endswith("tick-scorecard.jsonl"):
            continue
        if kpi and _match_any(base, p, kpi):
            buckets["product"] += 1
            product_paths.append(os.path.relpath(p, base) if p.startswith(str(base)) else p)
        elif _match_any(base, p, memory_pats):
            buckets["memory"] += 1
        elif _match_any(base, p, tooling):
            buckets["tooling"] += 1
        elif _match_any(base, p, self_state):
            buckets["self_state"] += 1
        else:
            buckets["other"] += 1

    # LOUD WHEN BLIND: unconfigured product tracking is null, never 0.
    config_state = "ok" if kpi else "missing"
    product_val = buckets["product"] if kpi else None

    # 4) churn — tracks NON-product write counts (n>=1, top 10) because the REAL logan pattern
    # was one rollup rewrite per tick × 33 ticks: intra-tick multiplicity alone would miss it.
    # The 10-record window aggregation catches the cross-tick form; n>=3 in one tick catches the
    # intra-tick form. Product rewrites are excluded (revising a chapter is work, not churn).
    # Runtime bookkeeping the LOOP writes every tick by design — counting it as agent churn put
    # "tick-status.json 16×" at the top of a pod's churn panel (truth review T4). Not churn.
    # (state/rollup.md deliberately NOT excluded — per-tick rollup rewriting was the real churn
    # pattern this panel was built to catch; the agent writes it, not the loop.)
    BOOKKEEPING = {"state/tick-status.json", "state/.heartbeat", "state/recall.md",
                   "state/effective-config.json"}
    churn_all = {}
    for p, n in ev_writes.items():
        if kpi and _match_any(base, p, kpi):
            continue
        rel = os.path.relpath(p, base) if p.startswith(str(base)) else p
        if rel in BOOKKEEPING:
            continue
        churn_all[rel] = churn_all.get(rel, 0) + n
    churn_tick = dict(sorted(churn_all.items(), key=lambda kv: -kv[1])[:10])
    prior = _read_jsonl(base / "state" / "tick-scorecard.jsonl", tail=9)
    w10 = {}
    for r in prior:
        for p, n in (r.get("churn") or {}).items():
            w10[p] = w10.get(p, 0) + n
    for p, n in churn_tick.items():
        w10[p] = w10.get(p, 0) + n
    churn_alarm = any(n >= CHURN_TICK_FIRE for n in churn_tick.values()) or \
                  any(n >= CHURN_W10_FIRE for n in w10.values())

    # 5) directive service: the tick DECLARES serves in tick-status.json; observed = a product write
    #    (or a match on the directive's own `artifacts` globs when present in directives.json).
    serves, serves_valid, serves_observed = [], None, None
    try:
        st = json.loads((base / "state" / "tick-status.json").read_text())
        serves = st.get("serves") or []
        if isinstance(serves, str):
            serves = [serves]
    except Exception:
        pass
    active = {}
    try:
        dj = json.loads((base / "state" / "directives.json").read_text())
        active = {d.get("id"): d for d in dj.get("directives", [])
                  if isinstance(d, dict) and d.get("status") == "active"}
    except Exception:
        pass
    if serves:
        serves_valid = all(s in active for s in serves)
        globs = [g for s in serves for g in (active.get(s, {}).get("artifacts") or [])]
        if globs:
            serves_observed = any(_match_any(base, p, globs) for p in touched)
        elif (cfg or {}).get("product_measured_externally"):
            # This pod's product ships to an EXTERNAL platform (such a pod publishes its output to
            # Royal Road), so a LOCAL product write can neither prove nor disprove that it served a
            # directive. Unknown is the honest answer: False would assert "not serving" from a signal
            # that cannot see the work, and off_directive would then fire forever on a working pod.
            serves_observed = None
        else:
            serves_observed = (product_val or 0) > 0 if kpi else None

    # 6) verify-gated work completions this tick (work.json items flipped done with ts >= t0)
    done_this, done_verified = 0, 0
    try:
        for it in json.loads((base / "work.json").read_text()):
            if not isinstance(it, dict) or it.get("status") != "done":
                continue
            its = _utc_epoch(it.get("ts"))
            if its is not None and its >= t0 - 1:
                done_this += 1
                if "verify PASSED" in (it.get("evidence") or ""):
                    done_verified += 1
    except Exception:
        pass

    # 7) cost + subtype from the tick's usage record; cumulative cursors for cheap deltas
    cost, subtype = None, None
    for r in reversed(_read_jsonl(base / "state" / "usage.jsonl", tail=10)):
        rts = _utc_epoch(r.get("ts"))
        if rts is not None and rts >= t0 - 1 and r.get("reason") != "chat":
            cost, subtype = r.get("cost_usd"), r.get("subtype")
            break

    # 8) decision capture + CLAIM PROVENANCE (2026-07-20). Decisions logged this tick, and — the
    # fabrication tripwire — whether each decision's cited `evidence` is WITNESSED by the tick's
    # actual tool events. A pod once logged a decision citing web tests it NEVER ran (zero matching
    # events) and the invented "instrument failure" was believed for a day. Generalises the orchestrator's
    # experiments_lint idea from one log file to any claim an agent emits: an unwitnessed evidence
    # string doesn't prove fabrication, but it is exactly where a human should look first.
    decisions_tick, unwitnessed = 0, 0
    ev_blob = ""
    try:
        ev_blob = " ".join((str(e.get("summary", "")) + " " + str(e.get("result", "")))
                           for e in _read_jsonl(base / "state" / "events.jsonl", tail=800)
                           if isinstance(e.get("ts"), (int, float)) and e["ts"] >= t0).lower()
    except Exception:
        pass
    for d in _read_jsonl(base / "state" / "decisions.jsonl", tail=50):
        dts = _utc_epoch(d.get("ts"))
        if dts is None or dts < t0 - 1:
            continue
        decisions_tick += 1
        evid = str(d.get("evidence", "")).strip()
        if not evid or evid.lower() in ("none", "n/a"):
            continue                       # honestly-unevidenced is fine; tracked by the report
        # tokens worth witnessing: URLs, file paths, commands — any long token from the evidence
        toks = [t for t in re.split(r"[\s,;()\[\]{}'\"]+", evid)
                if len(t) >= 8 and ("/" in t or "." in t)]
        if toks and not any(t.lower() in ev_blob for t in toks):
            unwitnessed += 1

    def _lines(p):
        try:
            return sum(1 for _ in open(p, errors="replace"))
        except OSError:
            return 0

    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "t0": int(t0),
        "agent": os.environ.get("AGENT_ID", base.name),
        "reason": os.environ.get("TICK_REASON", ""),
        "config": config_state,
        "writes": {**buckets, "product": product_val},
        "product_paths": product_paths[:10],
        "churn": churn_tick,
        "churn_w10_top": sorted(w10.items(), key=lambda kv: -kv[1])[:3],
        "churn_alarm": churn_alarm,
        "serves": serves, "serves_valid": serves_valid, "serves_observed": serves_observed,
        "work_done": done_this, "work_done_verified": done_verified,
        "decisions": decisions_tick, "decisions_unwitnessed": unwitnessed,
        "tick_cost_usd": cost, "subtype": subtype,
        "cursors": {"escalations_lines": _lines(base / "state" / "escalations.log"),
                    "egress_lines": _lines(base / "state" / "egress-policy.log")},
    }


def append(base, rec):
    f = pathlib.Path(base) / "state" / "tick-scorecard.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")
    # bound the file (same pattern as usage.jsonl)
    lines = f.read_text(errors="replace").splitlines()
    if len(lines) > 2000:
        f.write_text("\n".join(lines[-2000:]) + "\n")


def summary(base, n=20):
    """Aggregate the last n records → the digest's MEASURED PERFORMANCE lines (≤3, or a loud
    UNCONFIGURED line). Returns [] when there is no data yet."""
    base = pathlib.Path(base)
    recs = _read_jsonl(base / "state" / "tick-scorecard.jsonl", tail=n)
    if not recs:
        return []
    lines = []
    if any(r.get("config") == "missing" for r in recs[-3:]):
        lines.append("product tracking UNCONFIGURED (state/scorecard-config.json missing) — "
                     "product output is NOT being measured; this is a defect, not a pass")
    finished = [r for r in recs if r.get("subtype") in ("ok", "success", None)]
    scored = [r for r in recs if r.get("writes", {}).get("product") is not None]
    if scored:
        prod_ticks = sum(1 for r in scored if (r["writes"]["product"] or 0) > 0)
        lines.append(f"product_rate {prod_ticks}/{len(scored)} ticks (last {len(recs)} recs)")
        streak = 0
        for r in reversed(scored):
            if (r["writes"]["product"] or 0) > 0:
                break
            streak += 1
        if streak >= 3:
            lines.append(f"zero-product streak: {streak} consecutive scored ticks — the KPI needs "
                         "an artifact, not plumbing")
    w10 = {}
    for r in recs[-10:]:
        for p, c in (r.get("churn") or {}).items():
            w10[p] = w10.get(p, 0) + c
    if w10:
        p, c = max(w10.items(), key=lambda kv: kv[1])
        if c >= CHURN_TICK_FIRE:
            lines.append(f"churn: {p} rewritten {c}x in the last 10 ticks — stop rewriting it")
    off = 0
    for r in reversed(recs):
        so = r.get("serves_observed")
        if so is True:
            break
        if so is False or (r.get("serves") == [] and r.get("writes", {}).get("product") == 0):
            off += 1
        else:
            break
    if off >= 3:
        lines.append(f"off-directive: {off} consecutive ticks served no active directive")
    return lines[:4]


# ── selftest: fixtures replay a real recorded day ──────────────────────────────
def _selftest():
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    def _ev(base, ts, tool, path):
        with (pathlib.Path(base) / "state" / "events.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": ts, "agent": "t", "event": "tool", "tool": tool,
                                 "summary": path}) + "\n")

    with tempfile.TemporaryDirectory() as td:
        b = pathlib.Path(td); (b / "state").mkdir(); (b / "content").mkdir()
        t0 = int(time.time()) - 60
        # F1: LOUD WHEN BLIND — no config → product is null + config missing, never 0.
        _ev(b, t0 + 5, "Write", "state/rollup.md")
        rec = collect(b, t0)
        check("blind-product-null", rec["writes"]["product"] is None)
        check("blind-config-missing", rec["config"] == "missing")
        # F2: the scribepod day — 33 rollup rewrites, a script 5×, ZERO product. Configured.
        (b / "state" / "scorecard-config.json").write_text(json.dumps(
            {"kpi_artifacts": ["content/**/*.md"], "tooling_paths": ["bin/**", "work/*.py"]}))
        for i in range(33):
            _ev(b, t0 + 6 + i, "Write", "state/rollup.md")
        for i in range(5):
            _ev(b, t0 + 40 + i, "Write", "work/scribblehub_profile_update.py")
        rec = collect(b, t0)
        check("logan-product-zero", rec["writes"]["product"] == 0)
        check("logan-churn-alarm", rec["churn_alarm"] is True)
        check("logan-churn-count", rec["churn"].get("state/rollup.md", 0) >= 33)
        append(b, rec)
        # F3: churn fires at the THIRD rewrite within a single tick (not the 33rd).
        b2 = pathlib.Path(td) / "b2"; (b2 / "state").mkdir(parents=True)
        (b2 / "state" / "scorecard-config.json").write_text(json.dumps({"kpi_artifacts": ["content/*.md"]}))
        for i in range(3):
            _ev(b2, t0 + i, "Write", "state/rollup.md")
        check("churn-fires-at-3", collect(b2, t0)["churn_alarm"] is True)
        # F4: a real product write via BASH REDIRECT (no Write event) is caught by the mtime sweep.
        ch = b / "content" / "ch18.md"; ch.write_text("chapter")
        rec = collect(b, t0)
        check("product-mtime-caught", rec["writes"]["product"] == 1 and
              any("ch18" in p for p in rec["product_paths"]))
        # F5: directive service — declared serves with a product write = observed.
        (b / "state" / "directives.json").write_text(json.dumps(
            {"directives": [{"id": "d1", "status": "active", "text": "x"}]}))
        (b / "state" / "tick-status.json").write_text(json.dumps({"status": "idle", "serves": ["d1"]}))
        rec = collect(b, t0)
        check("serves-valid", rec["serves_valid"] is True)
        check("serves-observed", rec["serves_observed"] is True)
        # F6: summary renders the deficiency lines (zero-product day → streak + churn lines).
        b3 = pathlib.Path(td) / "b3"; (b3 / "state").mkdir(parents=True)
        (b3 / "state" / "scorecard-config.json").write_text(json.dumps({"kpi_artifacts": ["content/*.md"]}))
        for i in range(5):
            _ev(b3, t0 + i * 2, "Write", "state/rollup.md")
            append(b3, collect(b3, t0 + i * 2 - 1))
        s = summary(b3)
        check("summary-zero-product", any("zero-product" in ln or "product_rate 0" in ln for ln in s))
        check("summary-churn", any("churn" in ln for ln in s))
        # F7: verify-gated done counted.
        (b / "work.json").write_text(json.dumps([{"id": 1, "text": "x", "status": "done",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "evidence": "y | verify PASSED: test -f x"}]))
        rec = collect(b, t0)
        check("done-verified", rec["work_done"] == 1 and rec["work_done_verified"] == 1)
    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK (12/12)")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?")
    ap.add_argument("cmd", nargs="?", default="collect", choices=["collect", "summary"])
    ap.add_argument("--t0", type=float, default=None)
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    if not a.base:
        ap.error("agent dir required (or --selftest)")
    if a.cmd == "summary":
        for ln in summary(a.base, a.n):
            print(f"- {ln}")
        return
    t0 = a.t0 or (time.time() - 3600)
    rec = collect(a.base, t0)
    append(a.base, rec)
    w = rec["writes"]
    print(f"scorecard: product={w['product']} tooling={w['tooling']} self_state={w['self_state']} "
          f"churn_alarm={rec['churn_alarm']} config={rec['config']}")


if __name__ == "__main__":
    main()
