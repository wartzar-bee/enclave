---
name: budget-and-handoff
description: Plan work as coherent budgeted PACKAGES, keep a lean handoff so context never balloons, and clear at boundaries — the cost discipline for a persistent agent.
origin: enclave (informed by ECC strategic-compact, OpenClaw memory-flush, ruflo session-end, Manus offload)
---

# Budget & handoff — work in bounded packages, never let context balloon

A persistent agent's #1 cost is **cache_read**: every turn re-reads the whole context, so cost scales with
`turns × context`. Long ticks that balloon to 1M+ tokens are the money-fire. Bound it by working in
**planned, budgeted packages** and **handing off at boundaries** — not by guessing.

## 1. Plan a PACKAGE, not a task-hop
At the start of a unit, scope a **coherent package**: a set of *related* tasks (e.g. "Feature 1: the 8 UI-
state gaps"). Do NOT mix unrelated work in one package (no buy-bonus → localization → math in one go).

## 2. Budget the package in $ → write `state/budget.json`
Estimate what the package should COST and set a soft/hard **$** budget. **Cost — not context size — is what
the hook watches** (a lean session can still rack up many turns and burn $). Write:
```json
{"package":"Feature 1: UI states","soft_usd":2.5,"hard_usd":4.0}
```
Starter $ estimates (calibrate against actuals — §5):
| Work type | rough $ |
|---|---|
| One UI-state gap (build + drive-verify) | ~$1–2 |
| Small feature + QA driver | ~$2–4 |
| Math RE + sim validation | ~$2–4 |
| Art/asset pass (gen + place + render-check) | ~$1.5–3 |
| Research/audit (no build) | ~$1–2 |
Keep a package ≤ ~$4; if a feature is bigger, split it into multiple packages with handoffs between.

## 3. Keep ONE lean handoff: `state/handoff.md`
This is the **only** file a fresh session reads to resume — so it must be self-sufficient and small.
Refresh it **as you work** and **before every `finish`**. Schema:
```
OBJECTIVE:        <directive in one line>
NOW DOING:        <current sub-task>
EXACT NEXT STEP:  <the single next action, concrete>
KEY FILES:        <path:line — the few files/lines that matter now>
DECISIONS:        <durable choices, so they're not re-litigated>
DONE THIS SESSION:<terse bullets>
BLOCKERS / DO-NOT-TOUCH:
```

## 4. Hand off at the boundary — and obey the budget warning
The `ctx_budget` hook warns you with your live **$ spend** vs your package budget:
- **soft** ("📊 spend $X ≥ soft $Y"): wrap up the current sub-task, refresh `handoff.md`, no big new reads, don't start a new sub-task.
- **hard** ("🛑 spend $X ≥ hard $Y"): **finalize `handoff.md`, write `state/tick-status.json {"status":"continue","session":"clear"}`, then `finish`.** The next tick starts lean from your handoff. Never clear mid-edit — the soft warning gives you room.
- **Verify ONCE, then stop.** A render + one drive-through is enough; do NOT re-verify the same thing at multiple viewports / iterate cosmetically — that burns turns ($) for no real gain. When the task is verified + committed, hand off and go idle.

## 5. Calibrate — get better at estimating
After a package, append `{package, est_usd, actual_usd}` (actual = the tick's `cost_est`) to
`state/budget-calibration.jsonl` and `memory.py learn` if the estimate was far off. Consult it next time.

## 6. Offload discipline (keep context lean)
`grep`/`Read offset:limit`, never `cat` a whole file; pipe big output to a file + grep the lines you need;
keep **paths/URLs/refs** in context, **bodies on disk** — recall on demand. (The compactor hook enforces this.)
