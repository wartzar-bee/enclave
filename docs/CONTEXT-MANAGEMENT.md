# Enclave — autonomous-agent context management (research + design, 2026-06-29)

How other frameworks solve "the agent's context fills up → cost explodes / it degrades", and the design
we'll adopt. Grounds the cost lesson from the forgepod run ($112/8h, 64.3M cache_read = 99% of the bill).

## The field has converged (we're not reinventing the wheel)
Detection / trigger / action / who-decides, across Cline, Roo, OpenHands, Aider, LangChain, Manus,
Cognition/Devin, Claude Code itself:

1. **Detect** = token count as a **% of the model window** (minus reserved output + buffer). OpenHands is
   the lone event-count holdout (moving to tokens).
2. **Trigger %** = two camps: mainstream coding harnesses fire **HIGH ~80–92%** (Cline ~80, Roo ~80, Claude
   Code auto ~92–95, Codex 90); a conservative camp warns **~50%** (Gemini CLI; Cline's advisory mark) on
   the theory big windows degrade well before full. Proactive `/compact` is advised at **50–60%**.
3. **Action** = summarize-in-place dominates (keep first-N system+task + last-N recent tool calls, LLM-
   summarize the middle, re-inject active plan/files). Alternatives: **fresh-session-with-summary handoff**
   (Cline `new_task`) and **file/external-memory offload** (Manus, AutoGPT). Manus ladder: **raw > reversible
   compaction (keep path/URL, drop body) > summarization (last resort)**. Use a cheap model for summaries.
4. **Who decides** = **harness-automatic, NOT the agent.** ⚠ **Cognition/Devin tried letting the model
   self-summarize and REVERTED** — "the model didn't know what it didn't know"; deterministic harness-side
   compaction beat it. OpenHands is the only one that lets the agent self-trigger (opt-in tool).

**Lesson:** give the agent a real-time budget *signal* (the operator's instinct — correct), but keep the
TRIGGER + ENFORCEMENT in the harness and make the handoff a **deterministic template**, not a free-form
"summarize" the model writes (Cognition's lesson). Hybrid, not pure self-policing.

## The Claude-specific constraint (we run headless `claude -p`)
- Auto-compact exists (CLI + Agent SDK), fires ~92–95%, env `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100).
- **Per-turn `usage` in `--output-format stream-json`** gives real occupancy: `input + cache_read +
  cache_creation` (cache fields reliable; raw input/output in JSONL are streaming placeholders — don't sum
  them). `compact_boundary` system message = "a compaction just happened".
- **You CANNOT `/compact` a live `claude -p` session** (open gap #39275). The **Agent SDK CAN** (send
  `"/compact"` as input + a **`PreCompact` hook** to bank state first); the **Messages API** has server-side
  `compact_20260112` with an explicit `trigger`. → for true in-session compaction we'd move to the SDK/API.
- True window: 200k standard, **1M for `[1m]` models** (statusline hardcodes 200k — bug; don't trust it).

## ⭐ The agent must PREPARE its own handoff before the clear (operator's point — and the standard)
My token-ceiling backstop was WRONG: it `rm`s the session blind, with no chance for the agent to prepare.
The correct flow is "prep to resume → write the status file → THEN clear" — and both OpenClaw and ruflo
implement exactly this:

- **OpenClaw "memory flush"** (`/concepts/compaction`, `/reference/session-management-compaction`) — THE
  reference implementation. A **SOFT threshold fires BELOW the hard compaction limit**
  (`contextTokens > (window − reserveTokens) − softThresholdTokens`; soft ≈ 4k). When it trips, OpenClaw
  runs a **silent agentic turn** that tells the AGENT to "save important context to memory files" — **the
  agent itself decides what's durable and writes it** to `MEMORY.md` / `memory/<date>.md` BEFORE any
  summarization. THEN the harness does the mechanical thread-summary. Hybrid: **agent authors the durable
  handoff; harness does the lossy summary.** Runs the housekeeping turn on a **cheap/local model override**;
  replies `NO_REPLY` so it's suppressed; idempotent (one flush/cycle). Memory is 2-tier: curated always-
  loaded `MEMORY.md` + raw dated working logs, with a "distill up" habit.
  - ⚠ Two failure modes to copy-proof against: (a) the flush only covers compaction, **not `/reset`/end**
    (their open bug #8185) → so **write a handoff on EVERY exit**, not just under token pressure; (b) it's
    best-effort — one huge turn can leap past the soft threshold → keep a buffer + periodic save points.
- **ruflo / claude-flow** — `hooks session-end --generate-summary` makes the agent write its own handoff +
  a **`nextSession` "exact next action" pointer** as its last act; the native **`PreCompact` hook** writes
  state to disk before in-session compaction; and a **"store decisions after every step"** discipline means
  the handoff is continuously current, never reconstructed from soon-to-be-lost context. Memory lives in an
  external store split by **namespace** (decisions/patterns/tasks/blockers) so a resuming run fetches only
  the relevant slice, not the whole history.

**Conclusion:** never clear blind. The harness **SIGNALS** at a soft threshold → the agent **PREPARES** its
own handoff (free-form, self-selected, like a human "prep to resume" / our NOW.md) → **then** clear/reseed.
The agent authors the durable part; the harness only enforces the timing + the hard floor.

## The design we'll adopt (fits our headless + file-handoff topology)
Best fit = **Manus file-offload + Cline `new_task` fresh-session handoff**, harness-driven, agent-aware:

1. **Real-time budget signal (the warning mechanism).** A **PostToolUse `ctx_budget.py` hook** estimates
   occupancy after each tool call (read `transcript_path` / cumulative, vs the true window) and injects a
   graduated steer:
   - **~60%** → "context ~60% — wrap up the current sub-task, bank as you go, no large new reads."
   - **~75%** → "context ~75% — STOP: write your handoff (template below) + signal `session:clear`, then
     `finish`." (Hook exits 2 with the message, same pattern as the existing compactor/verify hooks.)
   This is what forgepod lacked: it ballooned to 2–3.7M because nothing told it "you're full".
2. **Deterministic handoff template** (NOT free-form): `task · decisions · files-touched (path:line) ·
   next-action · open-questions · last-2-tool-calls verbatim`. A fixed schema survives the clear; the next
   fresh session reseeds from it (Cognition: templated > model self-summary).
3. **Harness enforces** (already built): agent-driven `session:clear` (primary) + the token-ceiling backstop
   (lower it ~500k→~250k so it can't ride to 3.7M) + **reactive safety net**: on a real context-overflow
   error, auto-clear + retry (OpenHands' reactive path).
4. **Manus offload discipline** in CLAUDE.md: keep **paths/URLs/refs in context, bodies on disk**;
   `grep`/`cat` on demand instead of holding whole files. Recite the open `todo`/gaps at the tail.
5. **ONE canonical handoff file** (forgepod had release-gaps.md in /agent AND repo → it wasted turns
   reconciling "which is authoritative"). Pick one location.
6. **Optional step-up:** move the tick to the **Agent SDK** (real `/compact` input + `PreCompact` bank hook)
   or **Messages API `compact_20260112`** for deterministic in-session compaction — removes the "can't
   compact a live `-p` session" limit entirely. Bigger change; revisit if the hook approach isn't enough.

## Net
The earlier "just use short ticks" was a crude proxy. The real control = **agent-aware budget warning
(harness-measured) → deterministic bank → fresh-session handoff**, with file-offload to stretch runway.
Ticks can be as long as they're productive; the budget signal (not a fixed turn cap) is the brake.

## Sources
Comparative table: wasnotwas.com/writing/context-compaction · Manus: manus.im/blog/Context-Engineering-for-AI-Agents
· Cognition/Devin: cognition.com/blog/devin-sonnet-4-5-lessons-and-challenges · Cline: docs.cline.bot/prompting/understanding-context-management
· Roo: docs.roocode.com/features/intelligent-context-condensing · OpenHands: docs.openhands.dev/sdk/guides/context-condenser
· Aider repomap: aider.chat/docs/repomap.html · Claude Agent SDK agent-loop + PreCompact: code.claude.com/docs/en/agent-sdk/agent-loop
· Messages API compaction: platform.claude.com/docs/en/build-with-claude/compaction · Headless: code.claude.com/docs/en/headless
