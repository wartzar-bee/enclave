# Delegation — manager coordinates, local workers do the labor

**Status:** SPEC (pre-build), 2026-06-25. Distilled (not ported) from Hermes `delegate_tool` +
`verification_stop`, NVIDIA NemoClaw's per-agent model-pinning + spawn-allowlist, OpenJarvis's
router-policy. Adopts NONE of those repos — they failed our install-vetting bar; this is our own
implementation of the convergent pattern.

## Problem
With `BRAIN=claude`, the manager (Claude) is a capable model, so a *prompt* telling it to "offload bulk
work to local LLMs" is ignored — it just does the work itself (proven: a stoneforge tick ran on Opus,
0 delegations, hand-wrote a whole math model). `route.mjs` only does **one-shot completions** (no tools,
no file I/O), so it can draft text but cannot "implement this module" (which needs read→write→run→iterate).
Result: no mechanical way to make "Claude manages, local does the labor" true.

## Goal
A **delegation primitive** the manager invokes to hand a whole SUBTASK to an isolated **local worker**
that does the labor with its own tools and returns **only a summary**. Plus a **guard** that *forces*
the manager to delegate bulk work instead of doing it itself. Net: Claude spends tokens on planning +
delegation prompts + review; local models do codegen/sims/drafts; a verify-gate keeps quality honest.
The "only summary returns" rule also directly bounds manager token growth (our 136M-token-burn lesson).

## Architecture (reuses what exists)
```
manager (Claude, BRAIN=claude)
  │  plans the tick, picks ONE highest-value subtask
  │  calls ▼ (via Bash)
  ▼
delegate.py  ── selects worker model by --kind (policy.json pools) ──▶ local_agent.py (WORKER_MODE)
  │                                                                      │ ReAct loop on a LOCAL model
  │                                                                      │ tools: bash/read/write/edit/glob/grep/qmd
  │                                                                      │ guard: build_guard.py (unchanged)
  │  ◀── runs --verify cmd; retries worker on failure (bounded) ────────┘ writes files itself, in the repo
  ▼
returns JSON summary (status, files_changed, verify result, model, steps, tokens) — NOT the worker's steps
  │
  ▼
manager reviews summary, makes only SMALL integrating edits, decides next step, commits
```
- **Worker engine = `local_agent.py`** (already a guarded ReAct loop). delegate.py configures + invokes it.
- **Model selection = `route.mjs`/`policy.json` pools** (already local-first): `code`→mlx coder,
  `write|analyze`→mlx/ollama, `classify|summarize`→ollama fast.
- **Verify-gate** = our existing pattern (supervisor.py), applied per-delegation.

## Component 1 — `platform/agentd/delegate.py` (new)
CLI the manager calls. Synchronous, single worker (v1).
```
python3 /workspace/platform/agentd/delegate.py \
  --task "<precise subtask instruction — what to build + acceptance criteria>" \
  --kind code|write|analyze|classify        # → worker model pool (default: code) \
  [--context-files a.py,b.md]               # worker reads these first \
  [--verify "<shell cmd that MUST exit 0>"] # the gate (e.g. the eval/test cmd) \
  [--verify-retries 2] [--max-steps 20] [--timeout 600] \
  [--cwd /agent/work/<repo>]                # worker's working dir
```
**stdout = JSON only** (this is all the manager sees):
```json
{
  "status": "ok | verify_failed | incomplete | error",
  "summary": "<2-6 lines: what the worker did>",
  "files_changed": ["eval/models/foo.py"],
  "verify": {"cmd": "...", "passed": true, "tail": "<last ~20 lines>"},
  "model": "<worker model id>", "kind": "code",
  "steps": 9, "tokens": 41233, "cost_usd": 0.0,
  "worker_log": "/agent/state/delegations/<id>.jsonl"   // full trace on disk, NOT in stdout
}
```
Behaviour:
1. Resolve worker model from `--kind` via policy.json (env override `DELEGATE_MODEL_<KIND>`).
2. Pre-warm the model (one tiny call) so cold-load doesn't eat the step budget.
3. Run `local_agent.py` in `WORKER_MODE=1`: tools = bash/read/write/edit/glob/grep/qmd; **blocked**:
   `escalate` (workers never call frontier), `delegate` (no recursion), git/commit (build_guard already
   blocks). Caps: `--max-steps`, `--timeout`. Worker writes files itself in `--cwd`.
4. If `--verify` given: run it. On non-zero, re-invoke the worker with the failure tail appended, up to
   `--verify-retries`. Honest `status`: `ok` only if verify passed (or no verify given and worker
   reported done); else `verify_failed` / `incomplete`.
