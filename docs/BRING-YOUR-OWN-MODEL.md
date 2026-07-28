# Bring your own model — run enclave on any LLM (not just Claude)

enclave is brain-agnostic. The sandbox, memory, guard and tick loop are the same whatever drives them;
you just point the agent at a different LLM. There are four brain modes:

| `--brain` | What drives the tick | Model lives in | Needs a key? |
|-----------|----------------------|----------------|--------------|
| `claude`   | Claude Code (Anthropic) | `MODEL=` | Claude OAuth token |
| **`api`**  | **Any OpenAI-compatible HTTP endpoint** (OpenRouter, NVIDIA, Groq, xAI, OpenAI, Together, DeepSeek…) | `BRAIN_MODEL=` + `BRAIN_API_BASE=` | provider key |
| **`local`**| **A local server** (Ollama / mlx / vLLM) — no cloud, no key | `BRAIN_MODEL=` + `BRAIN_API_BASE=` | no |
| `optimize` | Claude-first, **auto-falls-back** to the cheapest reachable pool as your cap fills | `policy.json` | Claude + pools |

This guide covers the plain `api` and `local` paths — one command, bring your own key. For the adaptive
Claude→cheap-pool router, see **[docs/OPTIMIZE-BRAIN.md](OPTIMIZE-BRAIN.md)**.

