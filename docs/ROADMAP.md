# Enclave — roadmap / backlog

Shipped: hardened runtime + guard (incl. loader-hijack denylist), `enclave init` wizard +
`run`/`publish`, lean images, **prebuilt images on ghcr**, wiki memory layer, **claude.ai-style chat —
multi-conversation sidebar (new/search/star/delete), continuous resumable Claude-Code sessions per
thread (persist across rebuilds), markdown→HTML, slash-command menu (skills + /clear/help), Stop button,
file downloads (`/agent/outputs`), auto topic titles, image attach, voice in/out, live model picker**,
server-side voice proxy, support/analyst templates, **collision-proof multi-deployment** (`enclave new`,
project keyed to `AGENT_ID`, auto free-port), **containerized qmd accelerator**, **RLM big-context tool**,
**Cognee graph-provider adapter stub** (engine rejected — see VETTING), **WASM-sandbox scope + flagged
routing hook**, **unified memory graph** (`wiki.py graph --brain` — knowledge + memory + skills one
linked vault), **codegraph code-memory** (in-agent / shared-index / HTTP bridge), **durable secret-safe
vault** (scan-gated git + per-tick auto-snapshot + `vault-encrypt` at-rest), **YAGNI code discipline**
distilled into the build harness, **first-class working folder** (`WORK_DIR` → `/work` rw mount,
distinct from the home/brain — where the agent does + saves real work, kept fresh by continuous
indexing; `docs/WORK-DIR.md`).

> **Hard rule:** never bake an external dependency (npm/pip/repo) into the image or a setup script
> without a security pass first — provenance + pinned version + CVE scan + read the actual code for
> exfil. If a thing is too sprawling to fully review, author our own from the distilled idea instead.

## ✅ #2 — publish prebuilt images to ghcr  (DONE 2026-06-16)
`enclave publish --registry ghcr.io/demopod` builds + pushes both images. The block was a
token *type*: ghcr only accepts a **classic** PAT with `write:packages` (fine-grained PATs can't
push packages at all). Operator minted a classic PAT (scopes `repo,
write:packages`); stored at `.secrets/ghcr.env` (gitignored). Live:
- `ghcr.io/demopod/enclave-agent:latest` · `ghcr.io/demopod/enclave-chat:latest` (private).
- Teammates: set `ENCLAVE_AGENT_IMAGE`/`ENCLAVE_CHAT_IMAGE` in `.env`, then `enclave run --pull`
  (needs `read:packages`, or flip the packages public on their GitHub pages).
- **Follow-up:** images are `linux/arm64` only (built on the Mac). For x86 teammates, add a
  multi-arch buildx push (`--platform linux/amd64,linux/arm64`). Pending operator OK on build time.

