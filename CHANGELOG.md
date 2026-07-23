# Changelog

All notable changes to Enclave. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning is [SemVer](https://semver.org/). Pre-1.0 means the layout and env-var names can still
move between minor versions — pin a tag if that matters to you.

## [Unreleased]

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
