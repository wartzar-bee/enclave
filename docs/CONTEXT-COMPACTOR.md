# Within-tick context compactor — scope

**Status:** SCOPE / not built. Sibling to [`CONTEXT-AND-TICKS.md`](CONTEXT-AND-TICKS.md) (which covers
*between-tick* fixed cost) — this covers the gap that doc leaves open: **within-tick bloat**, the #1
live cost driver on stoneforge today.

## The problem (measured)

`CONTEXT-AND-TICKS.md` solves the *fixed* per-tick cost (lean `CLAUDE.md`, `recall.md` digest, no
`--resume`, off-Opus continuous). It does **not** address what happens *inside* one long tick:

> Every tool call's **output** is appended to the running transcript, and the whole transcript is
> re-sent to the model on every subsequent turn. A tick that does 40 tool calls re-sends an
> ever-growing blob 40+ times.

### What the number actually is (measured 2026-06-26)

The "1–5.7M tokens/tick" is **not** the context window (that's capped ~200K and auto-compacted, §B1).
It is **`cache_read` accumulation**: each turn re-reads the whole cached window, and that sums across
the tick. From `usage.jsonl`:

| turns | cache_read | input | cost (Sonnet) |
|------:|-----------:|------:|--------------:|
| 19    | 934K       | 3.3K  | $1.70         |
| 22    | 1.08M      | 22    | $0.91         |
| 42    | 1.75M      | 36    | $0.95         |
| 76    | 5.63M      | 78    | **$2.67**     |

`input` (genuinely new prompt content) is tiny; **`cache_read` is the whole bill and it scales with
turns** (~50–74K re-read per turn). So:

> **cost ≈ turns × window-size.** Two multiplicative levers: shrink the **window** (§A, the compactor)
> and cut the **turns** (§B, smaller ticks). They compound.

The driver is tool-output accumulation in long, tool-heavy ticks — NOT auto-loaded files (a tiny
fraction; trimming inbox/memory moved nothing). Confirmed via Diagnostics Phase-C: `Bash×9` at 46s the
hot tool, ticks of 40–90 mostly-tiny `python3 -c`/`grep`/`ls`/`cat` probes.

### Why a NEW mechanism (prompt discipline already failed)

stoneforge's `tick.txt` **already** carries a strong efficiency directive — one-shot inspect, pipe big
output to a file + grep, codegraph-not-grep, delegate bulk work, don't grind past 3 failures. The
research confirms a "Context Rules" system-prompt block is worth ~15–25% — but **we already have it and
ticks still hit 76 turns / 5.6M.** *Requesting* discipline isn't holding; the lever is **enforcement**
(a hook that gates/rewrites the call) and a **hard structural cap** (fewer turns). That's this doc.

## The two levers

This is a two-part fix. They compound; ship both.

1. **Shrink each tool output at the boundary** (this doc, §A) — deterministic, $0, no LLM. Distilled
   from rtk-ai/rtk (vetted 2026-06-26, LEARN-FROM verdict — we author our own; see
   `knowledge/external-tools.md`). rtk proves ~89% noise removal on shell output with pure rules.
2. **Let Claude self-manage within the tick** (§B) — smaller ticks, mid-tick compaction if available,
   and prompt discipline. Grounded by the headless-context research (`reports/` companion).

---

## §A. Deterministic output compactor

### A.1 The rules engine (steal rtk's schema verbatim)

A per-tool rules table; each rule = a regex matcher + an ordered list of strategies applied to the
raw tool output before it enters context:

- **strip** — drop ANSI codes, blank-line runs, known-noise lines (progress bars, `Downloading…`).
- **dedup** — collapse runs of near-identical lines into one + `(×N)` (test/lint/build floods).
- **group** — aggregate similar items under a header (files-by-dir, errors-by-type).
- **truncate** — keep head + tail with an elision marker `… (N lines elided, full output at <path>) …`;
  always spill the **full** output to a file so nothing is lost, only moved out of context.

Config format (rtk's TOML schema, adopted as-is):

```toml
[cargo_test]
match_command = "^(cargo|python -m pytest|npm test|node .*eval)"
strip_ansi = true
dedup = true
max_lines = 120          # head+tail budget; rest → spill file
spill = true             # write full output to state/.compact/<hash>.txt and reference it
```

### A.2 Integration mechanism — RESOLVED

**`PostToolUse` cannot rewrite a tool result** (Claude Code docs: it observes + can add
`additionalContext`, but does not replace the result that already entered context). So a "universal
output filter" is **not** available. The compactor is a **`PreToolUse` shaper**, in two tiers:

- **Tier 1 — context-guard (guaranteed; same mechanism as `delegation_guard`/`build_guard`):** a
  `PreToolUse` hook that **denies + steers** context-bombing calls. PreToolUse deny-with-reason is a
  rock-solid CLI capability. Patterns to gate: `cat`/`Read` of a file > N KB, un-piped `find`/`grep`/
  `ls -R`, a `python3 -c` dumping a whole sim/JSON. The deny message names the fix ("file is 210 KB —
  pipe to a file + grep, or Read with offset/limit"). This **enforces** what `tick.txt` only requests.
- **Tier 2 — Bash-output rewrite (rtk's exact model):** for Bash specifically, rewrite the command to
  run through the deterministic compactor (`<cmd> | sf-compact --rules cargo`) so the output is already
  compacted when it returns. Mechanism = PreToolUse `updatedInput` (modify the command string) where
  the CLI version supports it; otherwise expose `sf-compact` as a wrapper the agent is told to use and
  Tier-1 gates the un-wrapped form. Built-in `Read`/`Grep`/`Glob` can take an injected `limit`/
  `offset`/`head_limit`, but blunt auto-truncation risks cutting needed content — prefer Tier-1 gating
  + the prompt discipline already in `tick.txt` for those.

Net: **Bash is the big win** (it's the hot tool — `Bash×9`/tick) and gets true compaction; Read/Grep
get gated + disciplined. Verify `updatedInput` support on our pinned Claude Code version during build;
if absent, Tier-1 (deny+steer) + the `sf-compact` wrapper still delivers most of the saving.

### A.3 Where it lives

- `platform/agentd/hooks/compactor.py` — the hook (Pre or Post per A.2), reads `policies/compact.toml`.
- `policies/compact.toml` — the default rules (ship a conservative starter set: pytest, cargo, npm,
  git status, find, grep, ls -R; agents extend per-repo).
- Spill dir `state/.compact/` — full outputs, gitignored, rotated like `usage.jsonl`.
- Wire into the templates' `.claude/settings.json` + `bin/enclave` default-writer (same pattern as
  `delegation_guard`). Default **report-only** first (log would-compact bytes to
  `state/compact.log`, don't actually trim) → measure → flip to enforce per-agent via
  `COMPACT_ENFORCE=1`, exactly like the egress allowlist rollout.

### A.4 Safety / guardrails

- **Never lose data** — full output always spills to a referenced file; compaction only changes what's
  *in context*, never what's *on disk*.
- **Fail-open** — any hook error returns the raw output unchanged; a compactor bug must never wedge a
  tick (same rule as every other hook here).
- **Idempotent + cheap** — pure-Python regex, no network, no LLM, sub-ms; it must not add latency.
- **Honest** — when output is truncated, the elision marker states how much was cut and where the full
  copy is, so the agent (and a human reading the transcript) knows nothing was silently dropped.

---

## §B. Claude self-management within a tick

Findings from the headless-context research (official Anthropic docs; sources at bottom). What's real
for our `claude -p` path vs what's interactive/SDK-only:

### B1. Auto-compaction — already protecting us (no action)
`claude -p` **does** auto-compact headless (default; fires when the window crosses ~83.5%, reserving
~33K). This is **why no single tick's window blows past ~200K** even though `cache_read` sums to 5.6M —
it caps the *window*, not the *per-turn re-read*. Env `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100) only
shifts *when* it fires within the fixed buffer — **minor, skip.** Auto-compaction doesn't solve our
cost (turns × window); it just stops the window from being unbounded.

### B2. `/clear` + `/compact` — interactive-only; we already get the effect
No headless equivalent. But **each tick is already a fresh `claude -p` session** = a structural
`/clear` between ticks. Confirmed correct by the research; nothing to add. The within-tick lever is NOT
`/clear` — it's §A (smaller window) + B3 (fewer turns).

### B3. Smaller ticks (`MAX_TURNS`) — a real lever, higher-leverage than generic advice credits
Cost ∝ turns (table above), so a hard turn cap directly bounds the worst-case tick. stoneforge=80; the
$2.67 outlier was 76 turns. **Recommend 80→~40** as a structural guard: a tick wraps up and `continue`
picks the work up next tick. Tradeoff: each extra tick re-pays the fixed cost (CLAUDE.md + recall +
first reads, ~$0.3–0.5), so don't go too low. ~40 caps the tail without much fixed-cost churn.
Reversible knob in `agent.env`.

### B4. Mid-tick tool-result clearing — the "right" tool, but NOT on our path
The Anthropic **API** has context-editing (`clear_tool_uses_20250919`: drop old tool results, keep last
N) and the **Agent SDK** has configurable `compaction_control` (threshold + keep-count + custom summary
prompt; a cookbook shows 204K→82K, −58%). **Neither is exposed by the `claude -p` CLI** — both are
API/SDK-only. Our compactor (§A) is the **CLI-layer equivalent** (shrink at the boundary instead of
clearing after the fact). Moving the runtime to the SDK to get real `clear_tool_uses` is a **large
architectural change** against the deliberate "no SDK, no broker" design in `CONTEXT-AND-TICKS.md` —
**note as a future option, do not pursue now.**

### B5. `--resume`/`--continue` — stays BANNED (unchanged)
Helps within a live session but re-bills a growing transcript across ticks (the 136M-token burn).
`CONTEXT-AND-TICKS.md` already forbids it; the research independently confirms fresh ticks are optimal.

### B6. Prompt discipline — already deployed; tune, don't re-add
The evidence-based wins (don't re-read; pipe >1KB to a file + grep; batch independent calls; targeted
offset/limit reads; codegraph-not-grep; delegate bulk) are **already in `tick.txt`.** The research
values a Context-Rules block at ~15–25% — but since ours is present and ticks still bloat, the marginal
gain from *more* prompt text is low. Keep it lean; rely on §A enforcement instead. One cheap tweak: a
crisp 4-line "CONTEXT RULES" block near the TOP of `tick.txt` (it's currently mid-file) so it's read first.

### B7. Skills / plugins / MCP — none worth adopting; two free wins
- **No pre-built context-compaction skill/plugin exists** — the research is clear. Our §A compactor is
  the move (and rtk confirmed LEARN-FROM, not adopt).
- **MCP tool-search is already deferred** (schemas load on use) — free, on, nothing to do.
- **Subagents get a fresh isolated window** — heavy sub-work delegated to a subagent (or our off-Opus
  worker via `route.mjs`, already mandated) keeps its output OUT of the main tick's transcript. This is
  the same lever as our delegation layer; reinforces "delegate bulk labor."
- **Disable unused skills' model-invocation** (`disable-model-invocation: true` in SKILL.md) so their
  descriptions don't sit in context — a small, free trim worth a pass.

---

## Prioritized plan (highest leverage first)

1. **`MAX_TURNS` 80→40 on stoneforge** — 1-line `agent.env` change, live next tick, reversible, zero
   code. Directly caps the cost ∝ turns tail. **Do this first; it's free and measurable immediately.**
2. **PreToolUse context-guard (Tier 1)** — `compactor.py` deny+steer for context-bombing calls, wired
   like `delegation_guard`. **Report-only first** (log would-deny to `state/compact.log`), size the
   saving for one tick, then `COMPACT_ENFORCE=1`. Host-mounted hook → live next tick, no rebuild.
3. **Bash-output compactor (Tier 2)** — `sf-compact` wrapper + `compact.toml` rules (rtk schema);
   gate un-wrapped verbose Bash via the Tier-1 hook. The biggest structural win (Bash is the hot tool).
4. **Move `tick.txt` CONTEXT RULES to the top** (B6) — trivial, do alongside #1.
5. **Bake into the product** — templates + `bin/enclave` default-writer, default report-only
   (conservative), operator publishes the image. Other agents inherit on rebuild.

Verification at each step = the Diagnostics context chart + the monitor `context_bloat` alert dropping,
and `cache_read`/tick in `usage.jsonl` falling.

## Open questions

- **`updatedInput` support** on our pinned Claude Code version (Tier-2 auto-rewrite vs wrapper+gate).
- Break-even `MAX_TURNS` for stoneforge — start at 40, watch whether work fragments badly across ticks.
- Does compaction ever hide something the agent needed mid-reasoning? Mitigated by spill-file refs +
  conservative defaults + report-only burn-in.
- Whether to ever move the runtime to the Agent SDK for real `clear_tool_uses` (§B4) — deferred; large
  change vs the current no-SDK design.

## Sources

rtk eval + LEARN-FROM verdict: `knowledge/external-tools.md`. Headless-context research (auto-compaction,
context-editing API, SDK `compaction_control`, hooks): Anthropic docs — Compaction, Context editing,
Agent SDK overview, How Claude Code Works (context window). Companion to `CONTEXT-AND-TICKS.md`.
