# Enclave context-management mechanism — detailed plan (critically reviewed)

Goal: bound per-tick cost + never lose work, by giving a headless `claude -p` agent a **real-time budget
signal** and a **chance to prepare its own handoff before any clear** (OpenClaw "memory flush" pattern;
ruflo `session-end`/`PreCompact`). Builds on the research in `CONTEXT-MANAGEMENT.md`.

---
## PART 1 — CRITICAL REVIEW FIRST (these findings reshape the naive design)

**C1. The hook CANNOT see token usage — measurement must come from the stream parser.**
A Claude Code PostToolUse hook receives `{tool_name, tool_input, tool_response, transcript_path,
session_id}` — **no token/usage data.** The accurate occupancy signal (`input + cache_read +
cache_creation`) lives in the `stream-json` that `usage_capture.py` already parses.
→ **Design consequence:** the *parser* measures and writes a tiny `state/.ctx-budget.json` per assistant
message; the *hook* just reads that file (cheap) and decides whether to steer. Two processes, one file.
Fallback if the parser can't emit per-turn (only at tick end): a **turn/tool-call counter** the hook
increments itself — coarser but reliable. Ship both; prefer the parser value, fall back to the counter.

**C2. The threshold is a COST budget, NOT a % of the 1M window.**
Our pain was **cost (cache_read), not hitting the limit.** At 700k context every turn re-reads 700k →
expensive long before 1M. So clearing at "70% of 1M" is far too late.
→ **Use an absolute cost budget ~200k soft / ~300k hard**, well below the 1M window. This caps per-turn
cache_read → caps the dominant cost. (Window-limit safety is the *outer* backstop, not the trigger.)

**C3. "finish" alone re-triggers instantly on warm-resume.**
If the agent just `finish`es at the threshold, the next tick `--resume`s the SAME ~300k session → the hook
fires again immediately → loop.
→ **The hard steer MUST make the agent set `session:clear`**, so the next tick is a FRESH session that
reseeds from the handoff. finish-without-clear is a bug.

