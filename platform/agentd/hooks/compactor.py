#!/usr/bin/env python3
"""compactor.py — PreToolUse context-guard (within-tick bloat control).

The #1 live cost in a persistent fleet is `cache_read` accumulation: every turn re-reads the whole
cached window, and that scales with turns × window-size (see docs/CONTEXT-COMPACTOR.md). A handful of
tool calls dump a whole file / un-piped `find`/`grep -r` into the window, where it's then re-sent every
following turn. This hook GATES those context-bombing calls and STEERS the agent to the cheap form
(pipe to a file + grep; Read with offset/limit; one batched script) — enforcing the discipline the
tick prompt only *requests*.

Modes (per agent, via env):
  COMPACT_ENFORCE unset/0  → REPORT-ONLY: log what it WOULD gate to state/compact.log, always allow.
  COMPACT_ENFORCE=1        → ENFORCE: exit 2 with a steering message (the agent sees it, retries lean).
Thresholds: COMPACT_MAX_READ_BYTES (default 65536). Fail-OPEN: any error → allow (never wedge a tick).

PreToolUse protocol (same as build_guard/delegation_guard): stdin = JSON {tool_name, tool_input};
exit 0 = allow, exit 2 + stderr = block.
"""
import os, sys, json, re, time, pathlib

MAX_READ_BYTES = int(os.environ.get("COMPACT_MAX_READ_BYTES", "65536"))
ENFORCE = os.environ.get("COMPACT_ENFORCE", "").strip() in ("1", "true", "on", "yes")

# --- Bash patterns that dump unbounded output into context -----------------------------------------
# A whole-file dump to stdout: cat/bat/less/more/xxd/od a path, when NOT piped or redirected.
DUMP = re.compile(r"\b(cat|bat|less|more|xxd|od|hexdump)\s+[^|>]*$", re.I)
# A directory/recursive spew that can flood: find / grep -r / rg / ls -R / tree / du -a.
SPEW = re.compile(r"\b(find\s|grep\s+-[a-z]*[rR]|rg\s|ls\s+-[a-z]*R|tree\b|du\s+-a)", re.I)
# Bounded enough to be fine — presence of any of these on the line clears a SPEW/DUMP flag.
BOUNDED = re.compile(r"(\|\s*(head|tail|wc|sort\s+-u|uniq|grep\s+-c|jq)\b|>\s*\S|>>\s*\S|"
                     r"-maxdepth\s+[0-3]\b|--files-with-matches|grep\s+-[a-z]*l\b|grep\s+-[a-z]*c\b|-print0)", re.I)


def _agent_dir():
    d = os.environ.get("AGENT_DIR")
    if d and pathlib.Path(d, "state").is_dir():
        return pathlib.Path(d)
    # walk up from this hook (…/.claude/hooks/compactor.py → agent home)
    for p in pathlib.Path(__file__).resolve().parents:
        if (p / "state").is_dir() and (p / ".claude").is_dir():
            return p
    return pathlib.Path("/agent")


def _log(reason, tool, detail):
    try:
        p = _agent_dir() / "state" / "compact.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "mode": "enforce" if ENFORCE else "report",
               "tool": tool, "reason": reason, "detail": detail[:300]}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # logging must never break a tick


def _gate(reason, tool, detail, steer):
    """Report-only: log + allow. Enforce: log + block (exit 2) with the steering message."""
    _log(reason, tool, detail)
    if ENFORCE:
        sys.stderr.write(f"[compactor] {reason} — {steer}\n")
        sys.exit(2)
    sys.exit(0)


def _check_bash(cmd):
    if not cmd:
        return
    # check each &&/;/| segment's leading command, but evaluate boundedness on the WHOLE line
    bounded = bool(BOUNDED.search(cmd))
    if SPEW.search(cmd) and not bounded:
        _gate("un-piped recursive scan floods context", "Bash", cmd,
              "bound it: add `| head -50` or `| wc -l`, `-maxdepth`, or `grep -l`; "
              "or write the full output to a file and grep only the lines you need")
    if DUMP.search(cmd) and not bounded:
        _gate("whole-file dump to stdout floods context", "Bash", cmd,
              "don't `cat` a file into context — Read it with offset/limit, or `grep`/`sed -n` "
              "the specific lines, or pipe to a file and grep")


def _check_read(inp):
    fp = inp.get("file_path") or ""
    if not fp or inp.get("limit"):  # an explicit limit is already the disciplined form
        return
    try:
        size = os.path.getsize(fp)
    except OSError:
        return  # missing/unstattable → not our concern
    if size > MAX_READ_BYTES:
        _gate(f"Read of a large file ({size//1024} KB) with no limit", "Read", fp,
              "Read with offset/limit for just the section you need, or `grep`/`codegraph` to "
              "locate the lines first — a full large Read sits in context every following turn")


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail-open
    try:
        tool = ev.get("tool_name", "")
        inp = ev.get("tool_input", {}) or {}
        if tool == "Bash":
            _check_bash(inp.get("command", "") or "")
        elif tool == "Read":
            _check_read(inp)
    except SystemExit:
        raise
    except Exception:
        pass  # fail-open on any unexpected shape
    sys.exit(0)


if __name__ == "__main__":
    main()
