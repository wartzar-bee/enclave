#!/usr/bin/env python3
"""contracts.py — completion contracts on directives (N2, 2026-07-20; idea from hermes /goal).

"Done" must be judged against EVIDENCE, not the agent's assertion — the operator rule
"Done = DEPLOYED + LIVE, never just committed" as a mechanism. A directive may carry a
machine-checkable contract; when a tick CLAIMS to have served that directive
(state/tick-status.json `serves`), the runtime runs the contract's check command. A failing
contract makes the claim visible as CLAIMED-NOT-VERIFIED: loud log line + one escalation —
the claim is never silently accepted.

Contract store — $AGENT_DIR/state/contracts.json (written by the operator/studio when issuing
the directive; the agent may READ it to know its bar, never write it):
  {
    "<directive-key>": {
      "cmd":    "curl -sk https://host/page | grep -c 'the new chapter'",
      "expect": "^[1-9]",              // optional regex on the command's output; default: exit 0
      "desc":   "new Ch2 actually live on RR"
    }, ...
  }
A directive-key matches a claimed serve if either string contains the other (agents cite
directive ids loosely). Timeout 90s per check; a check that cannot run counts as FAIL (a
contract you cannot evaluate is not satisfied — fail-closed, mirrors the verify-gate rule).

Run (from runtime.sh post_tick_shared): contracts.py <agent_dir>
Exit 0 always (never breaks the tick); results in state/contract-results.jsonl.
"""
import json
import pathlib
import re
import subprocess
import sys
import time


def load_json(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return None


def match_contracts(serves, contracts):
    """[(serve, key, contract)] — a key matches a serve if either contains the other."""
    out = []
    for s in serves:
        s_low = str(s).strip().lower()
        if not s_low:
            continue
        for key, c in contracts.items():
            k_low = key.strip().lower()
            if k_low and (k_low in s_low or s_low in k_low):
                out.append((s, key, c))
    return out


def run_check(contract, timeout=90):
    """(passed: bool, detail: str). Fail-closed: an unrunnable check is a FAIL."""
    cmd = contract.get("cmd", "")
    if not cmd:
        return False, "contract has no cmd"
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"check timed out ({timeout}s)"
    except Exception as e:
        return False, f"check could not run: {type(e).__name__}: {e}"
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    expect = contract.get("expect")
    if r.returncode != 0:
        return False, f"exit {r.returncode}: {out[:160]}"
    if expect and not re.search(expect, out, re.M):
        return False, f"output did not match /{expect}/: {out[:160]}"
    return True, out[:160] or "exit 0"


def main():
    ad = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "/agent")
    st = ad / "state"
    contracts = load_json(st / "contracts.json") or {}
    status = load_json(st / "tick-status.json") or {}
    serves = status.get("serves") or []
    if isinstance(serves, str):
        serves = [serves]
    if not contracts or not serves:
        return 0

    matched = match_contracts(serves, contracts)
    if not matched:
        return 0

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results, failed = [], []
    for serve, key, c in matched:
        ok, detail = run_check(c)
        results.append({"ts": now, "serve": str(serve)[:120], "contract": key,
                        "pass": ok, "detail": detail, "desc": c.get("desc", "")})
        if not ok:
            failed.append((key, c.get("desc", ""), detail))

    try:
        with (st / "contract-results.jsonl").open("a") as fh:
            for r in results:
                fh.write(json.dumps(r) + "\n")
    except OSError:
        pass

    if failed:
        for key, desc, detail in failed:
            print(f"CONTRACT FAILED [{key}] {desc}: {detail} — claim is CLAIMED-NOT-VERIFIED")
        try:
            with (st / "escalations.log").open("a") as fh:
                for key, desc, detail in failed:
                    fh.write(f"{now} ESCALATE :: [contract] {ad.name} claimed to serve '{key}' "
                             f"({desc}) but the completion check FAILED: {detail}. The directive "
                             f"stays OPEN — done means the check passes, not that the agent says so.\n")
        except OSError:
            pass
    else:
        print(f"contracts: {len(results)} completion check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
