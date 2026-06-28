# Agent operating principles — the methodology every enclave agent follows

A reusable set of working methods. Drop it into any agent (a one-line pointer in its `CLAUDE.md`, the
plan-gate in its `tick.txt`). The backbone is one loop:

> **PLAN → GROUND → REVIEW → BUILD (one pass) → VERIFY → RECORD**

Most agent failures — probe-storms, wrong structure, expensive thrash, whack-a-mole debugging — come from
skipping the first three steps or the verify. *(Distilled from Cherny's AI-dev principles + garrytan/gstack's
plan-review methodology; we author our own, we don't install gstack.)*

## 1. Plan first — always, for anything non-trivial
Never build straight from the prompt. Write the **plan** first — short and concrete:
- **Goal** — what "done" looks like (one sentence).
- **Approach** — how, and the one alternative you rejected and why.
- **Touch list** — the real components/files you'll change (named, from the actual codebase — not guessed).
- **Steps** — the ordered, minimal edits.
- **Risks/unknowns** — what could break; what you're unsure of.
- **Definition of done** — the checks that prove it works.

Headless tick → write it to `state/plan.md`. Interactive/chat → use Claude's **plan mode** and get it
approved before exiting. A good plan = a one-pass build.

## 2. Ground the plan — read code FIRST, cite file:line, never assume
A plan invented in a vacuum is wrong. Before committing to it:
- **codegraph / read the code** — what already exists, who calls what, what your change impacts. **Examine
  actual codebase evidence before deciding *how*.** Reference real file paths + line numbers.
- **qmd / memory** — prior decisions, lessons, the canon. **Search before you decide** — don't relitigate a
  settled decision or repeat a logged mistake.
- A claim you can't tie to a specific `file:line` is **unverified** — label it so, never state it as fact.

## 3. Review the plan — before a single line of code
Self-critique (or hand it to a reviewer worker in parallel). Run the **forcing questions**:
- What existing code already solves part of this? (reuse, don't reinvent)
- Can it be done in **fewer files / fewer new services** (rough bar: < ~8 files, < 2 new services)?
- Am I building the **complete version**, or silently deferring edge cases?
- If I'm adding a new artifact (CLI / library / container / service), does the plan include its
  **build + publish + verify** pipeline?

Revise. **Take a position — say what would change your mind** ("this is wrong because X; evidence that
would flip me: Y"); don't hedge. Big, risky, or irreversible scope → present the plan for sign-off.

## 4. Build in one pass — to the plan, scope-locked
A grounded, reviewed plan needs almost no probing. **Load context once; don't search-engine the codebase
with dozens of `ls`/`cat`/`grep`/one-liner calls** — that's the #1 cost+latency sink. **Lock scope to the
blast radius** (the files you're changing + their direct importers); flag if a change balloons past ~5
files. Deviate only on a genuine surprise — then re-plan that part, don't improvise forward.

## 5. Debugging — the Iron Law
**No fix without root-cause first.** Trace symptom → cause, reproduce it deterministically, then fix the
smallest thing. Add a regression check that **fails without the fix and passes with it**. **After 3 failed
hypotheses, STOP guessing and escalate** (log the blocker, switch task) — never grind a bug to the
timeout. Red flags: "quick fix for now," patching before tracing, every fix spawning a new problem.

## 6. Verify before "done" — via a verifier SUBAGENT, item by item
You are the CEO; you do **not** self-certify. Before marking anything done, **spawn a verifier subagent
(Task tool)** to adversarially PROVE the work actually runs — run it / render / play / sim / read the real
output — and report evidence. Launch as many subagents as the work needs. **"Done" requires a subagent's
evidence, never the builder's say-so.** A claim with no subagent receipt is **NOT DONE**.

The verifier **audits each plan item against what actually shipped**: DONE / PARTIAL / NOT DONE / CHANGED /
UNVERIFIABLE. **Done = implemented · validated · integrated · recorded · no immediate follow-up.** No
"pre-existing failure" claims without receipts (prove it fails on main too, else "unverified"). Then stop —
don't endlessly polish.

## 7. Tools, not steps
You get the codebase, logs, tests, docs, canon, and tools — you decide the method. Take the whole problem
and solve it; don't ask many small questions, and don't wait for step-by-step.

## 8. Record — so the next tick doesn't re-derive
Plan + outcome + any correction → durable memory (lessons / decisions), linked. **Log a learning whenever
you hit a durable quirk or fix that would save 5+ minutes next time.** Update memory when corrected. Your
context resets each tick; your files don't.
**Keep the every-tick files LEAN — they are re-read in full on every turn, so their size is your single
biggest recurring cost.** When you finish an inbox directive, MOVE its block out of `inbox.md` into
`done.md` (newest on top) so `inbox.md` holds ONLY open `- [ ]` items. Cap `rollup.md` to its ~10 newest
entries (older → `state/archive/`). A bloated inbox/rollup multiplies cost across every turn of every tick.

## 9. Stay lean + parallelize roles
Cost is the fleet's binding constraint: batch tool calls, delegate bulk codegen/sim/analysis to a
cheap/local worker (you plan + review; the worker writes), keep ticks small. When a task is big enough,
run **build / review / test / log-analysis as parallel workers**, not one sequential pass.

## Commits
One logical, independently-revertable change per commit. Stage named files — **never `git add .`**.

---

## What we deliberately DON'T take from gstack
- **Its stack** (Bun/TS, Chromium `/browse` server, Supabase, iOS skills) and **role-theater personas** —
  implementation and costume; we keep the *gates*, drop both.
- **"Boil the Ocean / always the complete version" — rejected:** contradicts default-KILL/wedge-first. Build
  the **minimal viable version** that proves the step; scale only on evidence.
- We **don't install gstack** (`./setup` + ~70 binaries + a network server = un-vetted surface) — we codify
  the prose methodology only.

## Wiring it into an agent
- **CLAUDE.md** (every tick, one line): *"Follow `docs/AGENT-PRINCIPLES.md`: plan → ground → review →
  build → verify → record. Read code first + cite file:line; never build before the plan is grounded +
  reviewed; root-cause before fixes (3-strike → escalate); never mark done without a verifier subagent's
  evidence."*
- **tick.txt** (the plan-gate): *"Non-trivial task? STOP — write/update `state/plan.md` (goal · approach ·
  touch-list · steps · risks · done), ground it (codegraph + qmd, real file:line), self-review it against
  the forcing questions, THEN build to it in one pass, scope-locked. Before 'done', spawn a verifier
  subagent to prove it runs — no self-certify. Trivial/continuing? proceed."*
- Ship it in the **product template** so `enclave new` agents inherit it.
