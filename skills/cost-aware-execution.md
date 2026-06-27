---
skill: cost-aware-execution
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (agentic-engineering + strategic-compact)
---

# Cost-aware execution — decompose, route, and compact deliberately

Tokens are the budget. Wasted context and over-tiered models are how a cheap run stops being cheap —
uncontrolled context growth and timer-driven re-invocation can burn an enormous amount of tokens fast.

## Decompose into ~15-minute units
Each unit: independently verifiable, has a **single dominant risk**, and a clear done condition. If a unit has two risks or no clear "done", split it.

## Route models by complexity — escalate only on a real reasoning gap
- **cheap/fast tier** (haiku / local / NVIDIA-free) — classification, boilerplate, narrow edits, bulk codegen. Delegate labor here.
- **mid tier** (sonnet) — implementation, refactors (~90% of coding).
- **top tier** (opus) — architecture, root-cause on hard bugs, multi-file invariants.
Escalate a tier **only when the lower tier fails with a demonstrated reasoning gap**, never by default. Track per task: model, rough tokens, retries, success/fail.

## Compact at boundaries, never mid-implementation
Manual compaction at a *logical* boundary beats arbitrary auto-compaction.
- Compact: after research→before planning, after a milestone, after a failed approach (clear the dead-end), before a major context shift.
- Do NOT compact mid-implementation — you lose var names, paths, partial state.
- **Write to files/memory BEFORE compacting.** What survives a compact: files on disk, memory, the work queue, git state. What's lost: intermediate reasoning, previously-read file contents, tool history.

## Watch true context pressure, not tool-count
Window fills by tokens, not number of calls — a few large reads can blow it in 3 calls. Judge by the real token sum (input + cache-read + cache-creation) against the window (~160k on a 200k model, ~250k on a 1M model), not by "I've made N tool calls."

## In a tick-loop runtime
Never re-read the whole vault each tick — read `state/recall.md`. Never `--resume` a tick (it re-bills the whole transcript as input). Keep ticks short + lean; durable files carry continuity, not a long thread.