> Every command and `agent.env` snippet below is copied from a real `enclave init` run. A non-Claude
> agent runs on `BRAIN_MODEL`; enclave deliberately keeps `MODEL=` (Claude's field) *out* of its
> `agent.env` so the dashboard never mislabels it.

---

## 1. OpenRouter (default `api` preset) — one model, hundreds of models

```bash
enclave init --yes --name my-agent --brain api --cred sk-or-...your-openrouter-key...
```
Writes `home/agent.env`:
```
BRAIN_MODEL=deepseek/deepseek-chat
```
and seeds the key into `secrets/openrouter.env` (`OPENROUTER_API_KEY=…`). Pick any
[OpenRouter model](https://openrouter.ai/models) with `--model`:
```bash
enclave init --yes --name my-agent --brain api --model x-ai/grok-2 --cred sk-or-...
# → BRAIN_MODEL=x-ai/grok-2
```
Then `enclave run`.

## 2. NVIDIA NIM (free tier) — provider shorthand

`nvidia` and `openrouter` are built-in provider shorthands: name the provider and enclave fills in the
endpoint base + key var for you. Use a spec file:
```bash
cat > spec.yaml <<'EOF'
name: my-agent
brain: api
provider: nvidia
model: deepseek-ai/deepseek-v4-flash
EOF
enclave init --yes --spec spec.yaml
```
Writes:
```
BRAIN_MODEL=deepseek-ai/deepseek-v4-flash
BRAIN_API_BASE=https://integrate.api.nvidia.com/v1
BRAIN_API_KEY_ENV=NVIDIA_API_KEY
```
and scaffolds `secrets/nvidia.env` (paste your `NVIDIA_API_KEY` there). NVIDIA's catalog rotates —
pick any live id from `curl -s https://integrate.api.nvidia.com/v1/models -H "Authorization: Bearer
$NVIDIA_API_KEY"` (e.g. `deepseek-ai/deepseek-v4-flash`, `deepseek-ai/deepseek-v4-pro`).

**Verified live (cloud)** — a real completion against the endpoint enclave points
`--brain api --provider nvidia` at, captured against the NVIDIA NIM API on 2026-07-25:
```bash
curl -s https://integrate.api.nvidia.com/v1/chat/completions \
  -H "Authorization: Bearer $NVIDIA_API_KEY" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-ai/deepseek-v4-flash",
       "messages":[{"role":"user","content":"Reply with exactly one word: ready"}],
       "max_tokens":64,"temperature":0}'
# HTTP 200 → choices[0].message.content == "ready"
#          finish_reason == "stop"
#          usage == {prompt_tokens:11, completion_tokens:25, total_tokens:36}
```
Note `completion_tokens:25` for a one-word reply: `deepseek-v4-flash` is a **reasoning** model —
it spends hidden tokens (returned in `message.reasoning_content`) before the answer. Same trap as
the local reasoning-model gotcha in §4: with a small `max_tokens` the budget is consumed by the
reasoning trace and `message.content` comes back empty. Give reasoning models generous headroom, or
pick a plain instruct id (e.g. `meta/llama-3.3-70b-instruct`, also live on NVIDIA's catalog — verified
200 same day) for a terse answer under a tight token cap.

## 3. Any OpenAI-compatible endpoint (Groq / xAI / OpenAI / Together / vLLM)

No shorthand needed — give the base URL and the name of the key var explicitly:
```bash
cat > spec.yaml <<'EOF'
name: my-agent
brain: api
brain_api_base: https://api.groq.com/openai/v1
brain_api_key_env: GROQ_API_KEY
model: llama-3.3-70b-versatile
EOF
enclave init --yes --spec spec.yaml
```
Writes:
```
BRAIN_MODEL=llama-3.3-70b-versatile
BRAIN_API_BASE=https://api.groq.com/openai/v1
BRAIN_API_KEY_ENV=GROQ_API_KEY
```
Put `GROQ_API_KEY=…` in `secrets/groq.env`. Swap the base/key/model for xAI (`https://api.x.ai/v1`),
OpenAI (`https://api.openai.com/v1`), Together, or your own vLLM server.

## 4. Fully local — Ollama, no key, no cloud

```bash
enclave init --yes --name my-agent --brain local --model qwen2.5:7b
```
Writes:
```
BRAIN_MODEL=qwen2.5:7b
BRAIN_API_BASE=http://host.docker.internal:11434/v1
```
Run any [Ollama](https://ollama.com) model (`ollama pull qwen2.5:7b` first). No secret file — the agent
talks to your host's Ollama over `host.docker.internal`.

**Verified live** — the endpoint enclave points `--brain local` at is a standard OpenAI-compatible
route, so you can smoke-test your model before the first tick:

```bash
curl -s http://host.docker.internal:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"<your-model>","messages":[{"role":"user","content":"Say hi in one word."}],"stream":false,"max_tokens":512}'
# → choices[0].message.content = "Hey"  (real run, qwen3:30b-a3b, 2026-07-25)
```

> **Reasoning-model gotcha:** a thinking model (Qwen3, DeepSeek-R1, …) spends completion tokens on a
> hidden reasoning trace *before* the visible answer, and Ollama's `/v1` route returns that trace in a
> separate `message.reasoning` field. If `max_tokens` is small (e.g. 32) the whole budget is eaten by
> reasoning and `message.content` comes back **empty** — verified: 32 and 128-token caps both returned
> `content:''`; 512 returned `"Hey"`. Give reasoning brains generous headroom, or use a non-reasoning
> model / `/no_think`.

> **Known v1 limitation:** the local path assumes the host reachable at `host.docker.internal` (Docker
> Desktop on macOS/Windows). On native Linux, set `BRAIN_API_BASE` to your host IP or run Ollama in a
> sibling container on the same network. Tracked as a good-first-issue.

---

## Switch an existing agent's brain in place

No re-init — swap the model and rebuild, keeping memory:
```bash
enclave brain api --model deepseek/deepseek-chat   # → api endpoint
enclave brain local --model qwen2.5:7b             # → local Ollama
enclave brain claude                               # back to Claude
```

## Notes

- **Cost:** a non-Claude `api`/`local` agent already *is* the cheap worker — the delegation guard
  (which forces a Claude manager to hand code-writing to a cheap worker) is a no-op here. See
  [docs/DELEGATION.md](DELEGATION.md).
- **Model quality:** enclave doesn't validate arbitrary provider model ids (only Claude ids are
  allowlisted). A wrong id fails at run time with the provider's own error, not at init.
- **Verify before relying on it:** `enclave run` once and watch `state/` — a reachable endpoint returns
  a real tick; an unreachable one logs the provider error and the tick self-skips.