5. Write the full worker trace to `state/delegations/<id>.jsonl`; emit ONLY the JSON summary to stdout.
6. Log one line to `state/delegations.log` (id, kind, model, steps, tokens, verify pass/fail).

## Component 2 — `delegation_guard.py` PreToolUse hook (new) — the enforcement
Runs on the **manager's** Claude Code session (added to the agent's `.claude/settings.json` PreToolUse,
alongside build_guard). Makes delegation mechanical, not optional.
- **Triggers on `Write` and `Edit`.**
- **BLOCK** when: a `Write` whose `content` > `DELEGATION_MAX_CHARS` (default 800), OR an `Edit` whose
  `new_string` > that and introduces substantial new logic, **targeting a code/content file under the
  work repo** (`*.py,*.ts,*.js,*.svelte,*.mjs,*.css,*.html,*.json` under `--cwd`). Reason returned to
  the model:
  > "Bulk implementation must be DELEGATED. Run `delegate.py --task '…' --kind code --verify '…'` — the
  > local worker writes the file; you plan, read its summary, and make only small integrating edits.
  > (limit: >800 chars of new content). If a delegation already returned `verify_failed` for this exact
  > file this tick, you may write it yourself — include `[delegation-fallback]` in the tool call."
- **ALLOW** always: Read, Bash (incl. `delegate.py` and `route.mjs`), Grep/Glob, qmd; small edits
  (≤ threshold); any write to plan/state/docs (`state/**`, `*.md` rollups, `work.json`, `docs/**`);
  and writes tagged `[delegation-fallback]` **iff** `state/delegations.log` shows a failed delegation for
  that file this tick (escape hatch so a genuinely-stuck worker never wedges the manager).
- **Config:** `DELEGATION_ENFORCE=on|off` (default on for BRAIN=claude; off otherwise),
  `DELEGATION_MAX_CHARS`, path allow/deny globs — in `agent.env`.

## Component 3 — wiring
- `tick.txt` already carries the manager mandate (plan → delegate → review). Keep; the guard backs it.
- `local_agent.py`: add `WORKER_MODE` (tool allowlist + block escalate/delegate; `finish` permitted only
  after the caller's verify, enforced by delegate.py not the worker).
- `.claude/settings.json`: add `delegation_guard.py` to PreToolUse (shared image → reaches all agents;
  gated by `DELEGATION_ENFORCE`).
- Relationship to `supervisor.py`: same worker engine + verify concept; supervisor = the OFF-Opus
  autonomous planner (BRAIN=local), delegate.py = the Claude-manager's dispatch tool (BRAIN=claude).
  No conflict; they share `local_agent.py`.

## Non-goals (v1)
Parallel/async fan-out (Hermes `async_delegation` — add later), learned/trained routing (OpenJarvis SFT/GRPO),
declarative multi-worker manifest (NemoClaw `agents.yaml` — add when >1 worker type), mixture-of-agents.
v1 = one synchronous delegate + verify + guard enforcement. Keep it lean.

## Proof plan (how we verify it works — no claims without this)
1. **Unit:** `delegate.py --task "write prime_list(n) to /tmp/p.py" --kind code --verify "python3 -c 'import sys;sys.path.insert(0,\"/tmp\");import p;assert p.prime_list(5)==[2,3,5,7,11]'"` → expect `status:ok`, file written by a LOCAL model, verify passed, manager (caller) tokens ≈ 0.
2. **Guard:** manager attempts a 2 KB Write to a `.py` under the repo → BLOCKED with the delegate
   instruction; a 200-char edit → allowed; a `state/rollup.md` write → allowed.
3. **End-to-end tick:** run a stoneforge `BRAIN=claude` tick → assert `state/delegations.log` shows ≥1
   delegation, the bulk file was written by the worker (local model in its log), and the manager only
   planned/reviewed. Compare manager Claude tokens vs the all-Opus baseline tick (expect a large drop).

**Success =** delegations ≥1 per substantive tick · bulk files authored by local workers · verify-gate
passing · manager token use materially down. **Failure modes** are honest: `verify_failed` returns to the
manager, which may escalate the worker model, use the fallback escape hatch, or escalate to the operator.

## Files
- NEW `platform/agentd/delegate.py`
- NEW `platform/agentd/hooks/delegation_guard.py`
- EDIT `platform/agentd/local_agent.py` (WORKER_MODE)
- EDIT templates' `.claude/settings.json` (PreToolUse += delegation_guard, gated)
- DOC this file
All in the enclave product (shared image) → one rebuild reaches every agent.
