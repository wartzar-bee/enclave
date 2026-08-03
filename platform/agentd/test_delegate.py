#!/usr/bin/env python3
"""delegate.py CLI smoke tests.

Why this file exists: on 2026-08-03 the de-hardcoding commit deleted the KIND_MODEL table but left
`--kind`'s argparse `choices=list(KIND_MODEL)` behind. EVERY invocation — including `--help` — died
with NameError before parsing an argument, so delegation was dead on every pod running the shared
framework. The commit had verified _model_for() against the live policy.json and never once RAN the
CLI. The cheapest guard against that whole class is: execute the entry point.
"""
import json
import os
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DELEGATE = HERE / "delegate.py"
sys.path.insert(0, str(HERE))
import delegate  # noqa: E402

failed = 0


def check(name, cond, detail=""):
    global failed
    if not cond:
        failed += 1
    print(f"{'ok' if cond else 'FAIL'}: {name}{('  — ' + detail) if (detail and not cond) else ''}")


# 1. The entry point runs at all. This is the test that was missing.
r = subprocess.run([sys.executable, str(DELEGATE), "--help"], capture_output=True, text=True)
check("`delegate.py --help` exits 0 (no NameError at import/parse time)", r.returncode == 0,
      (r.stderr or "")[-400:])

# 2. Every kind the CLI accepts must actually resolve to a model, or the choice is a lie.
policy = {"models": {"nvidia": {"fast": "m-fast", "default": "m-default"}},
          "pools": {"nvidia": {"base_url_default": "https://example.invalid/v1"}}}
policy_path = HERE / ".test_delegate_policy.json"
policy_path.write_text(json.dumps(policy))
os.environ["DELEGATE_POLICY"] = str(policy_path)
try:
    for kind in delegate.DELEGATE_KINDS:
        try:
            model = delegate._model_for(kind)
        except Exception as e:  # noqa: BLE001 — the point is that it must NOT raise
            model = None
            check(f"kind {kind!r} resolves a model", False, f"{type(e).__name__}: {e}")
            continue
        check(f"kind {kind!r} resolves a model", bool(model), str(model))
    check("classify prefers the 'fast' capability", delegate._model_for("classify") == "m-fast")
    check("code falls through to 'default'", delegate._model_for("code") == "m-default")
finally:
    policy_path.unlink(missing_ok=True)
    os.environ.pop("DELEGATE_POLICY", None)

# 3. No vendor model name may reappear as CODE — that staleness cost a silent week. Comments are
# exempt on purpose: the file explains which retired model caused it, and that history is the point.
import ast  # noqa: E402

vendor = ("qwen/", "openai/gpt-oss", "meta/llama", "anthropic/claude", "nvidia/llama")
literals = [n.value for n in ast.walk(ast.parse(DELEGATE.read_text()))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]
offenders = [s for s in literals if any(v in s for v in vendor)]
check("no hardcoded vendor model names in code", not offenders, "; ".join(offenders)[:200])

print()
if failed:
    print(f"{failed} FAILED")
    raise SystemExit(1)
print("ALL PASS")
