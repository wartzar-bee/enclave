<!-- AGENT-RULES.md — FRAMEWORK-OWNED protocol block. version: 1
     Appended to EVERY tick's system prompt by runtime.sh, after the agent's own CLAUDE.md.

     Scope rule for this file, obey it when editing: put a rule here ONLY if framework code
     PARSES its output. These are wire formats between the agent and the harness, not advice.
     Opinions, craft and venture strategy belong in the agent's own CLAUDE.md.

     Why this file exists: before it, these rules lived in templates/autonomous/CLAUDE.md and were
     COPIED into a pod at creation. The template then evolved and no running pod ever inherited the
     change — 4 of 4 live pods were missing the `serves` contract, so contracts.py could never fire
     and one pod's contract sat dead for weeks. Same class as stale bind-mounted code, one layer up:
     committed is not live. This block is read fresh from the framework dir every tick. -->

# Harness protocol (framework-owned — the harness parses these; do not skip them)

## 1. Declare what you served — `/agent/state/tick-status.json`
Write that **absolute** path, not a relative one. A relative
`state/tick-status.json` lands under your *cwd* — `/work` or `/workspace`, not your home — and the
loop then has to recover it by guessing, or drops your declaration entirely and paces by inference.
Before you `finish`, write:
```json
{"status":"continue|blocked|done", "session":"clear|keep", "serves":["<directive-id>", ...]}
```
`serves` carries the id(s) from `state/directives.json` this tick actually worked on.
- The scorecard attributes your work by it. **An empty `serves` reads as drift**, not as modesty.
- If a directive you served carries a completion contract (`state/contracts.json`), the harness runs
  that check right after your tick. A claim whose contract fails is logged CLAIMED-NOT-VERIFIED and
  escalated — so only name a directive you actually advanced. Claiming more does not help you; it
  converts a quiet tick into a loud failed one.
- If nothing you did maps to a directive, say so: `"serves": []` plus a one-line why in
  `state/handoff.md`. That is an honest signal the harness can act on.

## 2. Budget a package — `state/budget.json`
```json
{"package":"<what this run of work is>", "soft_usd":N, "hard_usd":N}
```
**The `_usd` suffix is mandatory** — the feeder and the cost cutoff read those exact keys; a plain
`soft`/`hard` is silently ignored and you run uncapped. Calibrate from your own actuals.

## 3. Write back what you learned — `bin/memory.py`
A tick that discovers something and does not persist it has spent money to learn nothing: your
context is wiped between ticks, and the next tick re-derives the world from scratch.
- A durable fact / decision / correction → `memory.py --base /agent remember --type <fact|decision|lesson|user> "..."`
- **A reusable PROCEDURE you got working → `memory.py --base /agent learn <slug> "Title" --body-file <f> --gate`**
  The gate rejects one-liners and near-duplicates; a duplicate is a signal to re-learn THAT slug and
  bump its version, not to fork a new one. The two best-matching skills are reloaded into
  `state/recall.md` next tick, so a skill you write is a skill you get back.
- Re-version a skill the moment reality contradicts it. A skill at `version: 1` months after it was
  seeded is a skill nobody has tested.
The harness counts these as the `memory` write-class every tick — it is measured, and a long run of
zero is a finding against the pod.

## 4. Hand off what you can't do yourself — `state/outbox/` (typed envelope)
You can't `git push`, write to another pod, or fire an operator-gated action — you PREPARE, a studio
actor FIRES. Hand it off as ONE typed envelope, never a bespoke filename the studio has to know about:

    python3 platform/agentd/handoff.py emit --to <pod|studio|operator> --type <type> \
        --title "one-line summary" --payload '{...}'   # or --payload-file <f>

Writes `state/outbox/<utc>-<type>.json`. The off-Opus handoff-broker dispatches on `type`:
- **routing** types (`distribution-help`) auto-deliver to `to`'s inbox; the recipe returns to you.
- **judgment/operator** types (`maintainer-queue`, `board-request`, `glama-claim`, `operator-fire`,
  `release`, `cursor-correction`, `vision-captcha`) are surfaced for a studio session to fire.
An unknown `type` is surfaced, never dropped. This is the parsed handoff protocol — do NOT invent new
`state/*-queue.md` / `*-request*` filenames; emit an envelope so nothing you prepare rots unseen.

## 5. Keep every file you maintain LEAN — prune it, don't append to it
`work.json`, `inbox.md`, `handoff.md`, `state/*` are re-loaded EVERY tick, so anything you leave in them
is a recurring per-turn token cost. Maintain each as a live WORKING SET, never an append log:
- **`work.json` is a QUEUE, not a history log.** Open/doing items + a ONE-LINE status each. Move finished
  narration to `memory/activity/`; DROP done items. A dict of dated `status_*` / `kpi_*` prose is the
  anti-pattern (it re-loads every turn forever).
- **`inbox.md`:** mark `[x]` / clear a directive the moment you've acted on it.
- **`handoff.md`:** the ONE current lean handoff, overwritten each tick — not accumulated.
Every tick, prune outdated content and remove redundancy from what you touched. Bloat you don't clear,
you pay to re-read on every turn of every tick.
