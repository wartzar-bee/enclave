#!/usr/bin/env python3
"""test_nooa_worker.py — offline checks for the optional NOOA worker CLI.

NOOA itself is NOT installed in the lean image or on dev hosts — that's the point of the
build-arg — so this suite covers exactly the paths that must work WITHOUT it: the routing-
doctrine refusal (which must fire BEFORE any import of nooa), the refuse-to-guess model
resolution, and the clean exit-2 fallback message when nooa is absent."""
import pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
FAILS = 0


def check(name, cond):
    global FAILS
    print(("ok:" if cond else "FAIL:"), name)
    if not cond:
        FAILS += 1


def run(*args, env=None):
    import os
    e = {**os.environ, **(env or {})}
    e.pop("NOOA_WORKER_MODEL", None)
    e.update(env or {})
    return subprocess.run([sys.executable, str(HERE / "nooa_worker.py"), *args],
                         capture_output=True, text=True, timeout=60, env=e)


tmp = pathlib.Path(tempfile.mkdtemp())
f = tmp / "d.jsonl"
f.write_text('{"event":"tool","tool":"Bash","ok":true}\n{"event":"tool","tool":"Edit"}\n')

# doctrine: claude/anthropic ids refused, before nooa is ever imported
r = run("--query", "x", "--file", str(f), "--model", "anthropic/claude-opus-4-8")
check("doctrine: anthropic/* refused", r.returncode != 0 and "REFUSED" in r.stderr)
r = run("--query", "x", "--file", str(f), "--model", "claude-sonnet-4-6")
check("doctrine: bare claude-* refused", r.returncode != 0 and "REFUSED" in r.stderr)
r = run("--query", "x", "--file", str(f), "--model", "openai/gpt-oss-20b",
        "--base", "https://api.anthropic.com/v1")
check("doctrine: anthropic base refused", r.returncode != 0 and "REFUSED" in r.stderr)

# model resolution: refuses to guess, names the remedies
r = run("--query", "x", "--file", str(f), env={"DELEGATE_POLICY": str(tmp / "nope.json")})
check("model: no source → refuse to guess, names remedies",
      r.returncode != 0 and "NOOA_WORKER_MODEL" in r.stderr and "Refusing to guess" in r.stderr)

# env model + absent nooa → clean exit 2 with the pyexec fallback hint (dev hosts have no nooa)
r = run("--query", "x", "--file", str(f), "--model", "openai/gpt-oss-20b")
if "not installed" in r.stderr:
    check("absent nooa: exit 2 + fallback hint", r.returncode == 2 and "pyexec" in r.stderr)
else:
    print("ok: absent-nooa check skipped (nooa IS installed here)")

print("ALL PASS" if FAILS == 0 else f"{FAILS} FAILURES")
sys.exit(1 if FAILS else 0)
