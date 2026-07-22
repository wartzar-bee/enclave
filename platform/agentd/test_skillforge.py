#!/usr/bin/env python3
"""Suite wrapper: skillforge (recurring-task -> skill proposal) + the citation verifier.

Both ship their own offline fixtures behind `--selftest`; this puts them in run_tests.sh so a
regression fails the suite instead of waiting to be noticed on a pod.
"""
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
TARGETS = [HERE / "skillforge.py", HERE / "verifiers" / "citation_check.py"]

fails = 0
for t in TARGETS:
    r = subprocess.run([sys.executable, str(t), "--selftest"], capture_output=True, text=True)
    tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or ["(no output)"]
    print(f"{'ok ' if r.returncode == 0 else 'FAIL'} {t.name}: {tail[0]}")
    fails += (r.returncode != 0)

print(f"\n{len(TARGETS) - fails} passed, {fails} failed")
sys.exit(1 if fails else 0)
