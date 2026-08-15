#!/usr/bin/env python3
"""test_statefile.py — statefile.py selftest + cross-reader agreement on work.json shapes."""
import importlib, pathlib, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 1) the module's own selftest (atomic write + shape normalisation + torn-file tolerance)
r = subprocess.run([sys.executable or "python3", str(HERE / "statefile.py"), "--selftest"],
                   capture_output=True, text=True)
sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
if r.returncode != 0:
    sys.exit(1)

# 2) the three readers must AGREE on every shape (the bug was that they didn't)
statefile = importlib.import_module("statefile")
tick_feeder = importlib.import_module("tick_feeder")
memory = importlib.import_module("memory")

fails = []
def ck(n, c):
    if not c: fails.append(n)

with tempfile.TemporaryDirectory() as d:
    base = pathlib.Path(d)
    wf = base / "work.json"

    def agentloop_view():
        # mirror AgentLoop._has_open_work without constructing the whole loop
        return statefile.has_open_work(wf)

    for name, payload, expect_open in [
        ("bare-list-open", [{"id": 1, "status": "todo"}], True),
        ("bare-list-closed", [{"id": 1, "status": "done"}], False),
        ("dict-items-open", {"updated": "x", "items": [{"id": 1, "status": "todo"}]}, True),
        ("dict-tasks-open", {"tasks": [{"id": 2, "status": "doing"}]}, True),
        ("dict-items-closed", {"items": [{"id": 1, "status": "done"}]}, False),
    ]:
        statefile.write_json(wf, payload)
        a = agentloop_view()
        t = tick_feeder._has_open_work(str(base))
        m = bool([i for i in memory.Memory(str(base)).work_list() if i.get("status") not in ("done", "dropped")])
        ck(f"{name}:agentloop", a is expect_open)
        ck(f"{name}:tickfeeder", t is expect_open)
        ck(f"{name}:memory", m is expect_open)
        ck(f"{name}:all-agree", a == t == m)

if fails:
    print("test_statefile FAIL: " + ", ".join(fails))
    sys.exit(1)
print("test_statefile OK (atomic writes; agentloop/tick_feeder/memory agree on every work.json shape)")
