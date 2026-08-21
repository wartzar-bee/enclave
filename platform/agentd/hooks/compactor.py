#!/usr/bin/env python3
"""compactor.py — PreToolUse context-guard (within-tick bloat control).

The #1 live cost in a persistent fleet is `cache_read` accumulation: every turn re-reads the whole
cached window, and that scales with turns × window-size (see docs/CONTEXT-COMPACTOR.md). A handful of
tool calls dump a whole file / un-piped `find`/`grep -r` into the window, where it's then re-sent every
following turn. This hook GATES those context-bombing calls and STEERS the agent to the cheap form
(pipe to a file + grep; Read with offset/limit; one batched script) — enforcing the discipline the
tick prompt only *requests*.

Modes (per agent, via COMPACT_MODE; COMPACT_ENFORCE=1 is the legacy spelling of `enforce`):
  report  (default) → log what it WOULD gate to state/compact.log, always allow.
  enforce           → exit 2 with a steering message (the agent sees it, retries lean).
  spill             → Tier 2 (docs/CONTEXT-COMPACTOR.md §A.2): don't refuse, RESHAPE. The call runs
                      once, its full output lands in state/.compact/<id>.txt, and the model gets a
                      bounded preview + the locator. Mechanism: PreToolUse `updatedInput`.

Why spill beats enforce: a refusal costs a whole turn AND depends on the agent complying; a rewrite
costs nothing and cannot be ignored. (Adopted from DeepSeek Harness `spill-policy`, which places the
same idea post-execute — impossible here: PostToolUse cannot modify a tool result. See
ENCLAVE-DEEPSEEK-HARNESS-EVAL-2026-08-21.md.) Per §A.4 the full output is NEVER lost, only kept out
of the window, and the elision marker says how much was cut and where the rest lives.

Thresholds: COMPACT_MAX_READ_BYTES (default 65536), COMPACT_PREVIEW_BYTES (4096),
COMPACT_READ_LIMIT (400 lines). Fail-OPEN: any error → allow (never wedge a tick).

PreToolUse protocol (same as build_guard/delegation_guard): stdin = JSON {tool_name, tool_input};
exit 0 = allow, exit 2 + stderr = block, or exit 0 + a `hookSpecificOutput.updatedInput` JSON body
to run a rewritten call.
"""
import os, sys, json, re, time, pathlib, hashlib, shlex

MAX_READ_BYTES = int(os.environ.get("COMPACT_MAX_READ_BYTES", "65536"))
PREVIEW_BYTES = int(os.environ.get("COMPACT_PREVIEW_BYTES", "4096"))
READ_LIMIT = int(os.environ.get("COMPACT_READ_LIMIT", "400"))


def _mode():
    """report | enforce | spill. COMPACT_MODE wins; COMPACT_ENFORCE=1 is the legacy spelling."""
    m = os.environ.get("COMPACT_MODE", "").strip().lower()
    if m in ("report", "enforce", "spill"):
        return m
    if os.environ.get("COMPACT_ENFORCE", "").strip() in ("1", "true", "on", "yes"):
        return "enforce"
    return "report"


MODE = _mode()
ENFORCE = MODE == "enforce"  # kept: console.py and the docs still speak of enforce

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
               "mode": MODE,
               "tool": tool, "reason": reason, "detail": detail[:300]}
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # logging must never break a tick


def _spill_path(detail):
    """A fresh, collision-proof spill file under state/.compact/ (docs/CONTEXT-COMPACTOR.md §A.3)."""
    d = _agent_dir() / "state" / ".compact"
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    tag = hashlib.sha1(f"{detail}{os.getpid()}{time.time()}".encode()).hexdigest()[:8]
    return d / f"{stamp}-{tag}.txt"


def _bash_spill_input(inp, cmd, reason):
    """Rewrite a context-bombing Bash command so it runs ONCE, spills in full, and returns a preview.

    Newline-separated (never `{ cmd ; }`) so a trailing `#comment` in the agent's command cannot
    swallow the closing brace. `(exit $__rc)` re-raises the original status without exiting the
    tool's shell. Returns None when the command cannot be safely wrapped."""
    stripped = cmd.strip()
    if not stripped or stripped.endswith("&"):
        return None  # backgrounded: redirecting it would change its semantics
    sp = _spill_path(cmd)
    q = shlex.quote(str(sp))
    new_cmd = (
        "{\n" + stripped + "\n} > " + q + " 2>&1\n"
        "__rc=$?\n"
        "__sz=$(wc -c < " + q + " | tr -d ' ')\n"
        "head -c " + str(PREVIEW_BYTES) + " " + q + "\n"
        "echo\n"
        'echo "[compactor] ' + reason + ": full output ($__sz bytes) is in " + str(sp) + "; the "
        "preview above is only its first " + str(PREVIEW_BYTES) + " bytes. Nothing was lost — "
        "grep/sed -n/pyexec.py THAT FILE for the rest, do not cat it.\"\n"
        "(exit $__rc)"
    )
    out = dict(inp)
    out["command"] = new_cmd
    return out


