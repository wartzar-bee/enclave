# The chat plane

Enclave has **two planes** that drive an agent, on purpose:

| Plane | Source | What it's for | Module |
|---|---|---|---|
| **Work plane** | `inbox.md` + `tick.txt` (via `enclave send` / Telegram / the scheduler) | scheduled / autonomous work; **serialized**; runs the agent's mission tick after tick | `agentloop.py` + `runtime.sh` |
| **Chat plane** | `state/chat-inbox.jsonl` (written by the web chat) | interactive Q&A in the browser; answers **concurrently** with the work tick so a long task never blocks a reply | `chat_responder.py` (+ `web_chat.py` UI) |

`agentloop.py` spawns `chat_responder.chat_loop()` in a daemon thread. It watches `state/chat-inbox.jsonl`,
answers each message, and writes the reply to `state/chat-reply.md` (which the web UI polls). Disable the
whole plane with `CHAT_RESPONDER=off`.

## Chat is a CONVERSATION, not a work tick (read-only by default)

A chat turn loads the agent's `CLAUDE.md` (its autonomous *work-tick* mission — "build, every tick advance
the goal, never idle"). Left unchecked, a chat message like "status?" would send the agent off **building /
serving / screenshotting** and burn the whole turn to the timeout with no reply. So chat turns are
constrained to **answer + investigate, read-only**:

- The builder tools — **`Bash`, `Write`, `Edit`, `NotebookEdit`** — are disallowed in a chat turn (plus
  `AskUserQuestion`, which is interactive and would stall a headless turn). The agent still has `Read`,
  `Grep`, `Glob`, and semantic search (`qmd`) to find the answer (e.g. read `state/rollup.md` + `work.json`
  for "status?").
- Want chat to actually *act* (make an edit, run a build) → set **`CHAT_ALLOW_WRITES=1`**. Otherwise,
  ask for work via the **work plane** (`enclave send` / `inbox.md`) — it runs across ticks and is built for
  long tasks.

The system prompt (`CHAT_SYSTEM`) reinforces this: *answer directly; for a "do X" request, confirm it'll be
handled on the work tick, then stop.*

## Per-brain behaviour

The chat runs on whatever brain the agent runs on — and on the **right endpoint** for that brain:

- **`BRAIN=claude`** — each conversation is a **continuous, resumable Claude Code session** (one per web-chat
  thread): the first message starts a session, later messages `--resume` it, so the full thread (text *and*
  tool calls) is native context. Sessions persist across image rebuilds (named volume on `~/.claude`).
- **`BRAIN=api` / `BRAIN=local`** — a single-shot completion with the recent thread replayed as text (no
  native session). The chat hits the agent's own provider:
  - `local` → the **local model server** (MLX/Ollama, `LOCAL_BRAIN_BASE`, default `:8081`) with the local
    model (`LOCAL_BRAIN_MODEL`). A local brain that has no API key still authenticates (a dummy bearer the
    local server ignores) — it does **not** fall through to a cloud endpoint and 401.
  - `api` → `BRAIN_API_BASE` with `BRAIN_MODEL`, keyed by `BRAIN_API_KEY_ENV`.

### Chat on a different endpoint than the work brain — `CHAT_BASE`

A `BRAIN=local` agent runs its work tick on the host's single GPU. If chat *also* hit that GPU it would
queue behind the tick and time out. Point chat at a separate, fast endpoint instead:

```
CHAT_MODEL=qwen/qwen3-next-80b-a3b-instruct      # the chat model
CHAT_BASE=https://integrate.api.nvidia.com/v1    # a different endpoint than the work brain
BRAIN_API_KEY_ENV=NVIDIA_API_KEY                 # the key var for that endpoint
```

The work tick stays on local MLX; chat answers fast on the cloud-free model. `CHAT_MODEL` alone (without
`CHAT_BASE`) just overrides the model on the brain's default endpoint.

## Reply routing — one reply, the right conversation

Replies come back through a single `state/chat-reply.md` that `web_chat.py` polls. With several open
conversations, routing a reply purely by send-order (FIFO) crosses wires when turns finish out of order
(e.g. one conversation times out while another answers first). So the responder writes a sidecar
**`state/chat-reply.cid`** naming the conversation each reply belongs to, and `web_chat.py` routes by that id
(falling back to the FIFO only when the sidecar is absent — older responder / a proactive reply).

## Tunables (env)

| Var | Default | Effect |
|---|---|---|
| `CHAT_RESPONDER` | on | `off` disables the chat plane entirely |
| `CHAT_MODEL` | the agent's model | override just the chat model |
| `CHAT_BASE` | the brain's endpoint | run chat on a *different* endpoint than the work brain |
| `CHAT_ALLOW_WRITES` | unset (read-only) | `1` re-enables Bash/Write/Edit in chat turns |
| `CHAT_TURN_TIMEOUT` | 150 | seconds per chat turn before it's killed (a fallback reply is sent) |
| `CHAT_MAX_TOKENS` | 1024 | max tokens for the api/local single-shot path |

## Dashboard visibility for every brain

The fleet console's **Activity / Status / Diagnostics** read `state/events.jsonl` (a live per-tool feed) and
`state/usage.jsonl` (one record per tick). On the Claude path these come from Claude-Code hooks +
`usage_capture.py`. The `local`/`api` path fires no Claude hooks, so **`local_agent.py` emits the same two
records itself** — so a local/api agent shows real activity in the dashboard instead of looking idle while
it works. Both are best-effort (never block a tick).

See also: `web_chat.py` (the UI + reply polling), `chat_responder.py` (the responder), `docs/CONTROL.md`
(the work-plane control surface), `docs/FLEET-CONSOLE-PLAN.md` (the dashboard).
