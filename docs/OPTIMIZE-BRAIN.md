# BRAIN=optimize — the adaptive cost router

Start on Claude, shift to the cheapest reachable LLM as the subscription cap fills. Generic: every pool
is an OpenAI-compatible endpoint, so any provider works.

## How it decides (per tick)
`platform/agentd/route_brain.py` reads two things and prints ONE decision line for `runtime.sh`:

1. **Claude cap utilization** — `max(util_5h, util_7d)` from `state/claude-usage.json` (written by
   `claude_usage.py`). Unknown → assume headroom → stay on Claude.
2. **Tier** — judgment vs mechanical, from pending `- [ ]` inbox directives + the wake reason (same
   signal as `route_tier.py`). Uncertain → judgment (top).

| Cap | Judgment | Mechanical |
|-----|----------|------------|
| `< soft` (70%) | Claude **Opus** | Claude **Sonnet** |
| `soft..hard` (70–90%) | Claude **Sonnet** (conserve) | cheapest reachable **pool** |
| `>= hard` (90%) | highest-quality reachable **pool** | cheapest reachable **pool** |

Pools are ranked by `cost` (`free` → `low` → `high`). Each is **pinged** (`/models`) and skipped if
unreachable or missing its key — so it **always degrades back to Claude Sonnet** and never breaks a tick.

Decision line consumed by `runtime.sh`:
```
claude <model>                          # falls through to the Claude path with this model
pool <base_url> <api_key_env> <model>   # drives the tick via local_agent.py on that pool
```

## Configuring pools — `policy.json`
`enclave init --brain optimize` writes an editable `home/policy.json` (which **wins** over the baked
default at `platform/agentd/policy.json`). Edit it anytime — add/remove pools, change the cap floors.

```json
{
  "cap": { "soft_pct": 70, "hard_pct": 90 },
  "pools": {
    "local": { "base_url": "http://host.docker.internal:8081/v1", "api_key_env": "LOCAL_LLM_KEY", "model": "qwen2.5:7b", "cost": "free", "local": true },
    "cheap": { "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY", "model": "google/gemini-2.5-flash", "cost": "low", "local": false },
    "xai":   { "base_url": "https://api.x.ai/v1", "api_key_env": "XAI_API_KEY", "model": "grok-4", "cost": "high", "local": false }
  }
}
```

Add **any** OpenAI-compatible provider by adding a pool: `base_url` + `api_key_env` + `model` + `cost`
(`free`/`low`/`high`) + `local` (true → no key needed, ranked first for $0/privacy). The key goes in
`secrets/<name>.env` as `<api_key_env>=...` (resolved at tick time by env-var name, never committed).

## Init
```
enclave init --brain optimize          # prompts for the Claude token + a generic pool wizard
                                        #   (local server + remote pools: name/base/model/key/cost)
```
`--yes` ships the documented baked defaults (local mlx + OpenRouter cheap/top + an xAI example) — edit
`home/policy.json` and add `secrets/<pool>.env` afterward.

## Why this shape
Claude is free at the margin under the subscription, so spend it first and tier it (Opus for judgment,
Sonnet for mechanical). Only when the cap is genuinely tight do we pay a pool — cheapest for grunt work,
highest-quality for judgment we can't defer. The router is fail-open by construction: any failure (no
policy, no reachable pool, a metrics glitch, a parse error) falls back to Claude Sonnet.
