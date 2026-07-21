# Enclave

**A security-first, brain-agnostic agent runtime.** Run an autonomous agent in a hardened
container with scoped credentials and a local web chat — `docker compose up`, talk to it in your
browser. The agent is architecturally constrained: it can only reach what you explicitly give it,
even if prompt-injected.

> Status: **team-private alpha.** Internal team use while we harden it. See "Known gaps" below.

## Requirements
- **Docker** (Desktop or Engine) — **running**. The agent runs in a container. (`enclave run`/`console` check this and tell you if it's not.)
- **Python 3** — runs `bin/enclave`. **Git** + access to this private repo.
- Brain credential: `BRAIN=claude` → the **`claude` CLI** (the `init` wizard runs `claude setup-token`) + a Claude subscription · `api` → an OpenAI-compatible key (e.g. OpenRouter) · `local` → a model server on the host (Ollama/MLX).

## Quick start
The repo is **private** — clone with your GitHub access to the org:
```bash
git clone https://github.com/demopod/enclave.git enclave && cd enclave
./bin/enclave init                   # wizard: name, brain, model, port, paste your credential
./bin/enclave run                    # build + start, then opens the chat in your browser
```
`init` populates `home/` (the agent's mounted `/agent`: mission, runtime config, work queue, knowledge
wiki), `secrets/` (your read-only credential), and `.env`. For `BRAIN=claude` the wizard can run
`claude setup-token` for you to mint a token. `run` brings up the stack and auto-opens
`http://127.0.0.1:8888/`. Non-interactive (CI):
```bash
./bin/enclave init --yes --name my-agent --brain claude --model claude-sonnet-4-6 --cred "$TOKEN"
./bin/enclave run --no-open
```

## Operating it day-to-day
Everything runs through `./bin/enclave` from the deployment folder:

| Want to… | Command |
|----------|---------|
| Start / open the chat | `enclave run` (builds if needed, opens the browser) |
| Open the dashboard (all agents) | `enclave console` (self-wires; opens the browser) |
| Stop the agent | `enclave stop` |
| Watch the runner log | `enclave logs` |
| Health + recent activity | `enclave status` |
| Send a task to the work queue | `enclave send "…"` |
| Switch the brain (keeps memory) | `enclave brain <claude\|api\|local\|optimize>` |
| Update the runtime to latest + rebuild | `enclave update` (git clone) · `enclave update --from <product-dir>` (any) · `enclave update --pull` (prebuilt images) |
| Commit the memory vault now | `enclave snapshot ["msg"]` (also auto-runs each tick) |

**Switch brain** — flips the mode in place (rewrites `agent.env` + `.env`, runs the `optimize` pool
wizard when needed) and recreates the container. **Memory/inbox/work are untouched** (unlike re-running
`init`). The first switch to a new mode rebuilds the image (the runtime is baked in); after that add
`--no-build` for an instant env-only recreate.
```bash
enclave brain optimize               # prompts for your LLM pools, seeds policy.json + secrets, rebuilds
enclave brain optimize --yes         # seed the documented default pools instead of prompting
enclave brain optimize --reconfigure # re-run the pool wizard even if policy.json already exists
enclave brain claude --no-build      # fast switch back, no rebuild (image already has the mode)
enclave brain api --model deepseek/deepseek-chat
```
The `optimize` brain (Claude-first, cost-aware fallthrough to any OpenAI-compatible pool — xAI / OpenAI /
Groq / OpenRouter / local) is configured in `home/policy.json`; full guide: **`docs/OPTIMIZE-BRAIN.md`**.

**Update to the latest runtime** — the clone *is* the deployment, so pull and rebuild:
```bash
git pull            # get the latest platform/ + bin/enclave
enclave run         # rebuilds both images (docker compose up -d --build) and recreates the container
```
`run`/`brain` build by default; the changed `COPY platform/agentd/` layer is what pulls new runtime code
into the image. If a rebuild ever serves stale code, force it:
`docker compose build --no-cache agent chat && docker compose up -d`.

## Cost discipline (run a fleet without burning the model cap)
A persistent fleet on a frontier model burns the subscription/API cap fast — at fleet scale that's the binding constraint. Two layers keep cost down without losing judgment quality, both on by env flag:
- **Model-tier routing** (`platform/agentd/route_tier.py`, `ROUTER=on`) — routine maintenance heartbeats and purely mechanical directives (post / measure / narrate / commit) run on a cheaper model (`MODEL_ROUTINE`, e.g. `claude-sonnet-4-6`), reserving the top `MODEL` (e.g. `claude-opus`) for judgment (decide / design / review / adjudicate). **Safe-by-default:** anything ambiguous — or any upstream error — resolves to the top model. A directive tagged `[tier:top]`/`[tier:cheap]` overrides per-message; a completed `- [ ]` inbox item (one carrying a `done:` sub-line) is no longer counted as pending, so stale directives can't pin the top model.
- **Delegation** (`platform/agentd/delegate.py` + the `delegation_guard` PreToolUse hook) — when `BRAIN=claude`, a capable manager is *forced* to hand bulk code-writing to a cheap/local worker (`local_agent.py` in WORKER_MODE) instead of spending frontier tokens on the keystrokes: the worker writes the code under a verify-gate (and off-task edits are reverted) and returns only a JSON summary. The manager plans + reviews; the worker does the labor. The guard self-gates to `BRAIN=claude` — a no-op for `api`/`local` brains, which already *are* the cheap worker. See `docs/DELEGATION.md`.

## Managing a fleet (many agents)
Every deployment is independent, but you manage them all from one place — no per-agent babysitting.

- **`enclave fleet`** — CLI control plane over *all* deployments on the host (discovered via
  `docker compose ls`): `fleet list` (status / brain / model / chat-port / open-work / liveness, grouped
  by manager), `fleet up|down|restart|logs|send <id>`, `fleet open <id>`. Every mutation is validated
  (id + the compose file must live under `ENCLAVE_STACKS_ROOTS`) and written to an append-only audit log.
- **`enclave console`** — a web panel, **fully wired from one command**: it also starts the background
  services its buttons need (create-agent, apply-fix, health monitor), so you don't run any daemons by
  hand. Per agent: **Chat · Status · Diagnostics** (cost/context/tools/models, anomalies — works for
  **every brain**: `local`/`api` agents emit the same per-tick `events.jsonl`/`usage.jsonl` telemetry as the
  Claude path) **· Config**
  (brain/mode, and edit the agent's mission/CLAUDE.md live) **· Skills · Logs**, a left **rail** grouped
  by manager with live status, a per-agent **blocker strip**, a fleet **Monitor** (detect→cause→fix,
  with one-click Apply / Automate), and a **+ New Agent** form. Binds `127.0.0.1` only (tunnel for
  remote); optional `CONSOLE_TOKEN` gate; every mutation goes through the validated `fleet` helper, never
  direct docker. Flags: `--no-watchers` (console only), `--no-monitor`. Agents are discovered under
  `ENCLAVE_STACKS_ROOTS` (default: this checkout's parent — where `enclave new` puts siblings). Design
  notes: `docs/FLEET-CONSOLE-PLAN.md`.

## Running several agents at once
Each deployment is keyed to its **agent name** (`AGENT_ID`), not its folder — the compose project,
container names, and named volumes all derive from it, so deployments never collide no matter where
you put them. Spin up another agent in its own folder with one command:
```bash
./bin/enclave new support-bot                 # → ../support-bot/ , AGENT_ID=support-bot, a free port picked for you
./bin/enclave new analyst --dir ~/agents/analyst   # choose the folder explicitly
cd ../support-bot && ./bin/enclave run
```
`new` copies the product (minus your `home/`·`secrets/`·`.env`) into the new folder and runs `init`
there with that name + an auto-selected free port (8888, 8889, …). You name **both** the container and
the folder; isolation (home, secrets, work, chat sessions, ports) is automatic.

**No local build (prebuilt images):** a maintainer publishes once —
`./bin/enclave publish --registry ghcr.io/<owner>` (after `docker login ghcr.io`) — then teammates set
`ENCLAVE_AGENT_IMAGE`/`ENCLAVE_CHAT_IMAGE` in `.env` and run `./bin/enclave run --pull` (no 5-min build).
By default `publish` builds for the **maintainer's architecture only**. For a mixed fleet (Apple-silicon
+ x86), publish **multi-arch**: `./bin/enclave publish --registry ghcr.io/<owner> --platform
linux/amd64,linux/arm64` (uses buildx + QEMU; the emulated arch is slow to build but teammates on either
arch then `run --pull` cleanly).

## Where work is saved — home (brain) vs `/work` (project)
Enclave answers "where does the agent's work go, and how does it stay searchable?" with two mounts:
- **`home/` → `/agent`** — the agent's **brain**: `CLAUDE.md`, `memory/`, `skills/`, `inbox.md`,
  `work.json`, `state/`. A scan-gated git vault (durable + secret-safe).
- **`WORK_DIR` → `/work`** — the agent's **working folder**: the real project tree it operates on and
  **saves work into** (rw). Set `WORK_DIR` in `.env` to any host path to make that tree the working
  folder; leave it blank to default to `home/work` (inside the vault). `enclave init` prompts for it
  (`--work-dir /abs/path` non-interactively).

Saved work stays searchable by **indexing `/work`** — the host qmd gateway re-embeds it on a timer (or
use the containerized `qmd`/`codegraph` profiles), so the agent's own output feeds back into its memory
within minutes. The agent writes files freely; it can't `git` (guard-blocked) — you own commits.
> ⚠ Mounting a real repo rw exposes whatever it contains to the agent — **including any secrets
> checked into that tree**. Scrub the tree, treat the deployment as trusted, or use a read-only
> reference mount plus a separate writable output folder. Full guide: **`docs/WORK-DIR.md`**.

## The chat (claude.ai-style, `platform/agentd/web_chat.py`, pure stdlib)
It's a real Claude-Code conversation in the browser — only the UI differs:
- **Continuous, multi-conversation** — a collapsible left sidebar (**New chat · Search chats · chat list**,
  each with a "…" menu to **star/delete**). Every thread is a **resumable Claude Code session at the
  agent's own model** (not a downgraded side-model) — it remembers the whole thread incl. tool calls.
  Threads live in `state/chat/<id>.jsonl`; **sessions persist across image rebuilds** (named volume on
  `~/.claude`). New chat = a fresh session; the agent's durable memory carries across all of them.
- **Slash commands** — type `/` for a menu of the agent's **skills** (`/ps-op-support`, `/ps-data`, …,
  substring search) plus UI commands `/clear`, `/retry` (resend your last message), `/export` (download
  the chat as markdown), `/help`. Skills run in-session; UI commands run locally.
- **Export** — `/export` or the chat's "…" menu → **Export markdown** downloads the whole conversation.
- **Stop** — the send button flips to ⏹ while a turn runs and kills the in-flight turn.
- **Keyboard + mobile** — ⌘/Ctrl-K new chat, Esc stops a running turn (else closes menus); on a phone
  the sidebar slides in as an overlay and the chat is full-width.
- **File downloads** — the agent writes deliverables to `/agent/outputs/` and links them
  `[name](/download?path=name)`; the chat renders a ⬇ download button (CSV, reports, exports).
- **Rich rendering** — markdown → HTML (tables, lists, headings, links); fenced code stays literal.
- **Auto topic titles** — each conversation is named by topic, not the verbatim first message.
- **Learns from corrections** — when you correct it or teach it a lasting fact, it verifies what it can
  and saves it to durable memory automatically, stamped with a **confidence grade** (`unverified` →
  `plausible` → `verified` → `strongly-verified`) + provenance, linked into the knowledge graph. It
  tells you in one line what it saved and at which grade; grades get promoted/demoted as evidence
  arrives. Carries across all future chats and work ticks (not just the current thread).
- **Image attach** (paperclip/paste/drag-drop → `home/uploads/`), **voice in/out** (browser Web Speech,
  or server-side via `TRANSCRIBE_URL`/`TTS_URL` — see `docs/VOICE-BACKEND.md`), and a **live model picker**.
- **Conversation, not a work tick** — a chat turn is **read-only by default** (it can read files + search,
  but Bash/Write/Edit are disallowed) so "status?" returns an answer instead of sending the agent off on a
  long build. Ask for *actions* via the **work plane** (`inbox.md` + `enclave send` / Telegram); or set
  `CHAT_ALLOW_WRITES=1` to let chat act. Runs on the agent's brain: `claude` = a resumable session;
  `api`/`local` = a single-shot on that brain's own endpoint.
- Tunables: `CHAT_MODEL` (chat model), `CHAT_BASE` (run chat on a *different/faster* endpoint than the work
  brain — e.g. a `local` agent chats on NVIDIA so it doesn't fight the tick for the GPU), `CHAT_ALLOW_WRITES`,
  `CHAT_RESPONDER=off`. **Full reference: `docs/CHAT.md`.**

## Why it's safe (verifiable by reading code, not trusting us)
- **Container isolation** — `--cap-drop=ALL --security-opt=no-new-privileges`; no inbound ports on the agent.
- **PreToolUse guard** (`platform/agentd/hooks/guard.py`) — fires even under `--dangerously-skip-permissions`;
  blocks `git`, foreign-secret reads, and (opt-in profiles) cloud writes / production mutations.
- **Scoped secrets** — a read-only `./secrets/` mount; the agent can't reach anything else.
- **Scoped knowledge** — semantic search is fronted by a per-agent gateway with a collection allowlist.
- **Brain-agnostic** — `BRAIN=claude | api | local | optimize`; same container, same guard, one env var.
  `optimize` is the adaptive cost router: it runs Claude (free at the margin) while the 5h/7d cap has
  headroom, then shifts to the cheapest *reachable* pool in `policy.json` — any OpenAI-compatible
  provider (xAI / OpenAI / Groq / OpenRouter / a local mlx-ollama server), added by editing one file.

## Layout
```
Dockerfile.agent          lean agent image (python + node + claude CLI; opt-in codegraph)
Dockerfile.chat/.relay    web-chat + telegram sidecars (stdlib, tiny)
Dockerfile.qmd/.codegraph optional memory-accelerator images (off by default — compose profiles)
docker-compose.yml        the stack (+ opt-in `qmd` / `codegraph` / `telegram` profiles)
bin/enclave               CLI: new / init / brain / run / publish / snapshot / vault-encrypt|decrypt / send / chat / status / stop / logs
platform/agentd/          the runtime: agentloop, runtime.sh, guard + delegation_guard hooks, route_tier
                          (model-tier router), delegate.py + local_agent.py (manager→worker delegation),
                          memory (memory.py + wiki.py), vault_snapshot.py, web_chat, chat_responder,
                          qmd + codegraph gateways, rlm.py
tools/gcloud/             optional multi-tenant, read-only gcloud bridge (per-agent credential isolation)
templates/                starter agent homes (ops, support, analyst) — all wired with guard + delegation_guard
docs/                     design notes — CHAT (the chat plane + per-brain endpoints + tunables),
                          DELEGATION (manager→worker), OPTIMIZE-BRAIN, WORK-DIR (working folder + indexing),
                          WIKI-LAYER, MEMORY-PROVIDERS, MEMORY-MODES, CODE-MEMORY, WASM-SANDBOX,
                          VETTING (dependency security passes), ROADMAP
```

## Memory — one linked, durable, secret-safe brain
The agent's memory is **one linked vault**, all markdown, all git-trackable, navigable as a graph:
- **Store (default, zero-infra)** — an LLM-maintained markdown **wiki** (`home/knowledge/`) *plus*
  operational memory (`home/memory/` facts/decisions/lessons + `home/skills/`). They're one graph:
  `wiki.py graph --brain` traverses backlinks/neighbors/k-hop/paths across all of it. No DB/GPU/service.
  See `docs/WIKI-LAYER.md`.
- **Retrieve (opt-in)** — `qmd` hybrid semantic search, host *or* containerized (`--profile qmd`,
  CPU default). See `docs/MEMORY-PROVIDERS.md` / `docs/MEMORY-MODES.md`.
- **Code memory (opt-in)** — **codegraph** symbol/call/dependency graph over a repo corpus, three ways:
  in-agent (stdio), shared index, or a network HTTP bridge (`--profile codegraph`). See `docs/CODE-MEMORY.md`.
- **Reason over huge context** — `rlm` chunk → map → tree-reduce, for blobs too big to read in full.
- _A generic graph engine (Cognee) was evaluated and **rejected** (telemetry + 127 deps); the wiki
  graph + codegraph cover the real needs. See `docs/VETTING.md`._

**Durable + secret-safe:** `enclave init` makes `home/` its own git vault; the runtime **auto-snapshots
after every tick** so memory survives a machine wipe. Every snapshot is **scan-gated, fail-closed** — a
credential pasted into memory **blocks the commit** (git history is forever) and a pre-commit hook blocks
manual commits too. `enclave vault-encrypt` writes an AES-256 archive (key in `secrets/`, never committed)
so an off-machine copy is ciphertext. The agent can't `git` (guard-blocked) — the runtime owns commits.

## Known gaps (honest)
- **Prebuilt images** — `enclave-agent`/`enclave-chat` publish to ghcr (`run --pull`); single-arch by
  default, **multi-arch on demand** via `publish --platform linux/amd64,linux/arm64`. The optional
  `qmd`/`codegraph` accelerator images still build locally on first `--profile … up`.
- **WASM tool sandbox** — the policy + a flagged routing hook ship; the `wasmtime` runtime (vetted-safe)
  isn't wired into an executor yet. Defense-in-depth, not a blocker. See `docs/WASM-SANDBOX.md`.
- **Transparent at-rest encryption** — the openssl archive ships; `age`/`git-crypt` (per-file,
  transparent) are drop-in upgrades when installed.

See `SECURITY.md` for the threat model and `docs/` for design notes.
