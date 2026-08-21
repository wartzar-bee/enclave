# Within-tick context compactor — scope

**Status:** Tier-1 SHIPPED (`hooks/compactor.py` PreToolUse deny+steer, report/enforce).
**Tier-2 SHIPPED 2026-08-21 as `COMPACT_MODE=spill`** — the `updatedInput` rewrite of A.2, default
OFF and unmeasured (see §A.5). The rtk rules engine / `policies/compact.toml` half of Tier-2 is still
unbuilt — treat that part of §A as backlog. Sibling to [`CONTEXT-AND-TICKS.md`](CONTEXT-AND-TICKS.md) (which covers
*between-tick* fixed cost); this covers the gap that doc leaves open: **within-tick bloat**, the #1 live
cost driver on forgepod today.

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

forgepod's `tick.txt` **already** carries a strong efficiency directive — one-shot inspect, pipe big
output to a file + grep, codegraph-not-grep, delegate bulk work, don't grind past 3 failures. Research
values a "Context Rules" system-prompt block at ~15–25% — but **we have it and ticks still hit 76 turns /
5.6M.** *Requesting* discipline isn't holding; the lever is **enforcement** (a hook that gates/rewrites
the call) plus a **hard structural cap** (fewer turns).

## The two levers

Two-part fix; they compound, ship both.

1. **Shrink each tool output at the boundary** (§A) — deterministic, $0, no LLM. Distilled from
   rtk-ai/rtk (vetted 2026-06-26, LEARN-FROM — we author our own; `knowledge/external-tools.md`). rtk
   proves ~89% noise removal on shell output with pure rules.
2. **Let Claude self-manage within the tick** (§B) — smaller ticks, mid-tick compaction if available,
   prompt discipline. Grounded by the headless-context research (`reports/` companion).

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

**`PostToolUse` cannot rewrite a tool result** (it observes + can add `additionalContext`, but does not
replace the result that already entered context). So a "universal output filter" is **not** available.
The compactor is a **`PreToolUse` shaper**, in two tiers:

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

Net: **Bash is the big win** (the hot tool — `Bash×9`/tick) and gets true compaction; Read/Grep get
gated + disciplined.

**`updatedInput` support: VERIFIED 2026-08-21.** Present in the pinned pod CLI (220 occurrences in
`claude` **2.1.170** inside the live `wartzar-bee` pod) and on the host (2.1.238). Documented shape:
`{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","updatedInput":{…}}}`
— `updatedInput` applies with or without a `permissionDecision`; we state `allow` so no later
ask-rule re-prompts on our own rewrite. The `sf-compact` fallback is therefore unnecessary.

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
### A.5 `COMPACT_MODE=spill` — what shipped (2026-08-21)

Three modes now, selected by `COMPACT_MODE` (`COMPACT_ENFORCE=1` remains the legacy spelling of
`enforce`, and `console.py` still emits it):

| mode | on a context-bombing call |
|---|---|
| `report` (default) | log to `state/compact.log`, allow |
| `enforce` | exit 2 + steer; the agent must retry in a leaner form |
| `spill` | **rewrite and run it once** — full output to `state/.compact/<ts>-<hash>.txt`, a bounded preview back to the model, plus the locator |

