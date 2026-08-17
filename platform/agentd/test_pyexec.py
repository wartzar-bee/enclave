#!/usr/bin/env python3
"""test_pyexec.py — offline suite for the code-over-object tool (no network, stubbed brain).

Covers the load-bearing parts: the loader's format sniffing, the contract's key-coverage line
(the anti-KeyError device), cell execution incl. the error-feedback round trip, the FINAL
protocol, and the budget backstop. The brain is a scripted stub — these tests must stay green
on the baked image with no endpoint."""
import json, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pyexec

FAILS = 0


def check(name, cond):
    global FAILS
    print(("ok:" if cond else "FAIL:"), name)
    if not cond:
        FAILS += 1


tmp = pathlib.Path(tempfile.mkdtemp())

# ── loader: JSONL / JSON / CSV / text ──────────────────────────────────────────────────────
jl = tmp / "events.jsonl"
jl.write_text('{"tool": "Bash", "ok": true}\n{"tool": "Edit"}\n{"tool": "Bash", "ok": false}\n')
data, kind = pyexec.load_data(jl)
check("loader: JSONL → list of dicts", isinstance(data, list) and len(data) == 3 and "JSONL" in kind)

jf = tmp / "obj.json"
jf.write_text('{"a": {"b": 1}, "c": [1, 2, 3]}')
data2, kind2 = pyexec.load_data(jf)
check("loader: JSON → parsed value", isinstance(data2, dict) and data2["c"] == [1, 2, 3])

cf = tmp / "rows.csv"
cf.write_text("name,age\nsam,3\nkim,4\n")
data3, kind3 = pyexec.load_data(cf)
check("loader: CSV → list of dicts", isinstance(data3, list) and data3[0]["name"] == "sam")

txt = tmp / "notes.log"
txt.write_text("just some\nplain lines\n")
data4, kind4 = pyexec.load_data(txt)
check("loader: junk → raw text", isinstance(data4, str) and kind4 == "text")

# ── contract: key coverage names partial keys (the anti-KeyError device) ───────────────────
c = pyexec.describe(data, kind)
check("contract: counts records", "len(data) = 3" in c)
check("contract: flags partially-present key", "'ok': 2/3" in c)
check("contract: full-coverage key shown as full", "'tool': 3/3" in c)
check("contract: bounded (no data dump)", len(c) < 2000)

# ── cell runner: preloaded data, output capture, failure flag, cap ─────────────────────────
out, failed = pyexec.run_cell("print(sum(1 for d in data if not d.get('ok', True)))", jl)
check("cell: computes over preloaded data", out.strip() == "1" and not failed)
out2, failed2 = pyexec.run_cell("print(data[99]['nope'])", jl)
check("cell: failure flagged with traceback fed back", failed2 and "IndexError" in out2)
out3, failed3 = pyexec.run_cell("print('x' * 100000)", jl)
check("cell: stdout capped", len(out3) <= pyexec.STDOUT_CAP)

# ── protocol extraction ────────────────────────────────────────────────────────────────────
check("extract: code fence", pyexec._extract("```python\nprint(1)\n```")[0] == "code")
check("extract: FINAL wins when first", pyexec._extract("FINAL: 42\n```python\nx\n```")[0] == "final")
check("extract: code wins when first", pyexec._extract("```python\nx\n```\nFINAL: no")[0] == "code")
check("extract: prose → neither", pyexec._extract("the answer is 3")[0] == "neither")

# ── loop: scripted brain — probe cell, error cell, then FINAL ──────────────────────────────
SCRIPT = [
    "```python\nprint(data[99]['nope'])\n```",                      # errors → fed back
    "```python\nprint(sum(1 for d in data if not d.get('ok', True)))\n```",  # ok → '1'
    "FINAL: exactly 1 failure",
]
calls = {"n": 0, "saw_error": False, "saw_output": False}


def fake_chat(endpoint, messages, max_tokens, temperature, timeout, tools=None):
    for m in messages:
        if m["role"] == "user" and "status=error" in m.get("content", ""):
            calls["saw_error"] = True
        if m["role"] == "user" and "status=ok" in m.get("content", ""):
            calls["saw_output"] = True
    r = SCRIPT[min(calls["n"], len(SCRIPT) - 1)]
    calls["n"] += 1
    return r


pyexec._HAVE_BRAIN = True
pyexec.chat = fake_chat
pyexec.resolve_endpoints = lambda: ({"base": "stub", "model": "stub", "key": ""}, {})
ans = pyexec.run_pyexec("how many failures?", str(jl))
check("loop: reaches FINAL through an error round-trip", ans == "exactly 1 failure")
check("loop: traceback was fed back to the model", calls["saw_error"])
check("loop: good output was fed back to the model", calls["saw_output"])
check("loop: three brain calls total", calls["n"] == 3)

# ── loop: budget backstop returns last real output, labelled ───────────────────────────────
calls["n"] = 0
SCRIPT[:] = ["```python\nprint(len(data))\n```"] * 10   # never finishes
ans2 = pyexec.run_pyexec("count", str(jl), max_cells=2)
check("loop: budget exhaustion is labelled and carries last output",
      "budget" in ans2 and "3" in ans2)

# ── loop: SHORT prose reply is returned, not looped on ─────────────────────────────────────
calls["n"] = 0
SCRIPT[:] = ["There are three records."]
ans3 = pyexec.run_pyexec("count", str(jl))
check("loop: short prose returned as answer", ans3 == "There are three records.")

# ── loop: DEGENERATE long fence-less output is nudged back onto the protocol, not returned ─
calls["n"] = 0
SCRIPT[:] = ["import collections, json, sys " * 60,           # the observed 20b failure mode
             "FINAL: recovered after nudge"]
ans4 = pyexec.run_pyexec("count", str(jl))
check("loop: degenerate output nudged, then recovers", ans4 == "recovered after nudge")

print("ALL PASS" if FAILS == 0 else f"{FAILS} FAILURES")
sys.exit(1 if FAILS else 0)