**C4. Agent compliance is not guaranteed — layer the defenses (graceful degradation).**
The model may ignore or half-comply with the steer (Cognition's lesson: don't trust self-management alone).
→ **Four layers, escalating:** (1) soft steer ~200k (prep), (2) hard steer ~300k (bank+clear+finish),
(3) Claude's own auto-compact ~92% (lossy, last resort within a tick), (4) runtime token-ceiling **clear
between ticks** + **reactive: on a real context-overflow error, clear+retry** (OpenHands' reactive path).
No single layer is trusted.

**C5. Clearing more often risks MORE re-grounding cost — so the handoff must be ONE lean, self-sufficient
file.** forgepod's waste was re-reading the whole gap-list + `recall.md` + files every fresh tick, AND
reconciling TWO copies of `release-gaps.md`.
→ **One canonical lean handoff** (`state/handoff.md`) that is *everything* a fresh tick needs — so
re-grounding is "read one small file," not "re-derive my world." Distill-up discipline (OpenClaw two-tier).

**C6. Don't clear mid-implementation.** (Our own `cost-aware-execution` skill + general wisdom.) A hard
clear at 300k could land mid-edit and lose var names/paths.
→ The **soft** threshold (200k) gives ~100k of runway to reach a logical boundary before the hard clear;
the handoff template's "EXACT next action + files:line + partial state" makes even a mid-task resume safe.

**C7. Cheap-model housekeeping is hard in raw `claude -p`.** OpenClaw writes the flush on a cheap/local
model; in raw `claude -p` the agent IS Opus, so the handoff write costs Opus tokens.
→ **Keep the handoff SMALL (fixed template)** so the write is cheap. True cheap-model flush needs the
**Agent SDK** (separate compaction model) — defer to Phase 2.

**C8. Cover EVERY exit, not just token pressure.** OpenClaw's own open bug (#8185): `/reset` skips the
flush. A killed/MAX_TURNS/clean-finish tick must also leave a current handoff.
→ **tick.txt mandates: refresh `handoff.md` before `finish`, every tick.** + continuous append-bank so a
crash mid-tick still leaves a trail (ruflo "store after every step").

**C9. Build-our-own vs step up to the Agent SDK / Messages API.** The SDK gives native `/compact` +
`PreCompact` + cheap-model flush; the Messages API gives server-side `compact_20260112`. Our hook approach
reinvents part of this for raw `claude -p`.
→ **Phase 1 = the hook approach** (small, drops into existing `runtime.sh`, no rewrite). **Phase 2 =
evaluate the Agent SDK migration** for native in-session compaction + cheap flush. Decide after Phase 1.

**C10. Testing is Opus-dependent.** Can't fully validate without a real tick.
→ Unit-test the deterministic parts (parser emits the budget file; hook threshold logic; handoff schema
present). Integration: a **cheap-model dry run** (NVIDIA/local agent) to watch compliance, THEN one
supervised Opus tick. Never let it run unattended during validation.

---
## PART 2 — ARCHITECTURE (data flow)

```
claude -p (stream-json) ──pipe──► usage_capture.py
                                     └─ writes state/.ctx-budget.json  {tokens, pct, turn}  (per assistant msg)
PostToolUse after each tool ──────► ctx_budget.py (NEW hook)
                                     └─ reads .ctx-budget.json (+ own turn counter fallback)
                                     └─ < soft: pass · ≥ soft: exit2 "wrap up + refresh handoff"
                                                        · ≥ hard: exit2 "bank handoff + session:clear + finish"
agent ──────────────────────────► writes state/handoff.md (template) + tick-status {session:"clear"}
runtime.sh (next tick) ───────────► sees session:clear → fresh session → agent reads handoff.md (seed)
                                     reactive: overflow error → clear + retry ; outer floor: token-ceiling clear
```

Components:
- **`usage_capture.py`** (exists, in the pipe): + emit `state/.ctx-budget.json` per assistant message.
- **`ctx_budget.py`** (NEW PostToolUse hook): graduated steer; thresholds from env
  `CTX_SOFT_TOKENS`/`CTX_HARD_TOKENS` (default 200k/300k); fail-open if the budget file is missing.
- **`state/handoff.md`** (NEW, canonical, lean): the single resume-from file.
- **`runtime.sh`** (exists): already honors `session:clear` + the token-ceiling backstop; add overflow→retry.
- **`tick.txt` / CLAUDE.md** (agent instruction): handoff template + "refresh before every finish" + the
  Manus offload discipline (keep paths/refs in context, bodies on disk; `grep` not `cat`).

## PART 3 — THE HANDOFF TEMPLATE (deterministic, not free-form)
```
# handoff.md  (overwrite every tick; the ONLY thing a fresh session reads to resume)
OBJECTIVE:        <the directive in one line>
NOW DOING:        <the current sub-task>
EXACT NEXT STEP:  <the single next action, concrete>
KEY FILES:        <path:line — the 3-6 files/lines that matter right now>
DECISIONS:        <durable choices made (so they're not re-litigated)>
DONE THIS SESSION:<bullet list, terse>
BLOCKERS / DO-NOT-TOUCH: <constraints>
LAST TOOL RESULTS (verbatim, 1-2): <preserve the model's "rhythm">
```

## PART 4 — IMPLEMENTATION STEPS (ordered, each independently testable)
1. **`usage_capture.py`** → write `state/.ctx-budget.json {tokens,pct,turn}` per assistant message. Unit-test.
2. **`ctx_budget.py`** PostToolUse hook → read budget (+counter fallback) → graduated exit-2. Unit-test the
   threshold logic. Register in the Claude templates' settings + live forgepod.
3. **Handoff template + tick.txt/CLAUDE.md** → mandate handoff.md write before finish; hard-warn ⇒
   `session:clear`; add Manus offload discipline.
4. **`runtime.sh`** → keep `session:clear` honoring; lower the token-ceiling backstop to the cost floor
   (~300k); add **overflow-error → clear+retry**.
5. **One canonical handoff path** → kill the two-copy confusion.
6. **Tests** → parser file, hook thresholds, handoff schema presence. Then a cheap-model dry run, then ONE
   supervised Opus tick to tune `CTX_SOFT/HARD`.

## PART 5 — PHASING
- **Phase 1 (this plan):** hook + handoff + clear + reactive, on raw `claude -p`. Land on forgepod,
  validate cost drop on a supervised tick, then bake into the enclave product templates.
- **Phase 2 (later, decide after P1):** Agent-SDK migration for native `/compact` + `PreCompact` +
  cheap-model flush — only if the hook approach proves insufficient.

## PART 6 — DECISIONS (resolved with operator, 2026-06-29)
1. **Budget = AGENT-PLANNED per work-package, not a fixed global line** → see PART 7 (the core refinement).
2. **Phase 1 (hook) now; Phase 2 (Agent SDK) later** — operator deferred to me; we ship Phase 1, keep SDK as a documented Phase 2 option.
3. **Build product-ready on forgepod first, designed to roll to ALL agents** (templates) once validated.
4. **Opus writes the handoff** + **real-time cost/budget monitoring + intervention is REQUIRED** → see PART 8.

Payoff to validate on ONE supervised tick: capping context vs ballooning to 1–3.7M cache_read → est. **2–4×** cheaper ($112 run → ~$30–50) at equal quality.

## PART 7 — AGENT-PLANNED BUDGETED WORK-PACKAGES (the primary mechanism)
The token threshold (PART 1–3) becomes the **safety net**, not the trigger. The trigger is the agent's own
plan. Flow:

1. **Plan a coherent PACKAGE, not a task-hop.** At the start of a unit, the agent writes `state/plan.md`:
   the package = a set of **related** tasks (e.g. "Feature 1: tasks A–J"), explicitly NOT mixing unrelated
   work. (Fixes forgepod's buy-bonus→l10n→math thrash.)
2. **Estimate a token BUDGET for the package** from budgeting knowledge we seed (a reference table — see
   below) + its own past calibration: e.g. `est 110k → soft 150k / hard 200k`. Written into plan.md.
3. **Work the package, tracking actual vs planned in real-time.** The `ctx_budget` hook now compares the
   live spend (from the parser's budget file) against the **agent's planned soft/hard for THIS package**,
   not just a global line: *"you're at 80% of your 150k package budget — reach the boundary + hand off."*
4. **Hand off at the package boundary** (planned, clean — never mid-task), `session:clear`, fresh tick
   starts the NEXT package.
5. **LEARN.** After each package, record `{package, estimate, actual, delta}` → the agent updates its
   budgeting instinct via `memory.py learn` (an "estimation calibration" memory). Over time estimates
   converge. This is the ECC instinct-learning loop applied to cost.

**Seed budgeting knowledge** (a starter reference table in a skill/memory, refined by calibration):
| Work type | rough token budget |
|---|---|
| One UI-state gap (build + drive-verify) | ~30–50k |
| A small feature + QA driver | ~80–120k |
| Math RE + sim validation | ~60–100k |
| Art/asset pass (gen + place + render-check) | ~40–80k |
| Pure research/audit (no build) | ~30–60k |

**⚠ CRITICAL CAVEATS (why the safety net stays):**
- **Models estimate tokens BADLY out of the gate** (no innate token sense). The plan-budget is only as
  good as the calibration loop — so the **global hard floor (PART 1, C2/C4) ALWAYS stays** as the real
  bound. We trust the *plan* for clean boundaries, the *floor* for overruns/under-estimates. Never the
  estimate alone.
- **A package can be mis-scoped too big.** Planning must include "decompose so each package fits a sane
  budget (≤~200k)"; if a feature is bigger, it's multiple packages with handoffs between.

## PART 8 — REAL-TIME COST + BUDGET MONITORING & INTERVENTION (operator requirement)
We must be able to **watch cost live and stop / update / fix mid-run** — not discover a $112 burn after.
1. **Live spend stream.** `usage_capture.py` already emits per-assistant-message usage → also write a
   rolling `state/.ctx-budget.json {tokens, cost_usd_so_far, pct_of_package_budget, package, turn}` so the
   *current* tick's cost is visible WHILE it runs (today the dashboard only sees cost at tick END).
2. **Dashboard surface (live, per agent):** current package + planned budget + **% consumed** + **running
   $ this tick** + a projection. A budget bar that goes amber→red as it approaches the package soft/hard.
3. **Intervention controls:** **Pause** (finish the current tick, don't start the next — cleaner than a
   hard `down`), **Stop** (halt now), and **inject a correction** (the existing act-capable chat — "drop
   this package / re-plan / fix X"). The agent reads injected corrections at the next tick boundary.
4. **Auto-guards (off-Opus, no human needed):** the existing studio-monitor daemon watches the live
   budget file and (a) alerts on a runaway tick (spend/min over a ceiling), (b) can auto-pause an agent
   that blows past its hard floor N times — so an overnight run can't repeat the $112 surprise.

This closes the loop: agent plans + budgets + learns (PART 7), the harness measures + warns + enforces
(PART 1–4), and the operator/monitor sees it live and can stop/update/fix (PART 8).
