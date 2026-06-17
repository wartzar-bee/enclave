# Enclave

**A security-first, brain-agnostic agent runtime.** Run an autonomous agent in a hardened
container with scoped credentials and a local web chat — `docker compose up`, talk to it in your
browser. The agent is architecturally constrained: it can only reach what you explicitly give it,
even if prompt-injected.

> Status: **team-private alpha.** Internal team use while we harden it. See "Known gaps" below.

## Quick start
The repo is **private** — clone with your GitHub access to the org:
```bash
git clone https://github.com/wartzar-bee/enclave.git enclave && cd enclave
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

**No local build (prebuilt images):** a maintainer publishes once —
`./bin/enclave publish --registry ghcr.io/<owner>` (after `docker login ghcr.io`) — then teammates set
`ENCLAVE_AGENT_IMAGE`/`ENCLAVE_CHAT_IMAGE` in `.env` and run `./bin/enclave run --pull` (no 5-min build).

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
- **Real-time replies** — the chat runs on its own plane (`state/chat-inbox.jsonl` →
  `chat_responder.py`, a tool-capable cheap-model turn) **concurrent with** the autonomous work tick,
  so a long task never blocks a reply. The work plane (`inbox.md` + `enclave send` / Telegram) drives
  scheduled/directive work separately. Tune with `CHAT_RESPONDER=off` / `CHAT_MODEL=...`.
- **Image attachments** — paperclip / paste / drag-drop. Uploaded to `home/uploads/` (gitignored);
  the agent reads them by path with its Read tool (vision-capable brains see them).
- **Voice input** — mic button dictates via the browser's Web Speech API into the composer.
- **Speak replies** — per-message 🔊 and a header toggle for auto-read, via browser `speechSynthesis`.
- **Live model switch** — the composer model picker writes `home/state/model.override`; `runtime.sh`
  honors it on the next tick (allowlisted ids only — never executed as shell). Models follow `BRAIN`.
- _Voice privacy:_ voice runs **in the browser** by default (zero infra) — in Chrome, dictation audio
  goes to the browser's cloud STT. For a fully-controlled path, set `TRANSCRIBE_URL`/`TTS_URL` to a
  server-side STT/TTS service you run and the chat uses that instead (see `docs/VOICE-BACKEND.md`).

## Why it's safe (verifiable by reading code, not trusting us)
- **Container isolation** — `--cap-drop=ALL --security-opt=no-new-privileges`; no inbound ports on the agent.
- **PreToolUse guard** (`platform/agentd/hooks/guard.py`) — fires even under `--dangerously-skip-permissions`;
  blocks `git`, foreign-secret reads, and (opt-in profiles) cloud writes / production mutations.
- **Scoped secrets** — a read-only `./secrets/` mount; the agent can't reach anything else.
- **Scoped knowledge** — semantic search is fronted by a per-agent gateway with a collection allowlist.
- **Brain-agnostic** — `BRAIN=claude | api | local`; same container, same guard, one env var.

## Layout
```
Dockerfile.agent          lean agent image (python + node + claude CLI; opt-in codegraph)
Dockerfile.chat/.relay    web-chat + telegram sidecars (stdlib, tiny)
Dockerfile.qmd/.codegraph optional memory-accelerator images (off by default — compose profiles)
docker-compose.yml        the stack (+ opt-in `qmd` / `codegraph` / `telegram` profiles)
bin/enclave               CLI: init / run / publish / snapshot / vault-encrypt|decrypt / send / chat / status / stop / logs
platform/agentd/          the runtime: agentloop, runtime.sh, guard hooks, memory (memory.py + wiki.py),
                          vault_snapshot.py, web_chat, chat_responder, qmd + codegraph gateways, rlm.py
tools/gcloud/             optional multi-tenant, read-only gcloud bridge (per-agent credential isolation)
templates/                starter agent homes (ops, support, analyst)
docs/                     design notes — WORK-DIR (working folder + indexing), WIKI-LAYER,
                          MEMORY-PROVIDERS, MEMORY-MODES, CODE-MEMORY, WASM-SANDBOX,
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
- **Prebuilt images** — `enclave-agent`/`enclave-chat` are published multi-arch to ghcr (`run --pull`);
  the optional `qmd`/`codegraph` accelerator images still build locally on first `--profile … up`.
- **WASM tool sandbox** — the policy + a flagged routing hook ship; the `wasmtime` runtime (vetted-safe)
  isn't wired into an executor yet. Defense-in-depth, not a blocker. See `docs/WASM-SANDBOX.md`.
- **Transparent at-rest encryption** — the openssl archive ships; `age`/`git-crypt` (per-file,
  transparent) are drop-in upgrades when installed.

See `SECURITY.md` for the threat model and `docs/` for design notes.
