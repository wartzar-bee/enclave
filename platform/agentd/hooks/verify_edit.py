#!/usr/bin/env python3
"""
verify_edit.py — PostToolUse "did your edit leave the file broken?" gate (mechanical enforcement).

Wired into the agent's claude session via /agent/.claude/settings.json (PostToolUse on Edit|Write|
MultiEdit). After the agent writes a CODE file, this runs a FAST per-file syntax/parse check and, on a
real failure, exits 2 with the error — Claude Code feeds that stderr back to the agent, so it sees
"your edit broke <file>" and must fix it before moving on. This turns the first line of the
`verify-before-done` skill ("does it even parse/build") from advisory prose into a mechanical gate that
fires on every edit. (The FULL gate — build/tests/behaviour — stays in verify-before-done + the
work.json verify-gate; this is deliberately only the cheap, instant, universal first check.)

Design for UNATTENDED agents (cost + safety):
  • Syntax/parse ONLY — never runs the test suite, a build, or anything slow/networked (a per-edit hook
    must be instant or it stalls every tick and burns budget).
  • One file — only the file just edited.
  • Fails OPEN — unknown extension, missing checker (e.g. no `node`), unreadable input, or any internal
    error → exit 0 silently. It only ever exits 2 on a CONFIRMED syntax error in a file it can check.
    A verifier that false-trips on its own bugs would poison every edit; like guard.py, breakage = quiet.
  • Off-switch: VERIFY_EDIT_OFF=1.

Checks by extension: .py → py_compile · .json → json.load · .js/.mjs/.cjs → `node --check` (if node).
Everything else (.ts/.tsx/.html/.css/.md/.txt/…) is skipped — no cheap universal syntax check, and TS
type-checking is far too heavy for a per-edit hook.
"""
import sys, os, json, subprocess, pathlib

def _exit(code, msg=""):
    if msg:
        sys.stderr.write(msg)
    sys.exit(code)

def main():
    if os.environ.get("VERIFY_EDIT_OFF") == "1":
        sys.exit(0)
    try:
        ev = json.load(sys.stdin)
    except Exception:
        sys.exit(0)                      # unparseable hook input → fail OPEN

    inp = ev.get("tool_input") or {}
    fp = inp.get("file_path") or ""
    if not fp:
        sys.exit(0)
    p = pathlib.Path(fp)
    try:
        if not p.is_file():
            sys.exit(0)                  # deletion / odd path → nothing to check
    except Exception:
        sys.exit(0)

    ext = p.suffix.lower()
    try:
        if ext == ".py":
            r = subprocess.run([sys.executable, "-m", "py_compile", str(p)],
                               capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip().splitlines()
                _exit(2, "verify_edit: your edit left a Python SYNTAX ERROR in "
                         f"{fp} — fix it before continuing:\n" + "\n".join(err[-12:]) + "\n")

        elif ext == ".json":
            try:
                json.loads(p.read_text())
            except Exception as e:
                _exit(2, f"verify_edit: your edit left INVALID JSON in {fp} — fix it before "
                         f"continuing:\n{str(e)[:300]}\n")

        elif ext in (".js", ".mjs", ".cjs"):
            from shutil import which
            node = which("node")
            if not node:
                sys.exit(0)              # no node → can't check → fail OPEN
            r = subprocess.run([node, "--check", str(p)],
                               capture_output=True, text=True, timeout=20)
            if r.returncode != 0:
                err = (r.stderr or r.stdout or "").strip().splitlines()
                _exit(2, "verify_edit: your edit left a JavaScript SYNTAX ERROR in "
                         f"{fp} — fix it before continuing:\n" + "\n".join(err[-12:]) + "\n")
        # any other extension → no cheap check → exit 0
    except subprocess.TimeoutExpired:
        sys.exit(0)                      # checker hung → fail OPEN (never stall a tick)
    except Exception:
        sys.exit(0)                      # any internal error → fail OPEN

    sys.exit(0)

if __name__ == "__main__":
    main()
