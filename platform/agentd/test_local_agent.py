#!/usr/bin/env python3
"""Unit tests for local_agent.parse_tool_call — the ReAct tool-call parser.

Focus: BOTH multi-line AND single-line fenced forms must parse. Local models (qwen, etc.) frequently
emit single-line fences (```bash cat foo```), which the original \\n-anchored regexes rejected → wasted
"no tool call" steps. Pure function, no network. Run: python3 test_local_agent.py
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location("local_agent", pathlib.Path(__file__).with_name("local_agent.py"))
la = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(la)

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok  {name}")
    else:
        failed += 1
        print(f"XX  {name}")


def call(text):
    return la.parse_tool_call(text)


# ── single-line forms (the regression these fix) ──────────────────────────────────────────────
c = call("```bash cat /agent/memory/INDEX.md ```")
check("single-line bash → bash tool", c and c["tool"] == "bash")
check("single-line bash → command captured", c and c["input"]["command"].strip() == "cat /agent/memory/INDEX.md")

c = call('```bash find /a -name "*.md" | grep -i slot ```')
check("single-line bash with pipe+quotes", c and c["tool"] == "bash" and "grep -i slot" in c["input"]["command"])

c = call('```write /agent/work/work.json {"queue": []} ```')
check("single-line write → write tool", c and c["tool"] == "write" and c["input"]["file_path"] == "/agent/work/work.json")
check("single-line write → content captured", c and '"queue": []' in c["input"]["content"])

# ── multi-line forms (must still work) ─────────────────────────────────────────────────────────
c = call("```bash\ncat foo\n```")
check("multi-line bash", c and c["tool"] == "bash" and c["input"]["command"] == "cat foo")

c = call("```write /p/x.txt\nhello\nworld\n```")
check("multi-line write preserves body", c and c["tool"] == "write" and c["input"]["content"] == "hello\nworld")

# ── JSON ```tool form (must still work) ─────────────────────────────────────────────────────────
c = call('```tool {"tool":"read","input":{"file_path":"/a"}}```')
check("json tool read", c and c["tool"] == "read" and c["input"]["file_path"] == "/a")

c = call('{"tool":"finish","input":{"summary":"done"}}')
check("bare json finish", c and c["tool"] == "finish")

# ── first-call-wins + no-call ───────────────────────────────────────────────────────────────────
c = call("```bash echo one```\nsome prose\n```bash echo two```")
check("first call in document order wins", c and c["input"]["command"].strip() == "echo one")

check("no fenced/json call → None", call("just thinking out loud, no action") is None)

print(f"\n{passed}/{passed + failed} passed" + ("" if not failed else f"  ({failed} FAILED)"))
raise SystemExit(1 if failed else 0)
