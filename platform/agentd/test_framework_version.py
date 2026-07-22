#!/usr/bin/env python3
"""Suite wrapper for framework_version's selftest, plus the wiring assertions.

The selftest lives in the module (so it ships and can be run inside a pod); this file exists so the
framework suite actually runs it, and so the three long-lived processes cannot quietly lose their
staleness check — that check is the only thing making "committed" and "live" the same thing.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import framework_version   # noqa: E402

failed = 0


def check(name, cond):
    global failed
    if not cond:
        failed += 1
    print(f"{'ok' if cond else 'FAIL'}: {name}")


r = subprocess.run([sys.executable, str(HERE / "framework_version.py"), "--selftest"],
                   capture_output=True, text=True)
check("framework_version selftest passes", r.returncode == 0)
if r.returncode != 0:
    print(r.stdout[-2000:], r.stderr[-2000:])

# Each long-lived process must CALL restart_if_stale, not merely import the module. On 2026-07-22 the
# agent loop ran 4h29m of stale code, the monitor a whole session on a stale runbook, and the console
# 22h59m — the last of which rendered a working fleet as idle-or-down to the operator.
for mod, why in [("agentloop.py", "the agent loop (between ticks — the only safe point)"),
                 ("fleet_monitor.py", "the monitor daemon (between cycles)"),
                 ("console.py", "the console snapshot thread (a stale dashboard lies)")]:
    src = (HERE / mod).read_text()
    check(f"{mod} imports framework_version", "framework_version" in src)
    check(f"{mod} calls restart_if_stale — {why}", "restart_if_stale" in src)

# A test edit must never bounce the fleet: developers run the suite constantly.
names = {p.name for p in framework_version.source_files(HERE)}
check("the suite's own files are excluded from the fingerprint",
      not any(n.startswith(("test_", "tests_")) for n in names))
check("real framework modules ARE in the fingerprint",
      {"agentloop.py", "fleet_monitor.py", "console.py", "scorecard.py"} <= names)

print()
if failed:
    print(f"{failed} FAILED")
    raise SystemExit(1)
print("ALL PASS")
