#!/usr/bin/env python3
"""
delegation_guard.py — PreToolUse hook: force the MANAGER to DELEGATE bulk implementation to a
local worker (delegate.py) instead of hand-writing it. This is the mechanical enforcement behind
"Claude manages, local does the labor" — a prompt mandate alone does not stop a capable model from
just doing the work itself (observed: an Opus tick hand-wrote a whole module, 0 delegations).

Wired into the manager's claude session via .claude/settings.json PreToolUse (alongside guard.py).
Reads {tool_name, tool_input} on stdin; exit 2 + stderr reason BLOCKS the call (reason fed back to
the model). Fails OPEN on anything unparseable (never wedge the agent).

Gated by DELEGATION_ENFORCE (default: ON only when BRAIN=claude — a capable manager that should
delegate; OFF for BRAIN=local/api which ARE the worker). It NEVER touches Read/Bash/Grep/Glob/qmd
(so the manager freely plans, reads, and calls delegate.py/route.mjs); it only gates large Write/Edit
of CODE files under the work tree. Small edits, and all writes to plans/state/docs, pass through.

Config (env): DELEGATION_ENFORCE=on|off · DELEGATION_MAX_CHARS=800 · DELEGATION_DENY_GLOBS,
DELEGATION_ALLOW_GLOBS (extra fnmatch patterns). See docs/DELEGATION.md.
"""
import sys, os, re, json, fnmatch, pathlib

# File types that represent BULK LABOR a local worker should author.
CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".mjs", ".jsx", ".svelte", ".vue", ".css", ".scss",
             ".html", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp", ".rb", ".php",
             ".sql", ".sh", ".lua")
# Paths the manager may always write freely — planning / state / memory / docs / config, NOT labor.
ALLOW_PATH_RE = re.compile(
    r"(^|/)(state|docs|memory|knowledge)/"          # planning/state/docs trees
    r"|(^|/)(rollup|objective|board-report|tick-status|approvals|plan)\."   # state files
    r"|/work\.json$|/inbox\.md$"
    r"|\.md$|\.txt$"                                  # prose
    r"|/\.claude/", re.I)


def _enforce_on():
    v = os.environ.get("DELEGATION_ENFORCE", "").strip().lower()
    if v in ("on", "1", "true", "yes"):
        return True
    if v in ("off", "0", "false", "no"):
        return False
    return os.environ.get("BRAIN", "").strip().lower() == "claude"   # default: manager only


def _max_chars():
    try:
        return int(os.environ.get("DELEGATION_MAX_CHARS", "800"))
    except ValueError:
        return 800


def _globs(var):
    return [g.strip() for g in os.environ.get(var, "").split(",") if g.strip()]


def _is_labor_path(path):
    """A code/content file the worker should author — not a plan/state/doc/config path."""
    p = path.lower()
    for g in _globs("DELEGATION_ALLOW_GLOBS"):
        if fnmatch.fnmatch(p, g.lower()):
            return False
    if ALLOW_PATH_RE.search(p):
        return False
    for g in _globs("DELEGATION_DENY_GLOBS"):
        if fnmatch.fnmatch(p, g.lower()):
            return True
    return p.endswith(CODE_EXTS)


def _new_content(tool_name, tool_input):
    """Bytes of NEW content this call introduces."""
    if tool_name == "Write":
        return tool_input.get("content") or ""
    if tool_name == "MultiEdit" or "edits" in tool_input:
        return "".join((e.get("new_string") or "") for e in (tool_input.get("edits") or []))
    return tool_input.get("new_string") or ""           # Edit


def _failed_delegation_for(path):
    """Escape hatch: did a delegation already return verify_failed/incomplete for THIS file recently?
    Lets a genuinely-stuck worker hand back to the manager rather than wedging the tick."""
    try:
        base = os.path.basename(path)
        log = pathlib.Path(os.environ.get("AGENT_DIR", "/agent")) / "state" / "delegations.log"
        for ln in log.read_text().splitlines()[-40:]:
            try:
                e = json.loads(ln)
            except ValueError:
                continue
            if e.get("status") in ("verify_failed", "incomplete") and \
               base and base in (e.get("files") or []):
                return True
    except Exception:
        pass
    return False


def decide(tool_name, tool_input):
    """Pure allow/deny (unit-tested). Returns (allow, reason)."""
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return True, ""
    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not path or not _is_labor_path(path):
        return True, ""
    new = _new_content(tool_name, tool_input)
    if len(new) <= _max_chars():
        return True, ""
    # Escape hatch: explicit fallback tag AND a prior failed delegation for this file.
    if "[delegation-fallback]" in json.dumps(tool_input) and _failed_delegation_for(path):
        return True, ""
    base = os.path.basename(path)
    return False, (
        f"Bulk implementation ({len(new)} chars to {base}) must be DELEGATED to a local worker, not "
        f"hand-written. Run:\n"
        f"  python3 /workspace/platform/agentd/delegate.py --task '<what to build in {base} + acceptance "
        f"criteria>' --kind code --cwd <repo-dir> --allow-files {base} --verify '<cmd that must exit 0>'\n"
        f"(--allow-files bounds the worker's blast radius — anything else it touches is reverted.) "
        f"The worker writes the file; you PLAN, read its JSON summary, and make only small "
        f"(≤{_max_chars()} char) integrating edits. If a delegation already returned verify_failed/"
        f"incomplete for this file this tick, you may write it yourself — include the literal text "
        f"[delegation-fallback] in the call.")


def main():
    if not _enforce_on():
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)                                    # fail-open
    allow, reason = decide(data.get("tool_name", ""), data.get("tool_input", {}) or {})
    if not allow:
        sys.stderr.write(f"[delegation-guard] {reason}\n")
        sys.exit(2)
    sys.exit(0)


def _selftest():
    p = f = 0
    os.environ["DELEGATION_ENFORCE"] = "on"
    big = "x" * 2000
    small = "x" * 100

    def chk(name, got, want_allow, want_sub=""):
        nonlocal p, f
        a, r = got
        if a == want_allow and want_sub in r:
            p += 1
        else:
            f += 1
            print(f"  FAIL [{name}]: allow={a!r} reason={r[:80]!r}")

    chk("big-code-blocked", decide("Write", {"file_path": "/agent/work/repo/eval/models/foo.py", "content": big}), False, "DELEGATED")
    chk("small-code-ok", decide("Write", {"file_path": "/agent/work/repo/eval/models/foo.py", "content": small}), True)
    chk("state-write-ok", decide("Write", {"file_path": "/agent/state/rollup.md", "content": big}), True)
    chk("md-write-ok", decide("Write", {"file_path": "/agent/work/repo/docs/plan.md", "content": big}), True)
    chk("big-edit-blocked", decide("Edit", {"file_path": "/agent/work/repo/src/x.ts", "new_string": big}), False, "DELEGATED")
    chk("read-ok", decide("Read", {"file_path": "/agent/work/repo/eval/models/foo.py"}), True)
    chk("bash-ok", decide("Bash", {"command": "python3 delegate.py --task x"}), True)
    chk("fallback-without-failed-log-still-blocked",
        decide("Write", {"file_path": "/agent/work/repo/eval/models/foo.py", "content": big + " [delegation-fallback]"}), False, "DELEGATED")
    print(f"delegation_guard selftest: {p} passed, {f} failed")
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main()
