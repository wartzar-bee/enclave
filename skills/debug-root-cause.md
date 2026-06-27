---
skill: debug-root-cause
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (agent-introspection-debugging) + AGENT-PRINCIPLES Iron Law
---

# Debug by root cause — one discriminating check before any retry

The Iron Law: **no fix without a root cause first.** Retrying the same action with slightly different
wording is the #1 failure mode of agents — it burns tokens and proves nothing.

## When a run is stuck (looping, retrying, drifting, burning tokens with no progress)
1. **Capture before retrying** — the error/stack, the last few tool calls, what you were trying to do, and the env assumptions (cwd, branch, which service/port, expected files).
2. **Diagnose with the pattern→cause→check table:**

| Symptom | Likely cause | The one check to run |
|---|---|---|
| max tool-calls / same cmd repeated | loop with no exit | inspect the last N calls — what condition never flips? |
| degraded reasoning / context bloat | unbounded notes/logs in context | look for duplicated plans / huge pasted output |
| ECONNREFUSED / timeout | service down / wrong port | verify health + URL + port directly |
| 429 / quota | retry storm, no backoff | count calls + retry spacing |
| file missing after write / stale diff | wrong cwd / branch drift / race | recheck path + `pwd` + `git status` |
| tests still fail after the "fix" | wrong hypothesis | isolate the exact failing assertion |

3. **One discriminating check** — run the single observation that confirms or kills the hypothesis. Switch from speculation to direct observation of world state. Change the plan only if the check supports it.
4. **After 3 failed hypotheses, STOP guessing and escalate** (`state/escalations.log`). Don't spin.

## Honesty guard
Don't claim auto-healing actions you can't actually perform ("reset agent state", "updated the harness") unless you really did it with a real tool. End with: the failure pattern + root-cause hypothesis + the recovery action + evidence it's now better OR still blocked — never just "I fixed it."

## Regression check on every fix
A real fix comes with a check that **fails without it and passes with it.** No such check → you haven't proven the fix.
