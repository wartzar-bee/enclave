# Changelog

All notable changes to Enclave. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/). Pre-1.0 means the layout and env-var names can still
move between minor versions — pin a tag if that matters to you.

## [Unreleased]

## [0.3.0] — 2026-07-31

### Added
- **`enclave eval` — endpoint-based model-eval primitive** — benchmark models on any OpenAI-compatible
  pool endpoint (local MLX, ollama, NVIDIA, OpenRouter — host or in-container) with
  `enclave eval <capability|gsm8k> --models … (--pool NAME | --base URL)`. Per-model sampling params come
  from the catalog (`catalog.model_params`), so a single global temperature can't silently invalidate a
  comparison; a model with no documented entry is tagged `params_source:"default"` so an undocumented run
  is never mistaken for a documented one. `--record` appends each model's summary to the catalog evidence
  trail (`catalog.record_eval`, capped at 10) so routing picks can cite their eval. (`0e57333`)
- **Editable console catalog** — models, providers and presets are managed live from the dashboard
  (Models tab, `/api/catalog`) instead of being hardcoded. (`d2d7b83`)
- **Config drift badge** — the config tab surfaces keys where the on-disk config files disagree with the
  running container's env, so silent compose/file drift is visible at a glance. (`483c4f9`)

### Fixed
- **Credential redaction on auto-capture (security)** — auto-captured activity/decisions now route
  through the shared secrets redactor, so a credential can't reach tracked memory via the framework's own
  writers (previously only the fail-closed vault scan caught it). Follow-up loads `secrets.py` by file
  path to fix a CI-red `AttributeError`. (`cf40220`, `18c0887`)
- **Key-material labels in the secret scan (security)** — `SEED` / `SEED_HEX` / `PRIV_KEY` /
  `SIGNING_KEY` are now caught by scan + redact; pure-hex seeds previously slipped the entropy
  catch-all. (`94c9bfb`)
- **Boot re-syncs hooks** — the runtime re-syncs `/agent/.claude/hooks` from the mounted framework at
  boot, so a shipped hook fix reaches existing pods instead of staying dead after first init. (`62cf17f`)
- **Epoch-aware config + diagnostics gates** — the `warm_session` preflight sanctions epoch-bounded warm
  sessions (`WARM_SESSION=auto` + `EPOCH_TICKS>=1`), and the `context_explosion` / `prompt_creep`
  diagnostics account for context climbing toward the epoch boundary — no more false preflight/diagnostic
  escalations under adaptive ticks. (`207cd7b`, `32a56da`)
- **Wake-gate-aware liveness** — the monitor no longer restarts a healthy parked pod, the agentloop bumps
  `.heartbeat` on a gated wake-skip so alive-idle pods aren't flagged overdue, and `_has_open_work` reads
  both work.json shapes (list + dict) so dict-shaped pods aren't starved of ticks. (`f3ae30b`, `4b31b6e`,
  `125250a`)

## [0.2.0] — 2026-07-28
Second minor since the public 0.1.0. Folds in one session of framework work: adaptive (epoch-driven)
ticks, a typed-envelope handoff protocol, model-agnostic (BYOM) docs + an examples gallery, the
owner-charter template re-charter, and a batch of correctness fixes. Pre-1.0: env-var names and layout
can still move between minors — pin a tag if that matters.

### Added
- **Adaptive ticks (context epochs)** — the runtime now groups work into context *epochs* bounded by
  `EPOCH_TICKS` / `CTX_EPOCH_TOKENS` / `EPOCH_MAX_INCREMENTS` / `EPOCH_WALL_SEC`, with a heartbeat wake
  gate (`WAKE_GATE`, `WAKE_MAX_STALENESS_SEC` default 6h) and `WARM_SESSION=auto`, so a pod wakes on
  real change rather than a fixed clock while still guaranteeing a tick at least every staleness
  ceiling. Per-increment `usage.jsonl` now records `inc`, `cost_usd` (delta) and `cost_epoch_usd`
  (cumulative). Documented in `docs/CONTEXT-AND-TICKS.md`. (`92e5c43`)
