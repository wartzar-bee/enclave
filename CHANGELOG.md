# Changelog

All notable changes to Enclave. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/). Pre-1.0 means the layout and env-var names can still
move between minor versions — pin a tag if that matters to you.

## [Unreleased]

### Fixed
- **`preflight.probe_image` verifies the key WORKS, not that a file exists.** It returned
  `p.exists()`, so through the whole stretch where the game-dev pod's OpenRouter key answered 401 on every
  `gen.py` call the capability board still read `image: ok, "openrouter key present"` — the false alarm
  and the false all-clear were equally invisible, and a resolved key blocker stayed quoted as open for
  days. It now authenticates against OpenRouter's free `/auth/key` endpoint (no generation spend):
  200 → AUTHENTICATES, 401 → present but DEAD with the refresh instruction, otherwise → inconclusive.
  `_http` gains `headers=` and an `HTTPError` branch so 401/403/429 report as themselves instead of
  collapsing to 0 — a probe that cannot tell "dead" from "down" diagnoses neither. Verified live in a
  running pod. (`platform/agentd/preflight.py`)
- **Secret-gate false-positive class cleared + vault hooks self-refresh.** Five precision fixes to the
  secret scanner, each pinned by a verbatim fixture from the files that froze a brain-backup snapshot:
  label-value capture stops at a closing quote (so `BRAVE_API_KEY:'x'` no longer welds 1-char
  placeholders into an 8+ char "value"); the Slack FORMAT requires its real numeric-first tail, so the
  docs placeholder `xoxb-your-bot-token` stops matching (plain ERE — the shipped `bash_pattern` carries
  FORMATS verbatim, no lookaheads); `_CALL` tolerates a trailing `;` (`getApiKey();`); `is_reference`
  now peels literal `\n`/`;` tails (sourcemap-embedded source), treats `***` masks, fill-in words
  (`your`/`test`/`example`/…), and delimited credential-label identifiers (`managedApiKey`,
  `gateway-secret`, `ntn_env_token_123`) as references — all gated **behind** `looks_random` so entropy
  still wins and flat runs like `mysupersecrettoken` still block. Separately, `vault_snapshot.ensure_repo`
  now refreshes the baked pre-commit hook by CONTENT and runs on every snapshot, so a vault born under an
  older pattern stops blocking commits after a scanner fix ships. Suites: secrets + vault 39/39 + hooks +
  local_agent green. (`platform/agentd/secrets.py`, `vault_snapshot.py`; `2da10113`)

## [0.7.0] — 2026-08-18

A worker-tier + eval + cost-safety release. The headline is a new **code-over-data** worker path for
answering questions about big structured files cheaply (write code against the parsed object, never
feed the data to the model), a harness-vs-harness eval adapter to keep that path honest, and two
Claude-cost-leak gates promoted from incident fixes to un-rearmable config/runtime rules. All changes
are additive or opt-in; the base image stays stdlib-only. CI is 46/46 green at head.

### Added
- **`pyexec` — stdlib code-over-object big-data tool.** Parse a big structured file (JSONL/JSON/CSV/
  text) into a Python value once; a cheap worker brain sees only a bounded contract (size, per-key
  coverage, truncated samples — never the data) and writes code cells that run in a fresh subprocess
  with the data preloaded; tracebacks feed back, `FINAL:` ends the loop. Replaces the map-reduce
  `rlm.py` path for counting/aggregation, which structurally can't sum what each chunk never saw.
  (`platform/agentd/pyexec.py`, wired into `local_agent.py`; `fbd73361`)
- **`enclave eval bigdata` — harness-vs-harness measurement adapter.** Exact counting over a large
  seeded synthetic JSONL (deterministic, gold-by-construction; `--data` swaps a local file). `--harness
  pyexec|rlm|both` races harnesses on the same fixture + model; per-harness summary reports correct/n,
  avg tokens, avg calls, avg secs. (`platform/agentd/eval/adapters.py`, `docs/EVAL.md`; `adce71b9`)
- **Optional NOOA worker tier (`INSTALL_NOOA` build-arg, default OFF).** Opt-in pinned install of
  `nooa==0.0.8` + `litellm==1.84.0` (vetted 2026-08-17: official NVIDIA-NeMo org, no phone-home, no
  install hooks, pip-audit clean; litellm at its CVE-fix floor, past the yanked 1.82.7/8). Base image
  stays stdlib-only when unset. `nooa_worker.py` is the code-over-data sibling of `pyexec` for the
  eval race; NOOA execs model-written Python in-process, so the pod container is the containment
  boundary (same doctrine as the bash tool). (`Dockerfile.agent`, `platform/agentd/nooa_worker.py`;
  `9609c310`)

