#!/usr/bin/env python3
"""event_log.py — the monitoring EVENT SOURCE. Emits one structured JSON line per hook fire to
<agent>/state/events.jsonl (shared filesystem). Registered for PostToolUse + SessionStart + Stop,
so the dashboard can tail events.jsonl LIVE for a real-time activity feed — no git, no 3h snapshot.

Event shape: {"ts": <epoch>, "agent": "<id>", "event": "tool|tick_start|tick_end", ...}
  tool      → {"tool": "Bash", "summary": "git push origin main", "error"?: true}
  tick_start→ {"source": "startup|resume|..."}
  tick_end  → {}

Best-effort + NON-BLOCKING: any failure exits 0 (a monitoring hook must never interfere with a tick).
"""
import sys, json, time, os, pathlib

MAX_LINES = 800   # keep events.jsonl bounded; trimmed at tick end

def summarize(tool, inp):
    inp = inp or {}
    if tool == "Bash":
        # 140 truncated mid-command on almost every real call: the log showed THAT a command ran,
        # never WHAT it did. Evaluation needs the whole command.
        return (inp.get("command", "") or "").strip().replace("\n", " ")[:400]
    if tool in ("Write", "Edit", "NotebookEdit", "Read"):
        return inp.get("file_path", "") or ""
    if tool in ("Glob", "Grep"):
        return (inp.get("pattern", "") or inp.get("query", ""))[:100]
    if tool in ("Task", "Agent"):
        return (inp.get("description", "") or "")[:100]
    if tool in ("WebFetch", "WebSearch"):
        return (inp.get("url", "") or inp.get("query", ""))[:120]
    return ""

def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    base = os.environ.get("AGENT_DIR") or ev.get("cwd") or "/agent"
    sd = pathlib.Path(base) / "state"
    name = ev.get("hook_event_name", "")
    rec = {"ts": int(time.time()), "agent": os.environ.get("AGENT_ID", ""), "event": name}
    if name in ("PreToolUse", "PostToolUse"):
        tool = ev.get("tool_name", "")
        rec.update({"event": "tool", "tool": tool, "summary": summarize(tool, ev.get("tool_input", {}))})
        # Record the RESULT, not just the attempt. This hook was written as the dashboard's live
        # activity feed, so it kept the command and dropped the response — leaving a durable record
        # of actions with no outcomes, which cannot support "evaluate their work and performance".
        tr = ev.get("tool_response")
        err = isinstance(tr, dict) and bool(tr.get("is_error"))
        rec["ok"] = not err
        if err:
            rec["error"] = True
        out = ""
        if isinstance(tr, dict):
            out = tr.get("stdout") or tr.get("output") or tr.get("content") or tr.get("error") or ""
        elif isinstance(tr, str):
            out = tr
        if not isinstance(out, str):
            out = str(out)
        if out:
            rec["result"] = out.strip()[:400]
    elif name == "SessionStart":
        rec.update({"event": "tick_start", "source": ev.get("source", "")})
    elif name == "Stop":
        rec["event"] = "tick_end"
    try:
        sd.mkdir(parents=True, exist_ok=True)
        f = sd / "events.jsonl"
        with f.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        if name == "Stop":                              # trim once per tick, not per tool
            lines = f.read_text().splitlines()
            if len(lines) > MAX_LINES:
                f.write_text("\n".join(lines[-MAX_LINES:]) + "\n")
    except Exception:
        pass
    sys.exit(0)

if __name__ == "__main__":
    main()
