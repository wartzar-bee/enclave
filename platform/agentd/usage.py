#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────
# usage.py — rollups over per-agent state/usage.jsonl (written by usage_capture.py).
#
# Turns the append-only per-tick log into the numbers the dashboard meters and the
# budget guard need: tokens + cost over a window, broken down by model, per agent
# and fleet-wide. Pure stdlib, cheap (tail-scan with a date cutoff).
#
#   usage.py <agent-dir> --window today|7d|wtd|5h        → one agent
#   usage.py --fleet --window wtd                         → per-agent + fleet total
#   usage.py --fleet --window wtd --agents-root DIR       → override agents location
#   ... --pretty                                          → indented JSON
#
# Windows:
#   today = since 00:00 local today
#   7d    = trailing 7×24h
#   5h    = trailing 5h (the Claude session block window)
#   wtd   = week-to-date, anchored to the subscription weekly reset
#           (Tuesday 12:59 PM local by default; override --week-reset).
#
# Attribution, not ceiling: these token sums tell you WHICH agent ate the quota and
# its share — the absolute % of the subscription limit comes from Claude Code's own
# limit data (ccusage), surfaced separately by the guard / dashboard.
# ──────────────────────────────────────────────────────────────────────────
import argparse
import json
import os
import pathlib
import sys
import time
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_AGENTS_ROOT = HERE.parent / "agents"

# Token components summed into the headline "tokens" figure. Cache reads are cheap
# but real consumption, so include all four — the by-field breakdown stays available.
_TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write")


def _last_weekly_reset(now, reset_dow=1, reset_hour=12, reset_min=59):
    """Most recent weekly-reset boundary at-or-before `now`.
    reset_dow: Monday=0 … Sunday=6 (default Tuesday=1), local time. Default 12:59."""
    today_reset = now.replace(hour=reset_hour, minute=reset_min, second=0, microsecond=0)
    # days since the reset weekday (0..6), then back up to that day's reset time.
    days_back = (now.weekday() - reset_dow) % 7
    candidate = today_reset - timedelta(days=days_back)
    if candidate > now:  # reset weekday is today but the time hasn't passed → last week's
        candidate -= timedelta(days=7)
    return candidate


def window_cutoff(window, now=None, week_reset=(1, 12, 59)):
    """Return the epoch-seconds cutoff (inclusive lower bound) for a window, or None for 'all'."""
    now = now or datetime.now()
    if window == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "7d":
        start = now - timedelta(days=7)
    elif window == "5h":
        start = now - timedelta(hours=5)
    elif window == "wtd":
        start = _last_weekly_reset(now, *week_reset)
    elif window == "all":
        return None, now
    else:
        raise ValueError(f"unknown window: {window}")
    return start.timestamp(), start


def _parse_ts(s):
    """Parse an ISO-8601 'Z' timestamp → local-naive epoch seconds. Tolerant."""
    if not s:
        return None
    try:
        # Stored as UTC 'Z'; compare in epoch seconds (tz-agnostic).
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ")
        # treat as UTC
        return time.mktime(dt.timetuple()) - time.timezone
    except (ValueError, TypeError):
        return None


def _blank():
    d = {f: 0 for f in _TOKEN_FIELDS}
    d.update(tokens=0, cost_usd=0.0, ticks=0, cost_known_ticks=0)
    return d


def _add(acc, rec):
    tok = 0
    for f in _TOKEN_FIELDS:
        v = rec.get(f) or 0
        acc[f] += v
        tok += v
    acc["tokens"] += tok
    acc["ticks"] += 1
    c = rec.get("cost_usd")
    if c is not None:
        acc["cost_usd"] += c
        acc["cost_known_ticks"] += 1
    return tok


def rollup_file(path, cutoff_epoch):
    """Roll up one agent's usage.jsonl since cutoff_epoch. Returns (totals, by_model)."""
    totals = _blank()
    by_model = {}
    p = pathlib.Path(path)
    if not p.exists():
        return totals, by_model
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            ts = _parse_ts(rec.get("ts"))
            if cutoff_epoch is not None and (ts is None or ts < cutoff_epoch):
                continue
            _add(totals, rec)
            model = rec.get("model") or "unknown"
            _add(by_model.setdefault(model, _blank()), rec)
    return totals, by_model


