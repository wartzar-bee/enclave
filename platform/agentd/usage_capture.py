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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.environ.get("AGENT_ID", "unknown"))
    ap.add_argument("--reason", default=os.environ.get("TICK_REASON", "heartbeat"))
    ap.add_argument("--model", default=os.environ.get("MODEL_EFF", ""))
    ap.add_argument("--out", required=True, help="path to state/usage.jsonl")
    args = ap.parse_args()

    # Accumulate what we observe so a missing result event still yields a record.
    seen_usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
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
