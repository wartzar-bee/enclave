# Context & ticks — how an enclave agent stays cheap

This is the design guardrail for the #1 cost in a persistent fleet: how much an agent re-sends and
re-computes every time it wakes. Read this before changing `runtime.sh`, `agentloop.py`, `memory.py`,
or any template's `CLAUDE.md` / `tick.txt`.

## The hard reality: the API is stateless

Claude does **not** keep your conversation server-side between ticks. Each `claude -p` tick is a fresh
session. So:

- There is **no free "warm context" to carry forward**. The model starts blank every tick.
- `--resume` / `--continue` do NOT recover free context — they **replay the local transcript as input
  tokens**. On a loop slower than the ~5-min prompt-cache TTL (our heartbeat is 15 min+), that replay
  is re-billed **uncached** every fire, and the thread only grows. That is the failure mode that once
  burned 136M tokens. **Never use `--resume`/`--continue` for an autonomous loop.**
  *(⚠ SUPERSEDED 2026-07-04: the runtime now DOES warm-resume by default (`WARM_SESSION=1`) —
  bounded by the calibrated cost caps, session-clear signals and auto-clear nets. The ban above is
  kept as history of WHY the bounds exist; the current mechanism is [`COST-CONTROL.md`](COST-CONTROL.md).)*

The win is therefore not "keep a session alive" — it's **make each fresh tick cheap**.

## The three laws

### 1. Lean-fresh-tick — keep the per-tick FIXED cost small
Every tick pays for: the system prompt (`--append-system-prompt "$(cat CLAUDE.md)"`) + `tick.txt` +
whatever the agent then reads. All of it is uncached at our cadence, so all of it is a recurring bill.

- **`CLAUDE.md` is a per-tick fixed cost.** Keep it a lean operating core — mission, tick protocol,
  how-to-recall, access rules. **Do NOT inline the memory index, long reference material, or env
  dumps into it.** That content lives in the `knowledge/` vault and is *retrieved on demand*.
- **Retrieve, don't dump.** The runtime pre-builds a small digest at `state/recall.md` every tick
  (`memory.py digest`: open work + most-relevant memory by keyword + qmd semantic recall). The agent
  reads THAT. It does not re-read the knowledge index or re-run qmd by default — only to fill a
  specific gap. (This is the claude-mem "retrieve at session start" pattern, already built here.)
- Keep `state/recall.md` itself lean (low `k`, short snippets) — it's built and read every tick.

### 2. Off-Opus-continuous — the backlog grind never runs on the top model
A self-driving agent re-fires back-to-back while `work.json` has open items (`agentloop._after`). If
those continuous ticks run on Opus, the cap is gone in a day. So:

- Continuous / heartbeat ticks use the **routine** model; **Opus is reserved for judgment directives
  and escalation.** Enforced by `ROUTER=on` + `MODEL_ROUTINE` (`route_tier.py` maps
  `continue`/`heartbeat` → routine, judgment directives → top).
- Default autonomous agents to **`BRAIN=optimize`** (start on Claude, shift to the cheapest reachable
  pool in `policy.json` as the cap fills) or **`BRAIN=local`** (a $0, off-cap local-model grind with
  the in-container off-Opus supervisor). Reserve `BRAIN=claude` on Opus for reactive / judgment agents.

### 3. Event-driven, self-paced — don't wake without a reason
`agentloop.py` already does this: it wakes on an inbox/comms event or the long heartbeat, and after a
tick it re-fires only while there's open work (`continue`), otherwise it idles to the heartbeat and
waits on events. Two rules:

- **The agent declares its own pace.** Each tick writes `state/tick-status.json`
  `{"status":"continue"}` if real work remains, else `{"status":"idle"}`. Idle parks it on the
  heartbeat (still instant-wake on events). This is "tick yourself only when there's work."
- **A manager/orchestrator agent (e.g. a fleet master) should be event-driven only** — always `idle`, never a
  continuous Opus loop. Its heavy/continuous work is delegated to off-Opus worker agents.

## Anti-patterns (do not reintroduce)
- ❌ "Each tick reconstructs state from the whole vault." → read the pre-built digest instead.
- ❌ Inlining the memory index or reference docs into `CLAUDE.md`. → retrieve via qmd/digest.
- ❌ `--resume`/`--continue` to "save context" in a loop. → it re-bills a growing transcript.
- ❌ A continuous backlog loop on Opus, or a recurring Opus cron/heartbeat. → off-Opus + event-driven.

## Where each law lives in code
- Lean prompt + digest pre-build: `runtime.sh` (the `claude -p` invocation + `memory.py digest`),
  `memory.py::digest`/`recall`/`_qmd_query`, each template's `CLAUDE.md` + `tick.txt`.
- Off-Opus continuous: `route_tier.py`, `route_brain.py` + `policy.json`, the template `agent.env`
  (`BRAIN` / `ROUTER` / `MODEL_ROUTINE`), the in-container supervisor (`agentloop._ensure_supervisor`).
- Event-driven self-pacing: `agentloop.py` (`due`, `_after`, `_read_tick_status`, `_has_open_work`).