def _finalize(totals, by_model):
    out = dict(totals)
    out["cost_usd"] = round(out["cost_usd"], 4)
    out["by_model"] = {
        m: {"tokens": v["tokens"], "cost_usd": round(v["cost_usd"], 4), "ticks": v["ticks"]}
        for m, v in sorted(by_model.items(), key=lambda kv: -kv[1]["tokens"])
    }
    return out


def _usage_path(agent_dir):
    return pathlib.Path(agent_dir) / "state" / "usage.jsonl"


def aggregate(agent_paths, cutoff_epoch):
    """Roll up MANY agents into a fleet total + per-agent breakdown. `agent_paths` = {name: usage.jsonl
    path}. Returns (fleet_finalized, {name: agent_finalized}). Each agent carries `share_pct` (token
    share) + `cost_share_pct` ($ share). The dashboard builds `agent_paths` from the live snapshot's
    homes (whatever the fleet layout); the CLI builds it from --agents-root."""
    fleet_tot = _blank()
    fleet_by_model = {}
    agents = {}
    for name, path in agent_paths.items():
        tot, bym = rollup_file(path, cutoff_epoch)
        if tot["ticks"] == 0:
            continue
        agents[name] = _finalize(tot, bym)
        for f in _TOKEN_FIELDS:
            fleet_tot[f] += tot[f]
        fleet_tot["tokens"] += tot["tokens"]
        fleet_tot["ticks"] += tot["ticks"]
        fleet_tot["cost_usd"] += tot["cost_usd"]
        fleet_tot["cost_known_ticks"] += tot["cost_known_ticks"]
        for m, v in bym.items():
            acc = fleet_by_model.setdefault(m, _blank())
            for f in _TOKEN_FIELDS:
                acc[f] += v[f]
            acc["tokens"] += v["tokens"]
            acc["ticks"] += v["ticks"]
            acc["cost_usd"] += v["cost_usd"]
    ftok = fleet_tot["tokens"] or 1
    fcost = fleet_tot["cost_usd"] or 1e-9
    for a in agents.values():
        a["share_pct"] = round(100.0 * a["tokens"] / ftok, 1)
        a["cost_share_pct"] = round(100.0 * a["cost_usd"] / fcost, 1)
    return _finalize(fleet_tot, fleet_by_model), agents


def _bucket_key(ts_epoch, bucket):
    lt = time.localtime(ts_epoch)
    return time.strftime("%Y-%m-%dT%H:00" if bucket == "hour" else "%Y-%m-%d", lt)


def series(agent_paths, cutoff_epoch, bucket="day", by="agent"):
    """Time-bucketed cost + tokens for charts. `by`: agent|model|reason. Returns
    {bucket, by, buckets:[label…], series:{key:{cost:[…],tokens:[…]}}} with every series aligned to the
    sorted bucket list (so the frontend can plot stacked/line directly)."""
    cells = {}      # bucket_label -> key -> {cost, tokens}
    keys = set()
    for name, path in agent_paths.items():
        p = pathlib.Path(path)
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                ts = _parse_ts(rec.get("ts"))
                if ts is None or (cutoff_epoch is not None and ts < cutoff_epoch):
                    continue
                key = (rec.get("model") or "unknown") if by == "model" else \
                      (rec.get("reason") or "?") if by == "reason" else name
                keys.add(key)
                cell = cells.setdefault(_bucket_key(ts, bucket), {}).setdefault(key, {"cost": 0.0, "tokens": 0})
                cell["tokens"] += sum((rec.get(f) or 0) for f in _TOKEN_FIELDS)
                c = rec.get("cost_usd")
                if c is not None:
                    cell["cost"] += c
    buckets = sorted(cells)
    keys = sorted(keys)
    out = {k: {"cost": [], "tokens": []} for k in keys}
    for b in buckets:
        for k in keys:
            cell = cells.get(b, {}).get(k, {"cost": 0.0, "tokens": 0})
            out[k]["cost"].append(round(cell["cost"], 4))
            out[k]["tokens"].append(cell["tokens"])
    return {"bucket": bucket, "by": by, "buckets": buckets, "series": out}


