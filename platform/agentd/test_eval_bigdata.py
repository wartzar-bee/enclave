#!/usr/bin/env python3
"""test_eval_bigdata.py — offline suite for the bigdata harness-comparison adapter.

No network: fixture synthesis determinism, gold-by-construction, grading, task fan-out, the
local --data path, and the runner's harness subprocess branch against a dead endpoint (must
fail FAST and GRADED, never hang or crash the run)."""
import json, os, pathlib, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "eval"))
sys.path.insert(0, str(HERE))
from adapters import BigData
import runner

FAILS = 0


def check(name, cond):
    global FAILS
    print(("ok:" if cond else "FAIL:"), name)
    if not cond:
        FAILS += 1


tmp = pathlib.Path(tempfile.mkdtemp())
os.environ["AGENT_DIR"] = str(tmp)
ad = BigData()

# ── fixture: deterministic, gold consistent by construction ────────────────────────────────
p1, g1 = ad._fixture({"seed": 7, "size": 200})
p2, g2 = ad._fixture({"seed": 7, "size": 200})
check("fixture: same seed+size → same path and gold", p1 == p2 and g1 == g2)
b1 = pathlib.Path(p1).read_text()
check("fixture: deterministic bytes", b1 == pathlib.Path(p2).read_text())
check("fixture: gold total matches file", g1["total"] == 200 == len(b1.strip().splitlines()))
check("fixture: calls sum to total", sum(g1["calls"].values()) == 200)
check("fixture: fails consistent", g1["total_fails"] == sum(g1["fails"].values()) > 0)
recount = {}
for ln in b1.splitlines():
    d = json.loads(ln)
    if d["ok"] is False:
        recount[d["tool"]] = recount.get(d["tool"], 0) + 1
check("fixture: gold fails match a recount", recount == g1["fails"])
p3, g3 = ad._fixture({"seed": 8, "size": 200})
check("fixture: different seed → different gold", g3 != g1)

# ── --data path: gold computed from a local real-format log ────────────────────────────────
real = tmp / "real.jsonl"
real.write_text('{"event":"tool","tool":"Bash","ok":true}\n'
                '{"event":"tool","tool":"Bash","ok":false}\n'
                '{"event":"tick_end"}\n'
                '{"event":"tool","tool":"Edit"}\n')
pr, gr = ad._gold_from(str(real))
check("data: counts only tool events", gr["total"] == 3)
check("data: ok-missing is not a failure", gr["fails"] == {"Bash": 1} and gr["total_fails"] == 1)

# ── tasks: fan-out and rep semantics ───────────────────────────────────────────────────────
t_default = ad.tasks({"seed": 7, "size": 200})
check("tasks: default = pyexec x3", len(t_default) == 3 and all(x["harness"] == "pyexec" for x in t_default))
t_both = ad.tasks({"seed": 7, "size": 200, "harness": "both", "n": 2})
check("tasks: both x n=2 → 4 tasks", len(t_both) == 4 and
      {x["harness"] for x in t_both} == {"pyexec", "rlm"})
t_cli_default_n = ad.tasks({"seed": 7, "size": 200, "n": 50})
check("tasks: cli default n=50 means 3 reps, not 50", len(t_cli_default_n) == 3)

# ── grading ────────────────────────────────────────────────────────────────────────────────
task = t_default[0]
g = task["gold"]
good = (f"total {g['total']}, failures {g['total_fails']}: " +
        ", ".join(f"{t}: {v}" for t, v in g["fails"].items()))
check("grade: all gold numbers present → ok", ad.grade(task, good)[0])
ok, detail = ad.grade(task, f"total {g['total']}, no failures at all")
check("grade: missing numbers → not ok, named", not ok and "missing:" in detail)

# ── runner harness branch: dead endpoint fails fast and graded, never hangs ────────────────
ep = {"base": "http://127.0.0.1:9/v1", "key": "", "label": "dead"}
r = runner.run_harness(ep, "stub-model", task, timeout=60)
check("run_harness: returns without hanging", isinstance(r, dict))
check("run_harness: harness-level failure is text, not a crash",
      r["error"] is None and "endpoint error" in r["text"])
check("run_harness: no tokens counted on a dead endpoint", not r["tokens"] and r["calls"] == 0)
ok2, _ = ad.grade(task, r["text"])
check("run_harness: dead-endpoint output grades as wrong", not ok2)

# ── summarize: per-harness breakdown ───────────────────────────────────────────────────────
rows = [{"task": "pyexec-count-r1", "ok": True, "secs": 5.0, "tokens": 2000, "calls": 2, "error": None},
        {"task": "rlm-count-r1", "ok": False, "secs": 900.0, "tokens": 600000, "calls": 129, "error": None}]
s = ad.summarize(rows)
check("summarize: splits by harness", set(s["harnesses"]) == {"pyexec", "rlm"})
check("summarize: carries tokens+calls", s["harnesses"]["rlm"]["avg_tokens"] == 600000
      and s["harnesses"]["pyexec"]["avg_calls"] == 2)

print("ALL PASS" if FAILS == 0 else f"{FAILS} FAILURES")
sys.exit(1 if FAILS else 0)