Bash rewrite (newline-separated so a trailing `#comment` cannot swallow the closing brace;
`(exit $__rc)` re-raises the original status without exiting the tool's shell):

```
{
<original command>
} > state/.compact/<id>.txt 2>&1
__rc=$?; __sz=$(wc -c < …); head -c $COMPACT_PREVIEW_BYTES …
echo "[compactor] <reason>: full output ($__sz bytes) is in <path>; the preview above is only its
first N bytes. Nothing was lost — grep/sed -n/pyexec.py THAT FILE for the rest, do not cat it."
(exit $__rc)
```

A large no-limit `Read` instead gets an injected `limit` (`COMPACT_READ_LIMIT`, default 400) — the
file is already its own locator, so nothing is copied. A call with no safe rewrite (e.g. a
backgrounded `… &`, where redirecting would change its semantics) **falls back to `enforce`, never to
`report`**: spill is stricter than report, never looser.

**Why this shape.** A refusal costs a whole turn *and* depends on the agent complying; a rewrite costs
nothing and cannot be ignored. Adopted from DeepSeek Harness's `spill-policy` — which places the same
idea *post*-execute, impossible here (A.2) — as the one mechanism worth taking from that harness
(`ENCLAVE-DEEPSEEK-HARNESS-EVAL-2026-08-21.md`). The consumer already exists: a spill file is exactly
what `pyexec.py` wants.

**Measured 2026-08-21 — see §A.6.** 40.6% less context than no hook on the replayed Bash gates, and
the mode comparison came out decisively for spill: 75% of what this hook gates was never a context
bomb, so `enforce` would have burned 161 wasted turns to save nothing. Default still `report` until
the hook is wired on more than one agent.

Spill files are pruned by age on every spill (`COMPACT_SPILL_TTL_DAYS`, default 7, `0` disables).
They are unbounded by construction — one `cat` of a huge log writes the whole thing — and these pods
run for days, so without retention the context win is simply paid for in disk.

Env: `COMPACT_MODE`, `COMPACT_PREVIEW_BYTES` (4096), `COMPACT_READ_LIMIT` (400),
`COMPACT_MAX_READ_BYTES` (65536), `COMPACT_SPILL_TTL_DAYS` (7). Tests: `hooks/test_compactor.py` (45 checks, including executing
the rewritten command in a real shell and asserting the full output landed on disk).

### A.6 MEASURED, 2026-08-21 — and the Read branch was measuring the wrong thing

First real measurement of this hook since it shipped. Substrate: `stoneforge`'s `state/compact.log`
— **2,568 gates over 20 days** (2026-06-26 → 08-21). At the time of measurement stoneforge was the
only running agent with the hook in its `.claude/settings.json`; `financial-advisor` and
`wartzar-bee` had neither the wiring nor a `compact.log`. (That is no longer true — see "What this
changes": the hook is now wired by construction.)

**Finding 1 — 87% of gates are `Read`, and 99.5% of those are images.** 2,241 Read gates; 2,230 were
`.png`/`.jpg`; **11 were text**. The Read branch gates on *file bytes*, which is a valid proxy for
text and a meaningless one for a vision read: a 587 KB PNG (the median gated file) costs ~1–2k
tokens, not 587 KB of tokens. The "1,604 MB of context avoided" that falls out of summing those file
sizes is not a real number.

**Finding 2 — when enforce was briefly on (2026-06-26..28), it blocked 172 image reads.** On an art
agent. The hook was stopping stoneforge from looking at its own QA renders (`wildlands.png`,
`skull-reels-verify.png`, `emberfall-boot.png`, …). 173 of the 222 enforce-mode gates were Reads and
172 of those were images. **Fixed:** `VISUAL_EXT` is now exempt from gating *and* from reshaping —
injecting a line `limit` into an image Read would have been worse than the block.

**Finding 3 — the Bash gate is right but far too eager, which is the case FOR spill.** 215 of the 327
gated Bash commands were untruncated and safe to replay; re-run inside the pod, all 215 measured:

| | bytes |
|---|---|
| raw, no hook | 755,044 |
| under `spill` (4096 preview + ~190 B marker) | 448,341 — **40.6% less context** |
| under `enforce` | 0 bytes of output, and **215 refused turns** |

Real output: p50 **1,186 B**, mean 3,511 B, p90 7,486 B, max 54,294 B. **Only 54 of 215 (25%) ever
exceeded the 4096-byte preview** — i.e. three quarters of what this hook calls a "context bomb"
isn't one. That 75% is exactly where the two modes diverge: `spill` costs those calls ~190 bytes of
marker each (30,590 B total, 4.1% of raw); `enforce` costs them **161 wasted turns**. A wrong spill
is nearly free; a wrong refusal is a whole turn plus the agent's compliance. p90 = 7,486 B also says
4096 is a well-placed preview — it bounds the tail without shredding the ordinary call.

**Honest caveats.** One agent, one workload. Replay is not the original moment — the repo moved
under these commands over 8 weeks, so 6 returned zero bytes and the rest are approximations of what
they would have produced then. 112 of 327 Bash gates were excluded (truncated at the log's 300-char
`detail` cap, or not read-only enough to replay). No live A/B of turn counts exists, because
`enforce` has not run anywhere since June.

**What this changes.**
1. The Read branch's aim is fixed (`VISUAL_EXT`).
2. **The hook is now wired by construction**, the same fix `event_log` needed after the 27.5h
   blind-fleet incident: added to all three templates, to the `bin/enclave` default settings writer,
   and to `settings_migrate.ADD_HOOKS` so every already-deployed pod self-heals at tick boot. Only a
   hook whose default is inert belongs in `ADD_HOOKS`, and `report` is inert. Note the deployment
   path is live: pods mount the framework read-only and re-run `settings_migrate.py` every tick
   boot, so this reached `financial-advisor` within one tick of the file being saved.
3. `console.py`'s `context_explosion` / `prompt_creep` remediations now set `COMPACT_MODE=spill`
   rather than `COMPACT_ENFORCE=1` — on this evidence, enforce is the wrong remedy.

`report` stays the default mode. Promote `spill` to default only after the wider wiring has produced
`compact.log` data from more than one workload.

Artifacts: `scratchpad/spill-measure/` (`compact.log`, `replay.txt`, `replay-results.jsonl`,
`analysis1-4.txt`).

---

## §B. Claude self-management within a tick

Findings from the headless-context research (Anthropic docs; sources at bottom). What's real for our
`claude -p` path vs interactive/SDK-only:

### B1. Auto-compaction — already protecting us (no action)
`claude -p` **does** auto-compact headless (default; fires when the window crosses ~83.5%, reserving
~33K). This is **why no single tick's window blows past ~200K** even though `cache_read` sums to 5.6M —
it caps the *window*, not the *per-turn re-read*. Env `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` (1–100) only
shifts *when* it fires within the fixed buffer — **minor, skip.**

### B2. `/clear` + `/compact` — interactive-only; we already get the effect
No headless equivalent. But **each tick is already a fresh `claude -p` session** = a structural `/clear`
between ticks. The within-tick lever is NOT `/clear` — it's §A (smaller window) + B3 (fewer turns).

### B3. Smaller ticks (`MAX_TURNS`) — a real lever, higher-leverage than generic advice credits
Cost ∝ turns (table above), so a hard turn cap directly bounds the worst-case tick. forgepod=80; the
$2.67 outlier was 76 turns. **Recommend 80→~40** as a structural guard: a tick wraps up and `continue`
picks the work up next tick. Tradeoff: each extra tick re-pays the fixed cost (CLAUDE.md + recall +
first reads, ~$0.3–0.5), so don't go too low. ~40 caps the tail without much fixed-cost churn.
Reversible knob in `agent.env`.

### B4. Mid-tick tool-result clearing — the "right" tool, but NOT on our path
The Anthropic **API** has context-editing (`clear_tool_uses_20250919`: drop old tool results, keep last
N) and the **Agent SDK** has configurable `compaction_control` (threshold + keep-count + custom summary
prompt; a cookbook shows 204K→82K, −58%). **Neither is exposed by the `claude -p` CLI.** Our compactor
(§A) is the **CLI-layer equivalent** (shrink at the boundary instead of clearing after the fact). Moving
the runtime to the SDK for real `clear_tool_uses` is a **large architectural change** against the
deliberate "no SDK, no broker" design in `CONTEXT-AND-TICKS.md` — **future option, do not pursue now.**

### B5. `--resume`/`--continue` — stays BANNED (unchanged)
Helps within a live session but re-bills a growing transcript across ticks (the 136M-token burn).
`CONTEXT-AND-TICKS.md` already forbids it; research confirms fresh ticks are optimal.

### B6. Prompt discipline — already deployed; tune, don't re-add
The evidence-based wins (don't re-read; pipe >1KB to a file + grep; batch independent calls; targeted
offset/limit reads; codegraph-not-grep; delegate bulk) are **already in `tick.txt`.** Since it's present
and ticks still bloat, the marginal gain from *more* prompt text is low — keep it lean, rely on §A
enforcement. One cheap tweak: move a crisp 4-line "CONTEXT RULES" block to the TOP of `tick.txt` (it's
currently mid-file) so it's read first.

### B7. Skills / plugins / MCP — none worth adopting; two free wins
- **No pre-built context-compaction skill/plugin exists.** Our §A compactor is the move (rtk = LEARN-FROM,
  not adopt).
- **MCP tool-search is already deferred** (schemas load on use) — free, on, nothing to do.
- **Subagents get a fresh isolated window** — heavy sub-work delegated to a subagent (or our off-Opus
  worker via `route.mjs`, already mandated) keeps its output OUT of the main tick's transcript. Same lever
  as our delegation layer.
- **Disable unused skills' model-invocation** (`disable-model-invocation: true` in SKILL.md) so their
  descriptions don't sit in context — a small, free trim worth a pass.

---

## Prioritized plan (highest leverage first)

1. **`MAX_TURNS` 80→40 on forgepod** — 1-line `agent.env` change, live next tick, reversible, zero
   code. Directly caps the cost ∝ turns tail. **Do this first; it's free and measurable immediately.**
2. **PreToolUse context-guard (Tier 1)** — `compactor.py` deny+steer for context-bombing calls, wired
   like `delegation_guard`. **Report-only first** (log would-deny to `state/compact.log`), size the
   saving for one tick, then `COMPACT_ENFORCE=1`. Host-mounted hook → live next tick, no rebuild.
3. ~~**Bash-output compactor (Tier 2)** — `sf-compact` wrapper + `compact.toml` rules (rtk schema)~~
   → **superseded and SHIPPED as `COMPACT_MODE=spill`** (§A.5): `updatedInput` turned out to be
   available on the pinned CLI, so the wrapper the agent had to remember to use was never needed.
   Remaining Tier-2 backlog: the per-tool `compact.toml` rules engine (spill is content-agnostic).
4. **Move `tick.txt` CONTEXT RULES to the top** (B6) — trivial, do alongside #1.
5. **Bake into the product** — templates + `bin/enclave` default-writer, default report-only
   (conservative), operator publishes the image. Other agents inherit on rebuild.

Verification at each step = the Diagnostics context chart + the monitor `context_bloat` alert dropping,
and `cache_read`/tick in `usage.jsonl` falling.

## Open questions

- **`updatedInput` support** on our pinned Claude Code version (Tier-2 auto-rewrite vs wrapper+gate).
- Break-even `MAX_TURNS` for forgepod — start at 40, watch whether work fragments badly across ticks.
- Does compaction ever hide something the agent needed mid-reasoning? Mitigated by spill-file refs +
  conservative defaults + report-only burn-in.
- Whether to ever move the runtime to the Agent SDK for real `clear_tool_uses` (§B4) — deferred; large
  change vs the current no-SDK design.

## Sources

rtk eval + LEARN-FROM verdict: `knowledge/external-tools.md`. Headless-context research (auto-compaction,
context-editing API, SDK `compaction_control`, hooks): Anthropic docs — Compaction, Context editing,
Agent SDK overview, How Claude Code Works (context window). Companion to `CONTEXT-AND-TICKS.md`.