- **Typed-envelope handoff protocol** — `platform/agentd/handoff.py emit --to <pod|studio|operator>
  --type <distribution-help|maintainer-queue|release|...> --title … --payload '{…}'` writes one typed
  envelope to `state/outbox/`; the off-Opus handoff-broker dispatches on `type` (routing types
  auto-deliver to the recipient's inbox and return a recipe). Replaces bespoke `state/*-queue.md`
  filenames as the canonical PREPARE→FIRES channel; documented in `AGENT-RULES.md` §4 and the
  autonomous/venture `CLAUDE.md` templates. (`8c24eeb`)
- **Bring-your-own-model guide** — `docs/BRING-YOUR-OWN-MODEL.md` (run enclave on any non-Claude LLM)
  with a live-verified NVIDIA NIM example and a `curl /v1/models` line so readers pick a currently-live
  model id, plus a README pointer. (`ad99fc4`)
- **Owner-charter template** — the autonomous/venture agent templates re-charter the top agent as a
  value-test owner (judge each action on its line to the outcome) rather than a KPI-executor, and ship
  a `vision-captcha` worked example. (`ad99fc4`)
- **Examples gallery + agent templates** — a Quickstart (time-to-first-run path), an `examples/`
  gallery, and reusable agent templates: `code-review` + `web-research` (`e09d23e`) and `data-pipeline`
  (`d882579`), with `templates/README.md` made true to what ships. (`e3f7605`, `e09d23e`, `d882579`)
- **Safe starter task on `enclave init`** — a fresh install seeds a real, non-blank starter inbox so a
  new pod has a first task instead of a blank-inbox wall. (`87b5d7c`)

### Changed
- **Memory compaction is now size-aware, not age-only** — daily `compact` adds a `_size_guard` step
  that archives the oldest entries of the always-loaded `work.json` / `inbox.md` once they exceed a cap
  (default 40k each), because those two files are re-loaded every tick and an oversized one taxes every
  future tick's context. Safe: archives to history, never silently drops content. (`509a1d4`)
- **Dashboard rollup headline** — the fleet dashboard now rolls per-pod status into a single headline
  summary. (`ee4c824`)
- **Docs concision pass** across the runtime docs. (`82a0e26`)

### Fixed
- **Per-tick `chat-reply.md` Read-before-Write tax** — the runtime now deletes the outbound
  `state/chat-reply.md` (and its `.cid` sidecar) at tick start. It is a per-tick OUTBOUND file, never
  an agent input; left on disk it made the agent's first Write trip the brain's Read-before-Write
  guard every tick (a wasted turn + a full re-read of the file). Deleting it makes that Write a clean
  create. Safe: both consumers act only on a *new* write, and the tick cadence far exceeds their poll
  interval.
- **CI (`test_live_lifecycle.py`) went red on every push** — the opt-in live-lifecycle test now
  self-skips (exit 0) whenever `ENCLAVE_LIVE!=1`. Its `ENCLAVE_STACKS_ROOTS` requirement previously
  raised `SystemExit(1)` at import time, *before* the skip gate, so any un-opted-in run (CI, the
  hermetic `run_tests.sh`) exited non-zero and failed the build — contradicting the file's own
  "self-skips unless `ENCLAVE_LIVE=1`" docstring. The `ENCLAVE_LIVE` opt-in gate is now hoisted above
  every env requirement. (Code shipped in `58f655d7`; CI run now COMPLETED SUCCESS.)
- **Secrets vault false-positive on placeholders** — `<angle-bracket>` values (e.g. `<your-token>`)
  are now treated as reference placeholders, not real secrets, so a template no longer trips the
  vault scan. (`18cbf61`)
- **Fleet clone drift** — `enclave fleet up`/`restart` now fast-forwards the bind-mounted clone to
  `origin/main` before every start (`ENCLAVE_AUTO_SYNC` / `ENCLAVE_SYNC_BRANCH` knobs), so a stale
  working tree can't silently run old framework code. (`3fba22a`)
- **`effective_config` mis-reported `decision_capture`** — it now reports `decision_capture` as ACTIVE
  for the `claude` brain, matching actual behaviour. (`c980a81`)
- **`enclave init` MODEL leak** — non-Claude (`BRAIN=api|local`) installs no longer leak a stray
  `MODEL=claude-…` into `agent.env`. (`87b5d7c`)
- **Digest work-item fallback** — the tick digest falls back gracefully when a work item is malformed
  instead of dropping the section. (`ddcabc3`)
- **Scorecard churn-window reclassification** — corrected how the scorecard classifies work inside the
  churn window. (`1f7b09f`)

## [0.1.0] — 2026-07-23
First public release. Apache-2.0. Previously developed as a team-private alpha; the history is
retained rather than squashed, so the reasoning behind each behaviour stays readable.

### Added
- **Agent runtime** — hardened container (`--cap-drop=ALL`, `no-new-privileges`, no inbound ports),
  read-only `secrets/` mount, `home/ → /agent` brain vault, `WORK_DIR → /work` project mount.
- **Brain-agnostic** — `BRAIN=claude | api | local | optimize` behind one env var, same guard and
  same per-tick telemetry on every path.
- **PreToolUse guard** — blocks `git`, foreign-secret reads and opt-in cloud/production writes; fires
  even under `--dangerously-skip-permissions`. Declarative egress allowlist, **report-only until
  `GUARD_EGRESS_ENFORCE=1`**.
- **Fleet control** — `enclave fleet` CLI and a local web console (chat, status, diagnostics, config,
  skills, logs, monitor) over every deployment on the host.
- **Memory as one linked vault** — markdown wiki + facts/decisions/lessons + skills, traversable as a
  graph; scan-gated, fail-closed auto-snapshot after every tick; optional `qmd` semantic search and
  `codegraph` code memory.
- **Cost discipline** — model-tier routing (`ROUTER=on`) and manager→worker delegation, so routine
  work leaves the frontier model.
- **Self-improvement loop** — `skillforge.py` detects tasks the agent has repeated and prompts it to
  write a skill; `memory.py learn --gate` admits one only if it is a real procedure, and **refuses a
  revision whose declared `validate:` score drops**, with a rejected-edit buffer. Recall is composed
  into the tick prompt, so a skill written last tick is applied on the next.
- **Bridges** — the documented pattern for giving an agent a host capability, plus a working template
  (`docs/BRIDGES.md`, `tools/bridge-template/`).
- **Completion contracts** — a directive can carry a machine-checkable check; a tick that claims to
  have served it is verified against evidence, and a failing claim is logged and escalated.

### Known gaps at 0.1.0
Host bridges are not included (pattern only); egress enforcement is opt-in; Windows is untested; the
WASM tool sandbox ships as policy without a wired executor. See "Known gaps" in `README.md`.