### Changed
- **Escalation now defaults to `google/gemini-2.5-pro`, not a Claude id.** The `local_agent`
  escalation endpoint no longer falls back to `anthropic/claude-sonnet-4.6` on the metered OpenRouter
  base — Claude tokens come only from the claude CLI pool. (`platform/agentd/local_agent.py`; `5f7dd5a6`)

### Fixed
- **De-armed three retired-model traps (worker tier).** `delegate.py` now resolves `policy.json` at
  `$TOOLS_ROOT/tools/llm/` (the path compose actually mounts — the old path existed nowhere, so every
  delegation raised); `monitor/intel.py` drops the hardcoded retired-qwen default (HTTP 410) and now
  requires both key AND model before reporting the intel layer "on"; `catalog.py` purges the retired
  qwen id from the nvidia seed + no-claude preset (now `openai/gpt-oss-120b`). (`833afb52`)
- **NOOA harmony channel-token leak sanitized.** NVIDIA's gpt-oss stochastically emits a tool-call
  name as `execute_python<|channel|>commentary`; stripping from `<|` in the metrics wrapper restores
  exact tool-matching (measured: 0/9 → 7/9 exact, 9/9 computed-correct on the harder-task grid).
  (`platform/agentd/nooa_worker.py`; `3c3eda46`)
- **CI installs PyYAML.** The plugin suites need PyYAML (the shipped image bakes it) but the workflow
  ran bare Python, so 4 suites / 16 tests failed in CI while passing locally — red since `aeeb5ee`
  (2026-08-15). Verified 46/46 green in a clean `python:3.12`. (`.github/workflows/tests.yml`; `00432531`)

### Security
- **`cfg_llm_routing` preflight check (CRIT) + runtime escalation refusal.** A Claude/anthropic model
  id pointed at a metered endpoint re-buys tokens the subscription already covers (the $173.70
  OpenRouter leak, 2026-07-20/21, whose config remnants survived the original fix and re-armed it).
  Preflight now fires CRIT at boot on `BRAIN=api`+Claude model, `ESCALATION_MODEL` naming Claude off
  `anthropic.com`, or a provider-path `CHAT_MODEL`; `local_agent` refuses a metered-Claude escalation
  at runtime and disables it. Class is now un-rearmable. (+7 selftests) (`platform/agentd/preflight.py`,
  `local_agent.py`; `5f7dd5a6`)
- **Guard protects `.secrets/gh-app`.** The GitHub App private key (PaS ops tier) is now in the guard's
  read-blocked secret set — upstreamed from a downstream vendored patch so that copy needs no local
  patches. guard.py selftest 35/35. (`platform/agentd/hooks/guard.py`; `144f2b7f`)

## [0.6.0] — 2026-08-15

A security + durability + fleet-ops release driven by an external comparative review (enclave vs
LangGraph / CrewAI / OpenHands). Two hook-level credential/SSRF gates were **failing open**; several
state writers could tear `work.json` under the tick cutoff. Both classes are closed here, and an
optional kernel-level egress wall lands for deployments that want a real network boundary rather than
the guard's advisory allowlist.

