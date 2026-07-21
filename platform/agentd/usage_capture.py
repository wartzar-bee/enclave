#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────
# usage_capture.py — stdin filter for `claude -p --output-format stream-json --verbose`.
#
# Sits in the pod tick pipeline (runtime.sh). It does TWO things at once:
#   1. Streams a human-readable rendering of the turn (assistant text + compact
#      tool-call / tool-result notes) to STDOUT → runner.log. No log regression:
#      a tick reads as it did before, just rendered from the JSON event stream.
#   2. Captures the final `result` event's FIRST-PARTY usage (Claude Code's own
#      numbers: usage tokens + total_cost_usd + duration + turns) into ONE JSON
#      line appended to state/usage.jsonl — accurate per-agent, per-tick.
#
# Usage (in runtime.sh, after the claude command):
#   claude -p "..." ... --output-format stream-json --verbose 2>>"$LOG" \
#     | python3 usage_capture.py --agent "$AGENT_ID" --reason "$TICK_REASON" \
#         --model "$MODEL_EFF" --out "$AGENT_DIR/state/usage.jsonl" >> "$LOG"
#   rc=${PIPESTATUS[0]}
#
# Fail-OPEN by contract (an autonomous loop must never wedge on a metrics bug):
#   - a malformed line is echoed raw and otherwise ignored;
#   - if NO result event arrives (crash / timeout-kill), we still append a record
#     with cost_usd=null and the tokens we saw, so the dashboard shows "unknown"
#     for that tick rather than silently losing it.
# Pure stdlib. No third-party imports.
# ──────────────────────────────────────────────────────────────────────────
import argparse
import json
import os
import pathlib
import sys
import time


def _compact(obj, limit=160):
    """One-line, length-capped repr of a tool input/result for the log."""
    try:
        s = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if not isinstance(obj, str) else obj
    except Exception:
        s = str(obj)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _emit_text(line):
    """Write a rendered log line to stdout, flushed so runner.log stays live."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


# Live context-budget signal — written per assistant turn so the ctx_budget HOOK can steer the agent and
# the DASHBOARD can show cost climbing MID-tick (instead of learning the bill at tick end). Best-effort;
# never break the stream. $/Mtok defaults are Opus-ish, env-overridable.
_RATE_IN  = float(os.environ.get("RATE_INPUT_PER_MTOK", "15"))
_RATE_CR  = float(os.environ.get("RATE_CACHE_READ_PER_MTOK", "1.5"))
_RATE_CW  = float(os.environ.get("RATE_CACHE_WRITE_PER_MTOK", "18.75"))
_RATE_OUT = float(os.environ.get("RATE_OUTPUT_PER_MTOK", "75"))


def _raw_est(cum):
    """Uncalibrated running $ estimate from the SUMMED per-turn tokens at the list rates above.
    Structurally biased HIGH vs the result event's authoritative total_cost_usd (measured ~7× on
    forgepod: real $0.43 read as $3.04) — every $ control gates on this number, which is why it
    gets CALIBRATED (below) instead of consumers inflating their caps to compensate."""
    return (cum["input"] * _RATE_IN + cum["cache_read"] * _RATE_CR
            + cum["cache_write"] * _RATE_CW + cum["output"] * _RATE_OUT) / 1e6


# ── Cost calibration (2026-07-04 enclave review fix #4) ────────────────────
# state/.cost-calibration.json {model: {ratio, n, ts}} — ratio = EMA of (authoritative
# total_cost_usd / raw estimate), learned at every result event. _emit_budget applies it so
# cost_est ≈ real dollars, which lets the budget caps (ctx_budget hook, tick_feeder inject,
# runtime auto-clear nets) be set as HONEST dollar amounts instead of 7×-inflated fudge.
# A model with no history uses ratio 1.0 (conservative: over-estimates → caps fire early, then
# self-corrects after the first completed tick).
def _load_cal(state_dir):
    try:
        d = json.loads((state_dir / ".cost-calibration.json").read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cal_ratio(cal, model):
    try:
        r = float((cal.get(model or "") or {}).get("ratio", 0) or 0)
        return r if r > 0 else 1.0
    except Exception:
        return 1.0


def _update_cal(state_dir, cal, model, raw_est, actual):
    """Fold one observed (actual / raw-estimate) into the model's EMA. Atomic write; best-effort."""
    try:
        if not model or raw_est <= 0 or not actual or actual <= 0:
            return
        obs = actual / raw_est
        e = cal.get(model) or {}
        n = int(e.get("n", 0) or 0)
        old = float(e.get("ratio", 0) or 0)
        ratio = obs if n == 0 or old <= 0 else (0.7 * old + 0.3 * obs)
        cal[model] = {"ratio": round(ratio, 4), "n": n + 1, "ts": int(time.time())}
        tmp = state_dir / ".cost-calibration.json.tmp"
        tmp.write_text(json.dumps(cal))
        tmp.replace(state_dir / ".cost-calibration.json")
    except Exception:
        pass