def api_rollup(path, cutoff_epoch):
    """Roll up REAL external-API spend from api_spending.jsonl (fields: ts, model, prompt_tokens,
    completion_tokens, usd) since cutoff. Returns {usd, calls, by_model:{m:{usd,calls}}}. OUT-OF-POCKET
    money (OpenRouter / NVIDIA / api+optimize pools) — distinct from Claude subscription usage in
    usage.jsonl (which counts against the cap, not the wallet)."""
    out = {"usd": 0.0, "calls": 0, "by_model": {}}
    p = pathlib.Path(path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, TypeError):
                continue
            ts = _parse_ts(rec.get("ts"))
            if cutoff_epoch is not None and (ts is None or ts < cutoff_epoch):
                continue
            usd = rec.get("usd") or 0
            out["usd"] += usd
            out["calls"] += 1
            bm = out["by_model"].setdefault(rec.get("model") or "unknown", {"usd": 0.0, "calls": 0})
            bm["usd"] += usd
            bm["calls"] += 1
    out["usd"] = round(out["usd"], 4)
    for m in out["by_model"]:
        out["by_model"][m]["usd"] = round(out["by_model"][m]["usd"], 4)
    return out


def last_record(path):
    """The most recent tick record (last non-blank JSON line), or {} — for the per-agent 'last tick
    cost / model / rc' column. Reads only the tail."""
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except (ValueError, TypeError):
            continue
    return {}


def main():
    ap = argparse.ArgumentParser(description="Roll up per-agent usage.jsonl.")
    ap.add_argument("agent_dir", nargs="?", help="an agent dir (omit with --fleet)")
    ap.add_argument("--fleet", action="store_true", help="aggregate all agents under --agents-root")
    ap.add_argument("--agents-root", default=str(DEFAULT_AGENTS_ROOT))
    ap.add_argument("--window", default="wtd", choices=["today", "7d", "5h", "wtd", "all"])
    ap.add_argument("--week-reset", default="1,12,59",
                    help="weekly reset as DOW,HOUR,MIN (Mon=0; default Tue 12:59 = 1,12,59)")
    ap.add_argument("--series", action="store_true", help="time-bucketed cost/tokens for charts")
    ap.add_argument("--bucket", default="day", choices=["day", "hour"])
    ap.add_argument("--by", default="agent", choices=["agent", "model", "reason"])
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    try:
        wr = tuple(int(x) for x in args.week_reset.split(","))
        assert len(wr) == 3
    except (ValueError, AssertionError):
        ap.error("--week-reset must be DOW,HOUR,MIN e.g. 1,12,59")

    cutoff_epoch, since_dt = window_cutoff(args.window, week_reset=wr)
    since_iso = since_dt.strftime("%Y-%m-%dT%H:%M:%S") if since_dt else None

    if args.fleet:
        root = pathlib.Path(args.agents_root)
        agent_paths = {adir.name: _usage_path(adir) for adir in sorted(root.glob("*"))
                       if _usage_path(adir).exists()}
        if args.series:
            out = {"window": args.window, "since": since_iso,
                   **series(agent_paths, cutoff_epoch, args.bucket, args.by)}
        else:
            fleet, agents = aggregate(agent_paths, cutoff_epoch)
            out = {"window": args.window, "since": since_iso, "fleet": fleet, "agents": agents}
    else:
        if not args.agent_dir:
            ap.error("provide an agent dir, or use --fleet")
        ap_paths = {pathlib.Path(args.agent_dir).name: _usage_path(args.agent_dir)}
        if args.series:
            out = {"window": args.window, "since": since_iso,
                   **series(ap_paths, cutoff_epoch, args.bucket, args.by)}
        else:
            tot, bym = rollup_file(_usage_path(args.agent_dir), cutoff_epoch)
            out = {"agent": pathlib.Path(args.agent_dir).name, "window": args.window, "since": since_iso}
            out.update(_finalize(tot, bym))

    json.dump(out, sys.stdout, indent=2 if args.pretty else None)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
