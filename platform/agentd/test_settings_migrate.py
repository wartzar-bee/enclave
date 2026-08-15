#!/usr/bin/env python3
"""test_settings_migrate.py — run settings_migrate.py's embedded selftest under the CI runner."""
import pathlib, subprocess, sys

HERE = pathlib.Path(__file__).resolve().parent
r = subprocess.run([sys.executable or "python3", str(HERE / "settings_migrate.py"), "--selftest"],
                   capture_output=True, text=True)
sys.stdout.write(r.stdout)
sys.stderr.write(r.stderr)
sys.exit(r.returncode)