### Added
- **Optional kernel-level egress enforcement (`docs/EGRESS.md`, `docker-compose.egress.yml`).** The
  guard's egress allowlist is command-text matching — bypassable by design (`SECURITY.md`). This
  overlay adds the real wall: an [OpenSandbox egress](https://github.com/opensandbox-group/OpenSandbox)
  sidecar (Apache-2.0, digest-pinned + cosign-verifiable) owns the agent's network namespace and
  enforces a **default-deny DNS + nftables allowlist in the kernel**. Names not in the policy get
  NXDOMAIN; connections to IPs that didn't come from an allowed name are dropped — so `U=$host; curl
  $U`, `user@host` URLs, custom resolvers (`dig @8.8.8.8`), and direct-IP connects all fail. Off by
  default; enable per deployment (`egress-policy.json` at the deployment root, `EGRESS_TOKEN` in
  `.env` only). `effective_config.py` reports the active egress posture. (`6af8e800`, `6a99055c`)
- **Optional credential vault on the same sidecar (phase 2 of `docs/EGRESS.md`).** Transparent
  mitmproxy on 80/443 injects the real credential into outbound requests at the proxy, so the agent's
  env and mounts hold only a placeholder — a compromised agent has no secret to exfiltrate. Needs
  sidecar caps `CHOWN,SETUID,SETGID`, a shared CA volume, and per-client CA env; the vault is
  memory-only, so reseed (`egress-vault-init.sh` pattern) after every egress restart. (`fe999e07`)
- **`GUARD_FAILCLOSED` fail-closed batch (INERT by default).** With the flag set, a crashing guard
  blocks the mutation instead of allowing it, and enforce-mode egress blocks on a missing policy.
  Ships OFF — enable only once `propagation_check.py` is green fleet-wide; kill switch is
  `GUARD_FAILCLOSED=0` + recreate, or the root-owned `/workspace/GUARD_FAILCLOSED_OFF`. (`929b7890`)
- **`propagation_check.py` — verify a framework fix actually reached every pod.** Reports per-pod
  adoption of the webfetch/secret_scan gates so a security fix can't silently miss a live pod
  (0 DEGRADED = fully propagated). (`875b05c3`)
- **`drain_recreate.py` — recreate a pod at a tick boundary, not mid-tick.** `enclave update` now
  drains through it, so a redeploy can no longer SIGKILL a running tick and tear `work.json`.
  (`22886c7c`, `05bcfdb5`)
- **`supervision.py` — real heartbeats for the guardian / spawn / control watchers.** The console's
  "watcher detected" now reads an actual heartbeat rather than a directory that always exists,
  closing the "host daemon tier silently unloads" class. (`683904bd`)
- **`settings_migrate.py` back-fills existing pods at tick boot** so the WebFetch guard wiring reaches
  already-running deployments without a manual settings edit. (`0e952286`)

### Fixed
- **secret_scan's credential-leak gate was failing open.** It loaded the shared `secrets` module by
  name and silently bound Python's stdlib `secrets` off-path — so the gate crashed and allowed the
  write. Now loaded by file path; the hook `--selftest`s are RUN by `run_tests.sh` (they never were —
  that's how this stayed invisible). (`bbde2f45`)
- **WebFetch bypassed the guard entirely.** It was never in the PreToolUse matcher, so its SSRF / IMDS
  and egress checks were dead code — an agent could `WebFetch 169.254.169.254/...` unblocked. Now
  wired into the matcher and propagated to live pods. (`0e952286`)
- **Torn `work.json` and empty-queue misreads.** `statefile.py` routes state writers through atomic
  writes (tmp + fsync + `os.replace`) and provides ONE canonical `work.json` reader — fixing both the
  torn write under the pkill cutoff and the bug where a `{"items":[...]}` file read as an EMPTY queue
  in 2 of 3 readers, parking pods that had a full backlog. (`a45d2dcc`, `f96bbded`)
- **CRITICAL: `enclave update` never actually drained.** A review-fix caught the drain path being
  skipped, plus four fleet/lifecycle fixes and a `settings_migrate` hook-name collision. (`6a99055c`,
  `05a778de`)
- **Guardian resurrected deliberately-stopped pods.** It now honours `state/.operator-stopped` and
  leaves a stopped pod down. (`8e0ce49d`)

### Changed
- **Per-agent lifecycle lock (flock).** Console, CLI, monitor, and guardian can no longer race a
  recreate of the same pod. (`d7ea7dbb`)
- **Guard blocks the agent from writing its own live `hooks/` or `settings.json`.** An agent can no
  longer rewrite the code that governs it. (`d654b728`)
- **Docs truth pass.** Fixed the broken quickstart step, changed "brain-agnostic" →
  "model-portable (deepest on Claude)", and now states plainly that the guard's egress is
  report-only and `PERMISSION=dangerous` by default. (`e777116a`)

### Security
- **Dropped the attacker-reachable `*.amazonaws.com` wildcard** from the default egress allowlist —
  it let exfil/fetch to any AWS-hosted bucket past the advisory check. (`f3640c2e`)
- CI now runs the security hooks' selftests (suite 29 → 43), so a gate that crashes fails the build
  instead of shipping dark.

_Note: the Dockerfile change to root-own `/workspace` (`081f9f69`) is committed but not yet active —
it needs an image rebuild; the read-only framework mount already protects the live fleet in the
meantime._

## [0.5.0] — 2026-08-03

### Added
- **Console lists every enclave agent on the host, grouped by fleet.** `docker compose ls` is
  host-global, so `ENCLAVE_STACKS_ROOTS` was silently ignored for the agent list. It is now honoured and
  the compose-ls filter fails **closed**; each agent's `fleet` is derived from which configured root it
  lives under (`fleet.py:_fleet_of`), with an optional label map `ENCLAVE_FLEET_LABELS`. (`2507051`)
- **Collapsible fleet groups in the console rail.** Click a fleet header to collapse it; state persists in
  `localStorage` (the rail is rebuilt on every SSE push, so the handler is delegated). (`45f44c0`)
- **`candidate-handoff` route — pods hand off to pods with no studio relay.** A `candidate-handoff`
  envelope auto-delivers to the target pod's `inbox.md`; only judgment/operator types still route through
  the studio. (`58fdb74`)
- **`enclave new` live-mounts the framework read-only.** New pods get a scaffolded
  `docker-compose.override.yml` bind-mounting `platform/agentd` at `:ro`, so a framework fix lands on the
  next `restart` with no image rebuild. (`a166e50`)

### Fixed
- **Delegation no longer dies silently when a vendor retires a model.** `delegate.py`'s kind→model table
  hardcoded `qwen/qwen3-next-80b-a3b-instruct`; NVIDIA retired it (HTTP 410) and every delegation on live
  pods failed for a week (54 calls / 0 successes), each logged as one `$0 brain_error` tick so it read as
  idle-and-cheap. The table now carries **no model name** — it resolves from the same `policy.json`
  `route.mjs` reads (the duplication that went stale cannot recur), RAISES naming what to set rather than
  guessing, and `--kind` accepts a stable `DELEGATE_KINDS` vocabulary mapped to policy capability names via
  `_KIND_ALIASES`. `tick_feeder` also now locates `tick-status.json` in the home state dir AND cwd-relative
  spots so it reaches RUNNING pods. (`2a93882`, `15bf697`, `d2b5fee`)
- **Agents write `tick-status.json` to an absolute path.** Templates named it relative; a pod whose cwd is
  `/work` wrote the status into the wrong tree. Templates now name `/agent/state/tick-status.json`. (`a166e50`)
- **agentloop: blocking is per-DEPENDENCY, not per-pod.** `wake_gate` short-circuited on `blocked` before
  `has_work`, so one unanswered dependency froze the whole queue — a pod sat idle for the full 6h ceiling
  with unrelated actionable items. A blocked pod now still ticks when it has other work. (`ea4e6c8`)
- **`enclave new` honours `kpi_artifacts` — a pod was being born unmeasurable.** `kpi_artifacts` was
  consumed only by `spawn_watcher.py` for venture-class specs, so `enclave new --spec` dropped it and
  started with no `state/scorecard-config.json` (`scorecard.py` reported `product:null`, console showed
  `prod:blind`). `_apply_spec_extras` now writes the same config. (`f71750d`)
- **Console: fleet grouping no longer repeats "standalone" under every fleet** — single-fleet installs
  keep the old sub-headers. (`8ba5c5d`)
- **Vault secret-gate no longer false-freezes brain backups on env-var NAME placeholders.**
  `is_reference()` now exempts a bare SCREAMING_SNAKE credential label used as a placeholder value
  (`header api-key:DEVTO_API_KEY`); `looks_random()` still catches any real opaque token first. (`139202d`)
- **HITL console hides pure-FYI / automated-monitoring rows** — `[monitor:` / `[vault-watch]` / `[judge]` /
  `[coach]` / `[fleet-guardian]` / `[fyi]` / `[board]` / `[studio-action]` prefixes are filtered out of the
  human decision queue. (`139202d`)

## [0.4.0] — 2026-07-31

### Added
- **`enclave plugin` — a vetted, installable extension system** — add `bridge` / `tool` / `template` /
  `policy` add-ons without forking the framework. `enclave plugin init|add|list|remove`:
  `init` scaffolds a gate-passing skeleton; `add` runs the vetting gate (`tools/plugin/validate.py`)
  and **refuses to install anything it rejects**; the runtime re-vets and wires installed plugins on
  startup (fail-closed — a broken plugin is skipped with a logged reason, not a boot abort). Contract:
  `docs/PLUGINS.md`; five-step tutorial: `docs/BUILD-YOUR-FIRST-PLUGIN.md`.
- **Vetting gate is fail-closed and mechanism-agnostic.** The scan reads **every** source file a plugin
  ships (not just the entrypoint, regardless of file suffix), requires a pinned semver, and checks
  declared-vs-actual network egress / secret access / subprocess. A studio security review crafted four
  plugins that each defeated an earlier draft while printing "scan clean"; all four are now rejected
  (exit 2) and locked by RED fixtures: subprocess/`curl` egress is checked against `security.network`,
  a suffixless declared entrypoint is always scanned, and an over-size / unreadable / repo-escaping-symlink
  source is an **error** (never a warn). Framed honestly as a *lint that forces an honest manifest, not a
  sandbox* — a maintainer still reads the code. Never auto-runs an `install_script`.

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
