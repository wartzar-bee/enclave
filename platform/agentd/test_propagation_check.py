#!/usr/bin/env python3
"""test_propagation_check.py — run propagation_check.py's embedded selftest under the CI runner."""
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
r = subprocess.run([sys.executable or "python3", str(HERE / "propagation_check.py"), "--selftest"],
                   capture_output=True, text=True)
sys.stdout.write(r.stdout); sys.stderr.write(r.stderr)
sys.exit(r.returncode)
