#!/usr/bin/env python3
"""
test_supervision.py — supervision.py selftest + a REAL run: the watchers must actually beat.

Beyond the unit selftest, this runs spawn_watcher and control_watcher with --once against an empty
queue and asserts their heartbeat files appear and stale() sees them fresh — an invariant over a real
process, which is where the "daemon silently unloaded" class actually lives.
"""
import os, pathlib, subprocess, sys, tempfile, importlib

HERE = pathlib.Path(__file__).resolve().parent
PY = sys.executable or "python3"

# 1) module selftest
r = subprocess.run([PY, str(HERE / "supervision.py"), "--selftest"], capture_output=True, text=True)
sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
if r.returncode != 0:
    sys.exit(1)

fails = []
def ck(n, c):
    if not c: fails.append(n)

with tempfile.TemporaryDirectory() as d:
    reports = pathlib.Path(d) / "reports"
    queue = pathlib.Path(d) / "queue"
    queue.mkdir()
    env = {**os.environ, "ENCLAVE_REPORTS_DIR": str(reports)}

    # run each watcher once against an EMPTY queue → one beat, no spec processing, clean exit
    for script, name in [("spawn_watcher.py", "spawn-watcher"), ("control_watcher.py", "control-watcher")]:
        p = subprocess.run([PY, str(HERE / script), str(queue), "--once"],
                           capture_output=True, text=True, env=env, timeout=60)
        ck(f"{name}-ran", p.returncode == 0)
        ck(f"{name}-heartbeat-written", (reports / f"{name}-heartbeat.json").exists())

    # the checker (same ENCLAVE_REPORTS_DIR) must see both as fresh, and fleet-guardian as MISSING
    supervision = importlib.import_module("supervision")
    os.environ["ENCLAVE_REPORTS_DIR"] = str(reports)
    try:
        bad = dict(supervision.stale(max_age=3600))
    finally:
        os.environ.pop("ENCLAVE_REPORTS_DIR", None)
    ck("spawn-fresh", "spawn-watcher" not in bad)
    ck("control-fresh", "control-watcher" not in bad)
    ck("guardian-missing-flagged", "fleet-guardian" in bad and bad["fleet-guardian"] is None)

if fails:
    print("test_supervision FAIL: " + ", ".join(fails))
    sys.exit(1)
print("test_supervision OK (watchers beat on a real run; checker sees fresh vs missing)")