## ✅ #4 — containerize qmd  (DONE 2026-06-16)
`Dockerfile.qmd` installs PINNED **`@tobilu/qmd@2.5.3`** (vetted — see `docs/VETTING.md`) and builds
`better-sqlite3` *in-container* against the image's node, so the host node-26 ABI gotcha can't bite.
Runs `qmd_gateway.mjs` in HTTP mode on `:18182` (fail-closed on `QMD_ALLOWED_COLLECTIONS`, bind host
made configurable via `QMD_GW_HTTP_HOST`). Compose `qmd` profile (off by default) mounts the wiki as
read-only corpus + a named index volume; `QMD_MODE=reembed` rebuilds the index; CPU default
(`QMD_FORCE_CPU=1`) with a GPU path for Linux+NVIDIA. Tested: fail-closed + `/health` + serve.
- Enable: `docker compose --profile qmd up -d --build qmd` → point `home/.mcp.json` at
  `http://qmd:18182/mcp`. (Mac containers can't reach Metal → host qmd stays an option.)

## ✅ #5 — RLM big-context tool  (DONE 2026-06-16)
`platform/agentd/rlm.py` — our own impl (no external dep): chunk a huge input → MAP a sub-query over
each chunk on the cheap local brain → recursively tree-REDUCE to one answer. Wired as the `rlm` tool
in `local_agent` (EXECUTORS + HARNESS). Degrades to extractive keyword hits when offline. Tested on a
189k-char input (17 chunks → planted facts surfaced + stitched).

## ✅ #6 — Cognee graph-memory provider  (DONE 2026-06-16 — adapter stub, off by default)
`platform/agentd/providers/cognee_provider.py` — implements the full memory contract
(query/get/multi_get/ingest/lint/status) with the same per-agent allowlist; `get`/`multi_get` return
the wiki markdown today; `query` returns wiki keyword hits + a "graph not provisioned" notice. JSON-RPC
plug-point (`--http`) for `.mcp.json`. Registered in `docs/MEMORY-PROVIDERS.md`. Tested (contract +
allowlist deny + fail-closed + HTTP). **The Cognee engine stays gated** (license/telemetry + heavy
tree) — flip `COGNEE_ENABLED=1` only after a security pass; the interface won't change, only `query`.

## ✅ #7 — WASM tool sandbox  (DONE 2026-06-16 — scope + flagged hook; runtime gated)
`docs/WASM-SANDBOX.md` scopes the threat model + tool classes (Bash/code-exec = HIGH value).
`platform/agentd/hooks/sandbox_policy.py` is the pure, tested classifier; `guard.py` consumes it
behind `ENCLAVE_WASM_SANDBOX=1` to **log** what would be sandboxed (non-blocking, off by default).
The WASM **runtime** dep stays gated — when vetted, a `run_sandboxed()` executor flips the flag from
log to enforce; the policy/hook don't change. Tested (classifier + flag on/off + denials still deny).

## ✅ #8 — BRAIN=optimize adaptive cost router  (DONE 2026-06-17)
`platform/agentd/route_brain.py` + `policy.json` + a `runtime.sh` dispatch branch. Per tick it reads
Claude's 5h/7d cap utilization (`state/claude-usage.json`) and classifies the tick judgment/mechanical
(same signal as `route_tier.py`): **< soft (70%)** all on Claude tiered Opus/Sonnet; **soft..hard**
mechanical work leaves Claude; **>= hard (90%)** everything leaves — to the cheapest *reachable* pool
in `policy.json` (free local → low → high; judgment prefers the highest-quality reachable pool). Every
pool is an OpenAI-compatible endpoint (`base_url` + `api_key_env` + `model` + `cost`), so ANY provider
works (xAI / OpenAI / Groq / OpenRouter / local mlx-ollama) — add one by editing `policy.json`. Pools
are pinged + skipped if unreachable or missing a key, so it **always degrades back to Claude** and
never breaks a tick. `enclave init --brain optimize` runs a generic pool wizard (local + remote pools,
per-pool `secrets/<name>.env`) and writes an editable per-deployment `policy.json` (which wins over the
baked default). Reuses `local_agent.py` for the pool path. Tested: full decision matrix (7 cases),
deployment-policy override, decision-line parse, `init --yes` defaults. See `docs/OPTIMIZE-BRAIN.md`.

## ✅ #9 — backlog sweep (DONE 2026-06-18)
- **Multi-arch publish** — `enclave publish --platform linux/amd64,linux/arm64` does a buildx
  build+push (auto-creates a QEMU builder); single-arch stays the default. README multi-arch overclaim
  corrected (was "published multi-arch"; reality was arm64-only).
- **`autonomous` template** — the 4th starter: a self-driving daemon that each tick reads its
  pre-assembled digest (`state/recall.md`: open `work.json` + relevant memory) and executes the next
  step toward an operator `{MISSION}` (3h heartbeat, `SUPERVISE=auto`). It does NOT re-derive its whole
  state from the vault each tick, and its continuous backlog grind runs OFF-Opus (`BRAIN=optimize` +
  `ROUTER=on`) — see `docs/CONTEXT-AND-TICKS.md` (the lean-fresh-tick + off-Opus-continuous laws).
- **Chat polish** — `/export` (+ "…" menu) downloads a conversation as markdown; `/retry` resends the
  last message; ⌘/Ctrl-K new chat + Esc stop; mobile sidebar overlay + full-width chat. Surfaced model
  errors (no more silent "couldn't generate a reply"); init/brain reject unknown Claude model ids.
- **At-rest encryption** — `vault-encrypt` now prefers **age** (X25519) when installed, openssl
  fallback; decrypt auto-detects the format. age is used only if present (never auto-installed).
- **Security passes** — wasmtime (`docs/VETTING.md`): **PROCEED w/ conditions** (pin 45.0.0, Cranelift
  only, nested, track advisories). Cognee: **KEEP GATED** (sprawling, default-on telemetry to
  `test.prometh.ai`, default cloud egress).

## Open decisions / follow-ups
- **Chat streaming** — replies still wait-then-dump (the `claude -p --output-format json` turn is
  non-streaming). True token streaming needs `--output-format stream-json` + incremental delivery (SSE) —
  a real architecture change, deferred. A per-turn usage/cost line needs the same plumbing.
- **WASM runtime** (#7) — cleared to wire (`wasmtime==45.0.0`); the `run_sandboxed()` executor is the
  remaining ENGINEERING step (restricted exec surface; WASI can't run raw bash). Operator's call to start.
- **Cognee engine** (#6) — stays gated; if graph memory is needed, build our own networkx+sqlite layer
  behind the existing contract (per the hard rule).
- Per-teammate cloud-cred model → **resolved:** devops provisions a scoped read-only identity (see
  `SECURITY.md`).