def _read_spill_input(inp, size):
    """A large no-limit Read becomes a bounded Read. The file itself is already the locator."""
    out = dict(inp)
    out["limit"] = READ_LIMIT
    out.setdefault("offset", 1)
    return out


def _allow_with(updated, note):
    """PreToolUse: run the call with rewritten input. `updatedInput` applies without a
    permissionDecision, but we state `allow` so a later ask-rule cannot re-prompt on our rewrite."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": note,
        "updatedInput": updated,
    }}))
    sys.exit(0)


def _gate(reason, tool, detail, steer, rewrite=None):
    """report: log + allow. enforce: log + block (exit 2) + steer. spill: log + run the REWRITTEN
    call. A spill-mode call with no safe rewrite falls back to enforce — spill is stricter than
    report, never looser."""
    _log(reason, tool, detail)
    if MODE == "spill":
        updated = rewrite() if rewrite else None
        if updated is not None:
            _allow_with(updated, f"[compactor] {reason} — spilled to file, preview returned")
        sys.stderr.write(f"[compactor] {reason} — {steer}\n")
        sys.exit(2)
    if ENFORCE:
        sys.stderr.write(f"[compactor] {reason} — {steer}\n")
        sys.exit(2)
    sys.exit(0)


def _check_bash(inp, cmd):
    if not cmd:
        return
    # check each &&/;/| segment's leading command, but evaluate boundedness on the WHOLE line
    bounded = bool(BOUNDED.search(cmd))
    if SPEW.search(cmd) and not bounded:
        reason = "un-piped recursive scan floods context"
        _gate(reason, "Bash", cmd,
              "bound it: add `| head -50` or `| wc -l`, `-maxdepth`, or `grep -l`; "
              "or write the full output to a file and grep only the lines you need",
              rewrite=lambda: _bash_spill_input(inp, cmd, reason))
    if DUMP.search(cmd) and not bounded:
        reason = "whole-file dump to stdout floods context"
        _gate(reason, "Bash", cmd,
              "don't `cat` a file into context — Read it with offset/limit, or `grep`/`sed -n` "
              "the specific lines, or pipe to a file and grep",
              rewrite=lambda: _bash_spill_input(inp, cmd, reason))


# Files whose CONTEXT cost is not their BYTE cost. A 600 KB PNG is a vision read worth ~1-2k
# tokens, not 600 KB of tokens — gating it on file size is measuring the wrong thing, and
# `limit`/`offset` are meaningless on it. Measured 2026-08-21: 2,230 of stoneforge's 2,241 Read
# gates (99.5%) were .png/.jpg, and during the one window where enforce was on (2026-06-26..28)
# this hook BLOCKED 172 image reads — i.e. it stopped an art agent from looking at its own QA
# renders. See docs/CONTEXT-COMPACTOR.md §A.6.
VISUAL_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif", ".pdf"}


def _check_read(inp):
    fp = inp.get("file_path") or ""
    if not fp or inp.get("limit"):  # an explicit limit is already the disciplined form
        return
    if os.path.splitext(fp)[1].lower() in VISUAL_EXT:
        return  # never gate or reshape a visual read (see VISUAL_EXT)
    try:
        size = os.path.getsize(fp)
    except OSError:
        return  # missing/unstattable → not our concern
    if size > MAX_READ_BYTES:
        _gate(f"Read of a large file ({size//1024} KB) with no limit", "Read", fp,
              "Read with offset/limit for just the section you need, or `grep`/`codegraph` to "
              "locate the lines first — a full large Read sits in context every following turn",
              rewrite=lambda: _read_spill_input(inp, size))


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail-open
    try:
        tool = ev.get("tool_name", "")
        inp = ev.get("tool_input", {}) or {}
        if tool == "Bash":
            _check_bash(inp, inp.get("command", "") or "")
        elif tool == "Read":
            _check_read(inp)
    except SystemExit:
        raise
    except Exception:
        pass  # fail-open on any unexpected shape
    sys.exit(0)


if __name__ == "__main__":
    main()
