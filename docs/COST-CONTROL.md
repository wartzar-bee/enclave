# Cost control — the mechanism that ACTUALLY runs

**Status: SHIPPED + live (2026-07-04, deep-review remediation `167045f`).** This is the authoritative
description of the running cost stack. It supersedes the *design-era* context docs where they conflict:
[`CONTEXT-AND-TICKS.md`](CONTEXT-AND-TICKS.md) (its `--resume` ban predates warm sessions),
[`CONTEXT-MANAGEMENT.md`](CONTEXT-MANAGEMENT.md)/[`-PLAN.md`](CONTEXT-MANAGEMENT-PLAN.md) (fresh-tick
handoff era). Review evidence + rationale: the studio's `reports/enclave-review/PLAN.md`.

## The metric: calibrated `cost_est`
`usage_capture.py` parses the tick's stream-json and writes `state/.ctx-budget.json`
`{turn, ctx_tokens, cost_est, cost_raw}` per assistant turn. `cost_raw` sums per-turn tokens at list
rates — structurally HIGH vs Claude Code's authoritative `total_cost_usd` (measured ~7× on stoneforge).
`cost_est` = `cost_raw × ratio`, where ratio is a per-model EMA of `(actual total_cost_usd / raw
estimate)` learned at every result event and persisted to `state/.cost-calibration.json`.
**Rule: a wrong metric gets calibrated at the source — never compensated by inflating the caps.**
Unknown model → ratio 1.0 (over-estimates → caps fire early → self-corrects after one tick).

## The caps (honest dollars, agent.env)
`CTX_COST_SOFT_USD` / `CTX_COST_HARD_USD` / `CTX_COST_HARD_MAX` (defaults 2.0/3.5/6.0). The agent
plans a per-package budget in `state/budget.json {package, soft_usd, hard_usd}`; the floors clamp it
UP (no thrash) and `HARD_MAX` clamps it DOWN (runaway ceiling).

## Enforcement chain (per tick, INJECT mode — `tick_feeder.py`)
The prompt is fed to `claude -p --input-format stream-json` via a FIFO; the feeder then watches
`.ctx-budget.json` and injects USER messages the model obeys (`next_injection()`, unit-tested):
1. **WARN1** at soft — bank the chunk, refresh `handoff.md`, no new sub-task.
2. **WARN2** at soft+60% — finalize handoff NOW.
3. **TURNWRAP** at 80% of `MAX_TURNS` — wrap up before the guillotine (the old behavior discarded
   the tick's unsaved work at `error_max_turns`; 57 ticks/$111 died that way).
4. **STOP** at hard — write handoff + `tick-status {"status":"continue","session":"clear"}` + finish.
5. **Kill backstop** `GRACE` seconds after STOP (scoped: kills only THIS agent's claude by cmdline
   match on its agent dir — never other agents' ticks). Post-kill, `runtime.sh` clears the warm
   session and seeds a fallback handoff; the next tick cold-starts cheap.

Warm-session nets in `runtime.sh` (pre-tick): occupancy floor (`CTX_HARD_TOKENS`), session-cost floor
(resumed session already ≥ soft → drop it), new `[tier:top]` inbox directive → cold-start.

## The outer bounds (independent of agent behavior)
- **Subscription %-guard** (`claude_usage.py guard`): defers the tick (exit 75) at the 5h/7d floor.
  **Blind = loud**: no reading or a stale cache exits 66 → `runtime.sh` falls back to ccusage; if that
  is also dark it logs `SPEND-GUARD BLIND`, escalates `[guard:blind]` (deduped 6h) and proceeds
  fail-open — visibly. Every guarded tick prints `guard OK: 5h X% / wk Y%`.
- **Out-of-pocket weekly cap, ALL brains**: `API_BUDGET_WEEKLY_USD` (default 15) over a 7-day window
  of `state/api_spending.jsonl`; at the wall → escalate `[budget:external]` + defer. (Replaces the
  BRAIN=api-only cumulative-forever check, which permanently bricked agents.)
- **`MAX_TURNS` + `TICK_TIMEOUT`** per tick; **lock PID-liveness** (dead holder reclaimed immediately,
  live holder never killed).
- **Model tiering**: `ROUTER=on` + `MODEL_ROUTINE=<cheap tier>` routes routine/heartbeat ticks off the
  top model. `runtime.sh` warns `ROUTER NULLIFIED` when `MODEL_ROUTINE == MODEL`. Open `[tier:top]`
  inbox items pin the top model until done-flipped — **inbox hygiene IS the router**.

## Pacing (agentloop.py `_after`)
`tick-status.json`: `continue` → cooldown re-fire · `idle` → INTERVAL heartbeat · **`blocked`**
(`{"status":"blocked","waiting_on":"<concrete dependency>"}`) → INTERVAL heartbeat + `state/.blocked`
marker `{since, waiting_on}`, waking instantly on inbox/comms. Blocked exists so an agent waiting on
something external never busy-waits paid ticks (8 consecutive Opus "still blocked" ticks motivated it).

## Metering coverage
Work ticks (`usage_capture.py`) AND chat turns (`chat_responder.py`, `reason="chat"`) append to
`state/usage.jsonl` — chat used to be invisible spend. The capture also closes
`budget-calibration.jsonl` with real actuals per package.

Tests: `test_usage_capture.py` (calibration math + pipeline), `test_tick_feeder.py` (injection
decisions), `test_agentloop.py` (wake + pacing incl. blocked).