def _emit_budget(out_path, turn, ctx_tokens, cum, ratio=1.0):
    """Write state/.ctx-budget.json {turn, ctx_tokens(current occupancy), cost_est(calibrated
    running $), cost_raw(uncalibrated, for calibration debugging)}."""
    try:
        bp = pathlib.Path(out_path).parent / ".ctx-budget.json"
        raw = _raw_est(cum)
        bp.write_text(json.dumps({"ts": int(time.time()), "turn": turn,
                                  "ctx_tokens": int(ctx_tokens),
                                  "cost_est": round(raw * ratio, 4),
                                  "cost_raw": round(raw, 4)}))
        if turn == 1:  # fresh tick → re-arm the per-tick warning dedup the hook keys off
            try: (bp.parent / ".ctx-warned").unlink()
            except OSError: pass
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", "unknown"))
    ap.add_argument("--reason", default=os.environ.get("TICK_REASON", "heartbeat"))
    ap.add_argument("--model", default=os.environ.get("MODEL_EFF", ""))
    ap.add_argument("--out", required=True, help="path to state/usage.jsonl")
    args = ap.parse_args()

    # Accumulate what we observe so a missing result event still yields a record.
    seen_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    _turn = 0  # assistant-message count this tick (for the live budget signal)
    _cum = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}  # SUMMED (for the running $ estimate)
    _state_dir = pathlib.Path(args.out).parent
    _cal = _load_cal(_state_dir)   # per-model actual/estimate ratios (fix #4)
    seen_model = args.model or None
    result_written = False
    started = time.time()

    # Phase C runtime instrumentation — derived from the SAME stream, no extra cost. Latency is the
    # wall gap between a tool_use and its matching tool_result (≈ tool execution time). Back-compatible:
    # this rides under a "runtime" key, so older readers and older records are unaffected.
    rt = {"tool_calls": 0, "tool_failures": 0, "files_modified": 0, "delegations": 0,
          "compactions": 0, "tools": {}, "skills": {}, "models": {}}
    _open_tools = {}  # tool_use_id -> (name, t_start)
    FILE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit", "str_replace_editor"}

    def _tool(name):
        return rt["tools"].setdefault(name, {"n": 0, "fail": 0, "ms": 0, "max_ms": 0})

    for raw in sys.stdin:
        raw = raw.rstrip("\n")
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            # Not JSON (e.g. a stray non-stream line) — preserve it verbatim.
            _emit_text(raw)
            continue

        etype = ev.get("type")

        if etype == "system":
            sub = ev.get("subtype", "")
            if sub == "init":
                mdl = ev.get("model") or seen_model
                if mdl:
                    seen_model = mdl
                _emit_text(f"── init · model={mdl or '?'} · tools={len(ev.get('tools', []) or [])}")
            elif "compact" in (sub or ""):
                # Claude Code auto-compacted the context this tick (a sign the window filled up).
                rt["compactions"] += 1
                _emit_text(f"── context compacted ({sub})")
            continue

        if etype == "assistant":
            msg = ev.get("message", {}) or {}
            if msg.get("model"):
                seen_model = msg["model"]
            rt["models"][seen_model or "?"] = rt["models"].get(seen_model or "?", 0) + 1
            # Roll up the per-message usage Claude Code attaches (the result event
            # carries the authoritative totals; we keep these only as a fallback).
            u = msg.get("usage") or {}
            if u:
                seen_usage["input"] = max(seen_usage["input"], u.get("input_tokens", 0) or 0)
                seen_usage["output"] += u.get("output_tokens", 0) or 0
                seen_usage["cache_read"] = max(seen_usage["cache_read"], u.get("cache_read_input_tokens", 0) or 0)
                seen_usage["cache_write"] = max(seen_usage["cache_write"], u.get("cache_creation_input_tokens", 0) or 0)
                # live budget signal: current occupancy (this turn's prompt size; cache_read dominates +
                # is reliable) + a running $ estimate from the SUMMED tokens. Read by the hook + dashboard.
                _turn += 1
                _ti = u.get("input_tokens", 0) or 0; _tr = u.get("cache_read_input_tokens", 0) or 0
                _tw = u.get("cache_creation_input_tokens", 0) or 0
                _cum["input"] += _ti; _cum["output"] += u.get("output_tokens", 0) or 0
                _cum["cache_read"] += _tr; _cum["cache_write"] += _tw
                _emit_budget(args.out, _turn, _ti + _tr + _tw, _cum,
                             ratio=_cal_ratio(_cal, seen_model))
            for block in msg.get("content", []) or []:
                bt = block.get("type")
                if bt == "text":
                    txt = block.get("text", "")
                    if txt.strip():
                        _emit_text(txt)
                elif bt == "tool_use":
                    name = block.get("name", "?")
                    inp = block.get("input", {}) or {}
                    _emit_text(f"  ⏵ {name}({_compact(inp)})")
                    _tool(name)["n"] += 1
                    rt["tool_calls"] += 1
                    tid = block.get("id")
                    if tid:
                        _open_tools[tid] = (name, time.time())
                    if name in FILE_TOOLS:
                        rt["files_modified"] += 1
                    elif name == "Task":
                        rt["delegations"] += 1
                    elif name == "Bash" and "delegate.py" in str(inp.get("command", "")):
                        rt["delegations"] += 1
                    elif name == "Skill":
                        sk = inp.get("skill") or inp.get("command") or "skill"
                        rt["skills"][sk] = rt["skills"].get(sk, 0) + 1
                elif bt == "thinking":
                    pass  # don't spill reasoning into runner.log
            continue

        if etype == "user":
            # Tool results coming back to the model — render compactly so logs stay useful.
            msg = ev.get("message", {}) or {}
            for block in msg.get("content", []) or []:
                if block.get("type") == "tool_result":
                    content = block.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                        )
                    is_err = bool(block.get("is_error"))
                    tid = block.get("tool_use_id")
                    if tid in _open_tools:
                        name, t0 = _open_tools.pop(tid)
                        ms = int((time.time() - t0) * 1000)
                        t = _tool(name)
                        t["ms"] += ms
                        t["max_ms"] = max(t["max_ms"], ms)
                        if is_err:
                            t["fail"] += 1
                    if is_err:
                        rt["tool_failures"] += 1
                    _emit_text(f"  ⏴ {'⚠ ' if is_err else ''}{_compact(content)}")
            continue

        if etype == "result":
            usage = ev.get("usage") or {}
            rec = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "agent": args.agent,
                "reason": args.reason,
                "model": seen_model or args.model or None,
                "input": usage.get("input_tokens", seen_usage["input"]) or 0,
                "output": usage.get("output_tokens", seen_usage["output"]) or 0,
                "cache_read": usage.get("cache_read_input_tokens", seen_usage["cache_read"]) or 0,
                "cache_write": usage.get("cache_creation_input_tokens", seen_usage["cache_write"]) or 0,
                "cost_usd": ev.get("total_cost_usd"),
                "duration_s": round((ev.get("duration_ms") or 0) / 1000.0, 1),
                "turns": ev.get("num_turns"),
                "rc": 1 if ev.get("is_error") else 0,
                "subtype": ev.get("subtype"),
                "runtime": _finalize_rt(rt),
            }
            _append_record(args.out, rec)
            result_written = True
            # Calibration loop (fix #4): fold the authoritative cost into the per-model ratio, and
            # close the budget-calibration ledger with a REAL actual (it used to record the
            # circular "see tick cost_est").
            _update_cal(_state_dir, _cal, seen_model, _raw_est(_cum), ev.get("total_cost_usd"))
            try:
                plan = json.loads((_state_dir / "budget.json").read_text())
                if isinstance(plan, dict) and plan.get("package") and ev.get("total_cost_usd") is not None:
                    with open(_state_dir / "budget-calibration.jsonl", "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"ts": rec["ts"], "package": plan.get("package"),
                                             "est_usd": plan.get("hard_usd"),
                                             "actual_usd": round(ev["total_cost_usd"], 4),
                                             "by": "usage_capture"}) + "\n")
            except Exception:
                pass
            # Signal the stream-json feeder (tick_feeder.py) that this tick produced a result, so it can
            # close stdin (EOF → claude exits cleanly) instead of holding the session open for more input.
            try:
                (pathlib.Path(args.out).parent / ".tick-result").write_text(str(int(time.time())))
            except Exception:
                pass
            _emit_text(
                f"── result: {rec['subtype'] or '?'} · {rec['turns']} turns · {rec['duration_s']}s · "
                f"in={rec['input']} out={rec['output']} cache_r={rec['cache_read']} "
                f"cost={'$%.4f' % rec['cost_usd'] if rec['cost_usd'] is not None else '?'}"
            )
            continue

        # Unknown event type — ignore (forward-compatible with new stream-json events).

    # No result event (crash / timeout-kill / closed pipe): still record the tick,
    # cost unknown, with whatever tokens we saw → dashboard shows "unknown", not a gap.
    if not result_written:
        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "agent": args.agent,
            "reason": args.reason,
            "model": seen_model or args.model or None,
            "input": seen_usage["input"],
            "output": seen_usage["output"],
            "cache_read": seen_usage["cache_read"],
            "cache_write": seen_usage["cache_write"],
            "cost_usd": None,
            "duration_s": round(time.time() - started, 1),
            "turns": None,
            "rc": None,
            "subtype": "no_result",
            "runtime": _finalize_rt(rt),
        }
        _append_record(args.out, rec)


def _finalize_rt(rt):
    """Trim the runtime accumulator to a compact record fragment. Empty sub-maps are dropped so a
    tick with no tools stays small. Returns None when nothing was observed (keeps old-shape parity)."""
    out = {k: rt[k] for k in ("tool_calls", "tool_failures", "files_modified", "delegations",
                              "compactions") if rt.get(k)}
    if rt.get("tools"):
        out["tools"] = rt["tools"]
    if rt.get("skills"):
        out["skills"] = rt["skills"]
    if rt.get("models"):
        out["models"] = rt["models"]
    return out or None


def _append_record(path, rec):
    """Append one JSON line. Best-effort: a write failure must not crash the pipe."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        sys.stderr.write(f"usage_capture: could not append usage record: {e}\n")


if __name__ == "__main__":
    main()
