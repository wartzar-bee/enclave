#!/usr/bin/env bash
# run_tests.sh — run the whole Enclave framework test suite (dependency-free; plain python3 scripts).
#
# Every test_*.py is a standalone script that exits non-zero on failure (no pytest needed — the baked
# agent image ships python3 with no pytest). This runner discovers them, runs each, and reports a
# summary; exit code is non-zero if ANY suite failed, so CI can gate on it.
#
# Usage:
#   bash run_tests.sh                 # run all suites (python + frontend-static; E2E auto-skips w/o browser)
#   bash run_tests.sh -k console      # only suites whose filename matches "console"
#   ENCLAVE_E2E=1 bash run_tests.sh   # force the Playwright E2E suite to run (else it self-skips)
cd "$(dirname "$0")" || exit 2

PY="${PYTHON:-python3}"
FILTER=""
[ "${1:-}" = "-k" ] && FILTER="${2:-}"

# Discover suites (sorted; bash-3.2 compatible — no mapfile). Skip the helper module itself.
SUITES=$(ls test_*.py hooks/test_*.py 2>/dev/null | sort)

pass=0; fail=0; failed_names=""
echo "=== Enclave test suite ($PY) ==="
for t in $SUITES; do
  if [ -n "$FILTER" ]; then case "$t" in *"$FILTER"*) ;; *) continue ;; esac; fi
  printf '\n--- %s ---\n' "$t"
  if "$PY" "$t"; then
    pass=$((pass+1))
  else
    fail=$((fail+1)); failed_names="$failed_names $t"
  fi
done

echo
echo "=================================================="
echo "SUITES: $pass passed, $fail failed"
if [ "$fail" -ne 0 ]; then
  echo "FAILED:$failed_names"
  exit 1
fi
echo "ALL GREEN"
