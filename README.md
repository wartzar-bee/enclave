# Enclave

**A security-first, brain-agnostic agent runtime.** Run an autonomous agent in a hardened
container with scoped credentials and a local web chat — `docker compose up`, talk to it in your
browser. The agent is architecturally constrained: it can only reach what you explicitly give it,
even if prompt-injected.

> Status: **team-private alpha.** Internal team use while we harden it. See "Known gaps" below.

## Quick start
```bash
git clone <repo-url> enclave && cd enclave
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

## The chat (claude.ai-style, `platform/agentd/web_chat.py`, pure stdlib)
- **Image attachments** — paperclip / paste / drag-drop. Uploaded to `home/uploads/` (gitignored);
  the agent reads them by path with its Read tool (vision-capable brains see them).
- **Voice input** — mic button dictates via the browser's Web Speech API into the composer.
- **Speak replies** — per-message 🔊 and a header toggle for auto-read, via browser `speechSynthesis`.
- **Live model switch** — the composer model picker writes `home/state/model.override`; `runtime.sh`
  honors it on the next tick (allowlisted ids only — never executed as shell). Models follow `BRAIN`.
- _Privacy note:_ voice runs **in the browser** for zero-infra portability — in Chrome, dictation
  audio is transcribed by the browser's cloud service. A fully-offline STT/TTS backend (the host
  transcribe bridge / VibeVoice) is a planned opt-in for security-sensitive deployments.

## Why it's safe (verifiable by reading code, not trusting us)
- **Container isolation** — `--cap-drop=ALL --security-opt=no-new-privileges`; no inbound ports on the agent.
- **PreToolUse guard** (`platform/agentd/hooks/guard.py`) — fires even under `--dangerously-skip-permissions`;
  blocks `git`, foreign-secret reads, and (opt-in profiles) cloud writes / production mutations.
- **Scoped secrets** — a read-only `./secrets/` mount; the agent can't reach anything else.
- **Scoped knowledge** — semantic search is fronted by a per-agent gateway with a collection allowlist.
- **Brain-agnostic** — `BRAIN=claude | api | local`; same container, same guard, one env var.

## Layout
```
Dockerfile.agent          lean agent image (python + node + claude CLI; no bloat)
Dockerfile.chat/.relay    web-chat + telegram sidecars (stdlib, tiny)
docker-compose.yml        the stack
bin/enclave               CLI: init / run / send / chat / logs / status / stop
platform/agentd/          the runtime: agentloop, runtime.sh, guard hooks, memory, web_chat, qmd gateway
tools/gcloud/             optional multi-tenant, read-only gcloud bridge (per-agent credential isolation)
templates/                starter agent homes (ops, support, …)
docs/MEMORY-MODES.md      embedded vs shared memory design
```

## Memory (pluggable, portable by default)
- **Default = the wiki layer** — an LLM-maintained markdown knowledge base (`home/knowledge/`), zero
  infra, cross-platform, traceable. No DB/GPU/service. See `docs/WIKI-LAYER.md`.
- **Opt-in accelerators** behind one MCP interface — `qmd` (hybrid semantic search; CPU-in-container
  or a GPU host), LanceDB, Cognee (graph). See `docs/MEMORY-PROVIDERS.md` and `docs/MEMORY-MODES.md`.

## Known gaps (the road to "any teammate, any machine")
- **qmd in-container** — the wiki works everywhere now; the optional qmd accelerator still runs as a
  host engine. Containerizing it (CPU default, `--gpus` where available) is the next build.
- **Prebuilt image** — today `enclave run` builds locally; a team registry image is planned.
- **In-app chat replies** — the web chat delivers messages and shows the agent's `chat-reply.md`;
  a tighter responder loop (`chat_responder`) isn't in the lean image yet, so replies arrive on the
  agent's normal tick cadence.

See `SECURITY.md` for the threat model and `docs/` for design notes.
