#!/usr/bin/env python3
"""
test_selftests.py — run every security hook's embedded `--selftest` under the CI runner.

The security-critical hooks (guard.py, secret_scan.py, delegation_guard.py) each ship a `--selftest`
with their own assertions, but run_tests.sh only ever discovered `test_*.py`, so NOTHING executed
those selftests — the enforcement layer's tests existed and were never run. That is exactly how
secret_scan.py shipped an import that crashed on `--selftest` (silent fail-open) while the suite
stayed 29/29 green. This wrapper closes that gap: it auto-discovers every hook module that exposes
`--selftest`, runs it as the deployed artifact would (a subprocess, not an in-process import), and
fails the suite if any selftest is missing-or-broken. Discovery is dynamic, so a selftest added to a
new hook is covered automatically.

Standalone, no deps; exit 0 = all green, non-zero = a hook selftest failed or errored.
"""
import pathlib, subprocess, sys

HOOKS = pathlib.Path(__file__).resolve().parent
PY = sys.executable or "python3"


def _hooks_with_selftest():
    found = []
    for f in sorted(HOOKS.glob("*.py")):
        if f.name.startswith("test_"):
            continue
        try:
            if "--selftest" in f.read_text(encoding="utf-8", errors="replace"):
                found.append(f)
        except OSError:
            continue
    return found


def main():
    hooks = _hooks_with_selftest()
    if not hooks:
        print("test_selftests: no hook exposes --selftest — expected at least guard/secret_scan/"
              "delegation_guard; treating as failure")
        return 1
    fails = []
    for f in hooks:
        r = subprocess.run([PY, str(f), "--selftest"], capture_output=True, text=True)
        tail = (r.stdout or r.stderr or "").strip().splitlines()
        tail = tail[-1] if tail else ""
        status = "OK" if r.returncode == 0 else f"FAIL(rc={r.returncode})"
        print(f"  {f.name:24s} {status}  {tail}")
        if r.returncode != 0:
            fails.append(f.name)
            if r.stderr:
                sys.stderr.write(r.stderr)
    print(f"\nhook selftests: {len(hooks) - len(fails)}/{len(hooks)} green")
    if fails:
        print("FAILED: " + ", ".join(fails))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
