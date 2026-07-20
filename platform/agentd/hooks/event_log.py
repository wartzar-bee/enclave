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
import sys, json, time, os, pathlib, re

# Redaction. Capturing tool OUTPUT means a command that prints a credential would write it into
# events.jsonl, which downstream tooling renders into git-tracked reports. Scrub at the source so
# the secret is never on disk in the first place: a redactor that only runs at render time leaves
# the raw value sitting in the log file. CLAUDE.md: never log a credential.
_REDACT = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}"
    r"|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|gho_[A-Za-z0-9]{16,}"
    r"|nvapi-[A-Za-z0-9_-]{16,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----"
    r"|\b[A-Za-z0-9._%+-]+:[^\s@/]{6,}@)", re.I)
_REDACT_KV = re.compile(
    r"\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(\"?[^\s\"']{6,}\"?)", re.I)


def _redact(text):
    if not text:
        return text
    text = _REDACT.sub("[REDACTED]", text)
    return _REDACT_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


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
        rec.update({"event": "tool", "tool": tool, "summary": _redact(summarize(tool, ev.get("tool_input", {})))})
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
            rec["result"] = _redact(out.strip()[:400])
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
