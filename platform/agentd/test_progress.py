#!/usr/bin/env python3
"""Runs monitor/progress.py's embedded selftest so the suite gates it (same pattern as
hooks/test_selftests.py — a selftest nothing runs is a selftest that rots)."""
import pathlib, subprocess, sys

r = subprocess.run([sys.executable, str(pathlib.Path(__file__).parent / "monitor" / "progress.py"),
                    "--selftest"], capture_output=True, text=True)
print(r.stdout.strip())
sys.exit(r.returncode)
