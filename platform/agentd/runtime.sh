#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# runtime.sh — fires ONE headless tick for an assembled agent, on the host.
# Generalized from the host runner (same hardening: cap-guard + stale-
# lock reclaim + heartbeat + deadman gap-log). Spec-driven, not hardcoded.
#
#   bash platform/agentd/runtime.sh platform/agents/<id>
#
# Runs on a PERSISTENT host (the Mac that runs the launchd timers + the bridges),
# NOT the disposable container (which can't run launchctl). One launchd timer per
# agent calls this; see `agentctl.py install-timer`.
# ──────────────────────────────────────────────────────────────────────────
set -o pipefail   # NOT -u (nounset): a long-running autonomous loop must tolerate an unset var, not silently abort a tick before it starts
AGENT_DIR="$(cd "${1:?usage: runtime.sh <agent-dir>}" && pwd)"; export AGENT_DIR   # hooks (event_log) read this
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # capture BEFORE cd into AGENT_DIR
# agent.env = DEFAULTS only (fill a var only if unset) so a pre-set env — the daemon CMD's
# `-e` overrides, agentloop's TICK_REASON, or an ops override — WINS, consistent with the
# Dockerfile CMD. (AGENT_ID MODEL MODEL_ROUTINE ROUTER INTERVAL_SECONDS PERMISSION … TOOLS_ROOT)
while IFS='=' read -r k v; do case "$k" in (''|\#*) ;; (*) [ -z "${!k+x}" ] && export "$k=$v";; esac; done < "$AGENT_DIR/agent.env"
# Fleet-wide defaults (optional, lower precedence than agent.env / the daemon env): ONE place for
# cross-agent knobs like STUDIO_WEEKLY_BUDGET_USD. A tracked .conf (NOT *.env — those are gitignored
# as secrets) so the knob is versioned/backed-up. Same defaults-only merge (only fills an unset var).
FLEET_CONF="$SCRIPT_DIR/fleet.conf"; [ -f "$FLEET_CONF" ] || FLEET_CONF="${TOOLS_ROOT:-/workspace}/platform/agentd/fleet.conf"
[ -f "$FLEET_CONF" ] && while IFS='=' read -r k v; do case "$k" in (''|\#*) ;; (*) [ -z "${!k+x}" ] && export "$k=$v";; esac; done < "$FLEET_CONF"
# Work from the ACTUAL agent dir (in-container this is /agent; a host smoke-test resolves
# to the host path). REPO_ROOT in agent.env documents the in-container mount point.
cd "$AGENT_DIR" || exit 1
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG="$AGENT_DIR/logs/runner.log"
USAGE_LOG="$AGENT_DIR/state/usage.jsonl"   # per-tick first-party usage (usage_capture.py)
# REAL out-of-pocket spend ledger (out-of-pocket $: image-gen + paid LLM routing). Exported so ANY tool
# the brain runs in-tick (tools/image/gen.py, tools/llm/route.mjs) appends here; the dashboard's
# "External LLM spend" card sums it. Distinct from usage.jsonl (Claude subscription, cap-bound).
[ -z "${SPEND_LOG:-}" ] && export SPEND_LOG="$AGENT_DIR/state/api_spending.jsonl"
CAP="$SCRIPT_DIR/usage_capture.py"; [ -f "$CAP" ] || CAP="${TOOLS_ROOT:-/workspace}/platform/agentd/usage_capture.py"
FEEDER="$(dirname "$CAP")/tick_feeder.py"   # stream-json stdin feeder + graduated budget INJECTOR + cutoff
HEARTBEAT="$AGENT_DIR/state/.heartbeat"
LOCK="${TMPDIR:-/tmp}/agent-${AGENT_ID}.lock"
MIN_CAP_REMAINING="${STUDIO_MIN_CAP_REMAINING:-35}"
# Session (5h block) floor, P1: STUDIO_SESSION_LIMIT_PCT_FLOOR is the % of the 5h limit USED at which
# we defer — the percent-of-ceiling framing (operator, 2026-06-13). Converted to the legacy "% remaining"
# knob the cap-guard below already uses (floor 65%-used ≡ 35%-remaining). If unset, the old knob stands.
if [ -n "${STUDIO_SESSION_LIMIT_PCT_FLOOR:-}" ] && [ "${STUDIO_SESSION_LIMIT_PCT_FLOOR:-0}" -gt 0 ] 2>/dev/null; then
  MIN_CAP_REMAINING=$(( 100 - STUDIO_SESSION_LIMIT_PCT_FLOOR ))
fi
STALE_LOCK_SECS="${AGENT_STALE_LOCK_SECS:-2700}"     # 45m > any real tick
log(){ echo "$(date -u +%FT%TZ) — [$AGENT_ID] $*" >> "$LOG"; }
_mtime(){ stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# Deadman / gap detector — a silent death shows up loudly on the next fire.
NOW="$(date +%s)"
if [ -f "$HEARTBEAT" ]; then
  GAP=$(( NOW - $(cat "$HEARTBEAT" 2>/dev/null || echo "$NOW") ))
  [ "$GAP" -gt $(( 2 * ${INTERVAL_SECONDS:-10800} )) ] 2>/dev/null && \
    log "⚠ GAP: ${GAP}s ($((GAP/3600))h) since last fire — was DOWN, resuming"
fi
echo "$NOW" > "$HEARTBEAT"

# Stale-lock reclaim (can't wedge permanently after a killed tick).
if ! mkdir "$LOCK" 2>/dev/null; then
  AGE=$(( NOW - $(_mtime "$LOCK") ))
  if [ "$AGE" -gt "$STALE_LOCK_SECS" ] 2>/dev/null; then
    log "stale lock (age ${AGE}s) — reclaiming"; rmdir "$LOCK" 2>/dev/null || rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { log "could not reclaim lock, skip"; exit 75; }   # 75 = DEFERRED (agentloop re-queues)
  else
    log "previous tick running (lock age ${AGE}s), skip"; exit 75                     # 75 = DEFERRED, not done
  fi
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

# Soft pause (control-plane, 2026-06-13): a WARM hold the operator toggles from the dashboard. The
# container + loop stay up but every tick no-ops until state/paused is removed (resume) — covers both
# Claude and local pods. Hard stop (frees the container) = agentctl down / launcher /stop.
if [ -f "$AGENT_DIR/state/paused" ]; then
  log "paused (state/paused present) — skipping tick"; exit 75   # 75 = DEFERRED (agentloop re-queues)
fi

# Web-chat model picker (state/model.override): the operator can switch the model live from the chat
# UI. Highest precedence for this tick, across ALL brains (read before the local/api branches below).
# The value is the brain's model id, assigned as a plain string (never executed); the writer
# (web_chat.py) only persists ids from its known allowlist.
OVR="$AGENT_DIR/state/model.override"
if [ -f "$OVR" ]; then
  _m="$(head -1 "$OVR" 2>/dev/null | tr -d '[:space:]')"
  if [ -n "$_m" ]; then export MODEL="$_m" BRAIN_MODEL="$_m"; log "model override (web chat) → $_m"; fi
fi

# BRAIN=local (D-061 Phase-2, offline): drive the tick with the LOCAL model brain instead of the
# Claude Code harness — runs off the Anthropic cap entirely (skip the cap-guard below) and keeps the
# agent working offline. Same assembled package (CLAUDE.md/tick.txt/memory/qmd/guard hooks/secrets);
# only the brain differs. Judgment escalates to an external reasoning API, not the interactive session.
if [ "${BRAIN:-claude}" = "local" ]; then
  # Pre-load recall (same as the Claude path) so a fresh tick doesn't re-derive the world.
  MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
  [ -f "$MEM" ] && { mkdir -p "$AGENT_DIR/state"; python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true; }
  LA="$SCRIPT_DIR/local_agent.py"; [ -f "$LA" ] || LA="${TOOLS_ROOT:-/workspace}/platform/agentd/local_agent.py"
  log "tick start (brain=local, model=${LOCAL_BRAIN_MODEL:-policy-default}, guard=on)"
  ROLE="${ROLE:-}" GUARD_HOOK="${GUARD_HOOK:-}" python3 "$LA" "$AGENT_DIR" >> "$LOG" 2>&1
  rc=$?
  [ "$rc" -ne 0 ] && log "tick error (exit $rc)"
  log "tick end"
  exit 0
fi

# BRAIN=api: drive the tick with ANY OpenAI-compatible API endpoint (NVIDIA NIM, DeepSeek, Gemini, Mistral…).
# Runs off the Anthropic subscription entirely — use this for GCloud / non-Mac deployments where
# BRAIN=claude (subscription) and BRAIN=local (Mac MLX) are both unavailable.
# Uses the SAME local_agent.py ReAct harness + guard hooks; only the brain endpoint changes.
# Endpoint is fully generic: BRAIN_API_BASE (base URL) + BRAIN_MODEL (driver model) + BRAIN_API_KEY_ENV
# (NAME of the key var, default OPENROUTER_API_KEY — set NVIDIA_API_KEY for build.nvidia.com, XAI_API_KEY
# for xAI, etc.). Hard judgment escalates to ESCALATION_MODEL (defaults to the driver model, same endpoint).
# Budget guard: API_BUDGET_USD (default $10/agent) caps cumulative spend tracked in state/api_spending.jsonl.
if [ "${BRAIN:-claude}" = "api" ]; then
  API_BUDGET="${API_BUDGET_USD:-10.0}"
  # Read cumulative spend from log (fail-open: if log absent/corrupt, assume $0)
  SPENT="0"
  if [ -f "$AGENT_DIR/state/api_spending.jsonl" ]; then
    SPENT="$(python3 -c "
import sys, json, pathlib
try:
  lines = pathlib.Path('$AGENT_DIR/state/api_spending.jsonl').read_text().splitlines()
  print(sum(json.loads(l).get('usd',0) for l in lines if l.strip()))
except Exception: print(0)
" 2>/dev/null || echo 0)"
  fi
  if python3 -c "import sys; sys.exit(0 if float('${SPENT:-0}') < float('${API_BUDGET}') else 1)" 2>/dev/null; then
    log "api spend: \$${SPENT} of \$${API_BUDGET} budget used"
  else
    log "API budget cap: \$${SPENT} >= \$${API_BUDGET} — DEFER (raise API_BUDGET_USD to continue)"; exit 75
  fi
  # Resolve the API key by its CONFIGURED env-var name (BRAIN_API_KEY_ENV) — generic across providers
  # (OPENROUTER_API_KEY by default; NVIDIA_API_KEY / XAI_API_KEY / OPENAI_API_KEY / … for others). Look in
  # the live env first, then scan any scoped .secrets/*.env for "<NAME>=…" (same pattern as the optimize path).
  API_KEY_ENV="${BRAIN_API_KEY_ENV:-OPENROUTER_API_KEY}"
  API_KEY="$(printenv "$API_KEY_ENV" 2>/dev/null || true)"
  if [ -z "$API_KEY" ]; then
    for SDIR in "$AGENT_DIR/.secrets" "${TOOLS_ROOT:-/workspace}/.secrets"; do
      [ -d "$SDIR" ] || continue
      for SFILE in "$SDIR"/*.env; do
        [ -f "$SFILE" ] || continue
        API_KEY="$(grep "^${API_KEY_ENV}=" "$SFILE" 2>/dev/null | head -1 | cut -d= -f2-)"
        [ -n "$API_KEY" ] && break
      done
      [ -n "$API_KEY" ] && break
    done
  fi
  [ -z "$API_KEY" ] && log "WARN: $API_KEY_ENV not found (env or .secrets/*.env) — API calls will likely fail"
  # Pre-load recall (same as the Claude path)
  MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
  [ -f "$MEM" ] && { mkdir -p "$AGENT_DIR/state"; python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true; }
  LA="$SCRIPT_DIR/local_agent.py"; [ -f "$LA" ] || LA="${TOOLS_ROOT:-/workspace}/platform/agentd/local_agent.py"
  log "tick start (brain=api, model=${BRAIN_MODEL:-deepseek/deepseek-chat}, key=$API_KEY_ENV, guard=on)"
  LOCAL_BRAIN_BASE="${BRAIN_API_BASE:-https://openrouter.ai/api/v1}" \
  LOCAL_BRAIN_MODEL="${BRAIN_MODEL:-deepseek/deepseek-chat}" \
  LOCAL_BRAIN_KEY="${API_KEY:-}" \
  ESCALATION_BASE="${ESCALATION_BASE:-${BRAIN_API_BASE:-https://openrouter.ai/api/v1}}" \
  ESCALATION_MODEL="${ESCALATION_MODEL:-${BRAIN_MODEL:-deepseek/deepseek-chat}}" \
  ESCALATION_KEY="${ESCALATION_KEY:-${API_KEY:-}}" \
  LOCAL_MAX_TOKENS="${LOCAL_MAX_TOKENS:-8192}" \
  LOCAL_MAX_STEPS="${LOCAL_MAX_STEPS:-32}" \
  LOCAL_REQ_TIMEOUT="${LOCAL_REQ_TIMEOUT:-120}" \
  SPEND_LOG="$AGENT_DIR/state/api_spending.jsonl" \
  ROLE="${ROLE:-}" GUARD_HOOK="${GUARD_HOOK:-}" python3 "$LA" "$AGENT_DIR" >> "$LOG" 2>&1
  rc=$?
  [ "$rc" -ne 0 ] && log "tick error (exit $rc)"
  log "tick end"
  exit 0
fi

# BRAIN=optimize (adaptive cost router, D-072): start on Claude (subscription — free at the margin),
# shift to the cheapest REACHABLE OpenAI-compatible pool in policy.json as the 5h/7d cap fills.
# route_brain.py prints ONE decision line; we either fall through to the Claude path (export MODEL)
# or drive the tick with local_agent.py on the chosen pool. Fail-OPEN to Claude — never breaks a tick.
if [ "${BRAIN:-claude}" = "optimize" ]; then
  RB="$SCRIPT_DIR/route_brain.py"; [ -f "$RB" ] || RB="${TOOLS_ROOT:-/workspace}/platform/agentd/route_brain.py"
  DECISION="$(MODEL="${MODEL:-claude-opus-4-8}" MODEL_ROUTINE="${MODEL_ROUTINE:-claude-sonnet-4-6}" \
              python3 "$RB" "$AGENT_DIR" --reason "${TICK_REASON:-heartbeat}" 2>>"$LOG" || echo "claude ${MODEL:-claude-opus-4-8}")"
  set -- $DECISION
  if [ "$1" = "pool" ]; then
    POOL_BASE="$2"; POOL_KEY_ENV="$3"; POOL_MODEL="$4"
    # Resolve the pool's API key from env or any scoped secrets/*.env by its env-var name.
    POOL_KEY="$(printenv "$POOL_KEY_ENV" 2>/dev/null || true)"
    if [ -z "$POOL_KEY" ]; then
      for SDIR in "$AGENT_DIR/.secrets" "${TOOLS_ROOT:-/workspace}/.secrets"; do
        [ -d "$SDIR" ] || continue
        for SFILE in "$SDIR"/*.env; do
          [ -f "$SFILE" ] || continue
          POOL_KEY="$(grep "^${POOL_KEY_ENV}=" "$SFILE" 2>/dev/null | head -1 | cut -d= -f2-)"
          [ -n "$POOL_KEY" ] && break
        done
        [ -n "$POOL_KEY" ] && break
      done
    fi
    [ -z "$POOL_KEY" ] && POOL_KEY="x"   # local servers accept any key
    MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
    [ -f "$MEM" ] && { mkdir -p "$AGENT_DIR/state"; python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true; }
    LA="$SCRIPT_DIR/local_agent.py"; [ -f "$LA" ] || LA="${TOOLS_ROOT:-/workspace}/platform/agentd/local_agent.py"
    log "tick start (brain=optimize→pool, model=$POOL_MODEL @ $POOL_BASE, guard=on)"
    LOCAL_BRAIN_BASE="$POOL_BASE" \
    LOCAL_BRAIN_MODEL="$POOL_MODEL" \
    LOCAL_BRAIN_KEY="$POOL_KEY" \
    ESCALATION_BASE="$POOL_BASE" \
    ESCALATION_MODEL="$POOL_MODEL" \
    ESCALATION_KEY="$POOL_KEY" \
    LOCAL_MAX_TOKENS="${LOCAL_MAX_TOKENS:-8192}" \
    LOCAL_MAX_STEPS="${LOCAL_MAX_STEPS:-32}" \
    LOCAL_REQ_TIMEOUT="${LOCAL_REQ_TIMEOUT:-120}" \
    SPEND_LOG="$AGENT_DIR/state/api_spending.jsonl" \
    ROLE="${ROLE:-}" GUARD_HOOK="${GUARD_HOOK:-}" python3 "$LA" "$AGENT_DIR" >> "$LOG" 2>&1
    rc=$?
    [ "$rc" -ne 0 ] && log "tick error (exit $rc)"
    log "tick end"
    exit 0
  fi
  # "claude <model>" → run the Claude path below with route_brain's chosen model.
  export MODEL="${2:-${MODEL:-claude-opus-4-8}}"
  log "brain=optimize → claude $MODEL (cap has headroom)"
fi

# ── Subscription-ceiling guard (P1, 2026-06-13) — REAL %-of-limit, the numbers `claude /status` shows ──
# PRIMARY source = Claude's own unified rate-limit headers (claude_usage.py, cached). Usage credits are
# OFF, so the fleet must stay strictly UNDER the subscription limit — we throttle on % of the ceiling
# (NOT dollars), deferring as either window nears the wall. Two windows: 5h session + 7d weekly (resets
# Tue ~12:59 local). Floors default 90% (warn 70/85). All fail-OPEN: a metrics glitch never wedges a tick.
USAGE_GUARDED=0
USAGE_HELPER="$SCRIPT_DIR/claude_usage.py"; [ -f "$USAGE_HELPER" ] || USAGE_HELPER="${TOOLS_ROOT:-/workspace}/platform/agentd/claude_usage.py"
if [ -f "$USAGE_HELPER" ]; then
  GUARD_OUT="$(python3 "$USAGE_HELPER" guard \
      --session-floor "${STUDIO_SESSION_LIMIT_PCT_FLOOR:-90}" --weekly-floor "${STUDIO_WEEKLY_LIMIT_PCT_FLOOR:-90}" \
      --session-warn "${STUDIO_SESSION_WARN_PCT:-70}" --weekly-warn "${STUDIO_WEEKLY_WARN_PCT:-85}" 2>/dev/null)"
  GRC=$?
  [ -n "$GUARD_OUT" ] && log "$GUARD_OUT"
  [ "$GRC" -eq 75 ] && exit 75                         # 75 = DEFERRED (agentloop re-queues)
  [ "$GRC" -ne 66 ] && USAGE_GUARDED=1                 # 66 = helper had no reading → fall back to ccusage
fi

# FALLBACK 5h cap guard (ccusage local block data) — only when the real-% helper gave no reading
# (no token / offline / no headers). Uses ccusage's own block token-limit; STUDIO_MIN_CAP_REMAINING /
# STUDIO_SESSION_LIMIT_PCT_FLOOR set the % threshold (see MIN_CAP_REMAINING derivation above).
if [ "$USAGE_GUARDED" = 0 ] && [ "$MIN_CAP_REMAINING" -gt 0 ] 2>/dev/null; then
  REMAIN="$(npx -y ccusage@latest blocks --json --token-limit max 2>/dev/null \
    | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const j=JSON.parse(s);const b=(j.blocks||[]).find(x=>x.isActive);if(!b){console.log(100);return;}const l=(b.tokenLimitStatus||{}).limit,t=b.totalTokens;console.log(l&&t!=null?Math.max(0,Math.round((1-t/l)*100)):100);}catch(e){console.log(100);}});' 2>/dev/null)"
  REMAIN="${REMAIN:-100}"
  if [ "$REMAIN" -lt "$MIN_CAP_REMAINING" ] 2>/dev/null; then
    log "session cap guard (fallback): 5h block ${REMAIN}% remaining (used $((100-REMAIN))% ≥ $((100-MIN_CAP_REMAINING))% floor) — DEFER"; exit 75
  elif [ "$REMAIN" -le 15 ] 2>/dev/null; then
    log "session WARN (fallback): 5h block ${REMAIN}% remaining (≥85% used)"
  fi
fi

# Per-agent share cap (P1, opt-in AGENT_WEEKLY_SHARE_PCT) — stops ONE hot pod monopolising the fleet's
# weekly quota: defer just this agent when its week-to-date tokens are ≥ X% of FLEET consumption. Pure
# attribution from usage.jsonl (no API/ceiling needed), and only with ≥2 active agents so a solo agent
# never self-defers. Fail-OPEN.
AG_SHARE="${AGENT_WEEKLY_SHARE_PCT:-0}"
if [ -n "$AG_SHARE" ] && [ "$AG_SHARE" != "0" ] 2>/dev/null; then
  USG="$SCRIPT_DIR/usage.py"; [ -f "$USG" ] || USG="${TOOLS_ROOT:-/workspace}/platform/agentd/usage.py"
  if [ -f "$USG" ]; then
    AG_SHARE_PCT="$(python3 "$USG" --fleet --window wtd 2>/dev/null \
      | AGENT_ID="$AGENT_ID" python3 -c 'import sys,json,os
try:
  d=json.load(sys.stdin); ags=d.get("agents",{})
  me=ags.get(os.environ["AGENT_ID"],{})
  print(me.get("share_pct",-1) if len(ags)>=2 else -1)
except Exception: print(-1)' 2>/dev/null)"
    AG_SHARE_PCT="${AG_SHARE_PCT:--1}"
    # integer compare (strip decimals); -1 / parse-fail → skip (fail-open)
    if [ "${AG_SHARE_PCT%.*}" -ge "$AG_SHARE" ] 2>/dev/null; then
      log "agent share cap: ${AGENT_ID} at ${AG_SHARE_PCT}% of fleet weekly tokens ≥ ${AG_SHARE}% — DEFER this agent"; exit 75
    fi
  fi
fi

# Permission mode → claude flags.
case "${PERMISSION:-acceptEdits}" in
  dangerous)  PERM=(--dangerously-skip-permissions) ;;
  allowlist)  PERM=(--permission-mode default --allowedTools "Read Edit Write Bash Glob Grep WebFetch WebSearch") ;;
  *)          PERM=(--permission-mode acceptEdits) ;;
esac

# The agent works from its data dir but RUNS the tools baked at /workspace —
# put both in scope.
ADD_DIRS=(--add-dir "$AGENT_DIR")
[ -d /workspace ] && [ "$AGENT_DIR" != /workspace ] && ADD_DIRS+=(--add-dir /workspace)
# The WORK_DIR project tree (mounted at /work) is in scope too — so the agent can work on it AND
# Claude Code discovers the working folder's .claude/skills (e.g. a project's own skill set). See docs/WORK-DIR.md.
[ -d /work ] && [ "$AGENT_DIR" != /work ] && ADD_DIRS+=(--add-dir /work)

# Memory recall (P3): pre-load the agent's open work + most-relevant past memory into
# state/recall.md so a context-wiped tick doesn't re-derive the world (agents forget — lean
# on durable files). Best-effort; the agent also recalls semantically (qmd MCP) in-tick.
MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
if [ -f "$MEM" ]; then
  mkdir -p "$AGENT_DIR/state"
  python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true
fi

# Housekeeping (continuous mode makes stores grow fast): rotate the runner log + a daily memory
# compaction. FULLY ISOLATED in a subshell with `|| true` so it can NEVER abort the tick (a bug here
# must not stop the agent from working).
(
  LOGMAX="${RUNNER_LOG_MAX_LINES:-2500}"
  if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt "$LOGMAX" ]; then
    tail -n "$LOGMAX" "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" && log "rotated runner.log → last ${LOGMAX} lines"
  fi
  # usage.jsonl trailing-line cap (NOT monthly file-rotation: a monthly archive would drop the
  # late-previous-month ticks that a 7d/wtd window straddling the 1st still needs). A generous cap
  # keeps every window (max 7d) intact while bounding the file. ~5000 lines ≫ any real month of ticks.
  UMAX="${USAGE_LOG_MAX_LINES:-5000}"
  if [ -f "$USAGE_LOG" ] && [ "$(wc -l < "$USAGE_LOG" 2>/dev/null || echo 0)" -gt "$UMAX" ]; then
    tail -n "$UMAX" "$USAGE_LOG" > "$USAGE_LOG.tmp" 2>/dev/null && mv "$USAGE_LOG.tmp" "$USAGE_LOG" && log "rotated usage.jsonl → last ${UMAX} lines"
  fi
  CSTAMP="$AGENT_DIR/state/.last-compact"
  if [ -f "$MEM" ] && { [ ! -f "$CSTAMP" ] || [ "$(( NOW - $(_mtime "$CSTAMP") ))" -gt "${COMPACT_EVERY_SECS:-86400}" ]; }; then
    OUT="$(python3 "$MEM" --base "$AGENT_DIR" compact 2>>"$LOG")" && [ -n "$OUT" ] && log "$OUT"
    : > "$CSTAMP"
  fi
) >/dev/null 2>&1 || true

# Model-tier router (P2, D-071): downgrade routine/mechanical ticks to a cheaper model,
# reserve the top MODEL for judgment. Safe-by-default — ROUTER!=on, no router, or any error
# → the configured top MODEL. TICK_REASON/TICK_TIER are passed by agentloop.
MODEL_EFF="$MODEL"
if [ "${ROUTER:-off}" = "on" ]; then
  RT="$SCRIPT_DIR/route_tier.py"; [ -f "$RT" ] || RT="${TOOLS_ROOT:-/workspace}/platform/agentd/route_tier.py"
  if [ -f "$RT" ]; then
    PICK="$(python3 "$RT" "$AGENT_DIR" --reason "${TICK_REASON:-heartbeat}" --model "$MODEL" \
            --routine "${MODEL_ROUTINE:-sonnet}" ${TICK_TIER:+--forced "$TICK_TIER"} 2>>"$LOG")"
    [ -n "$PICK" ] && MODEL_EFF="$PICK"
  fi
fi

# qmd MCP (P3): give the agent semantic recall over the whole workspace (incl. its committed
# memory). --strict-mcp-config = exactly this config, nothing ambient. Skipped if no .mcp.json.
MCP=()
[ -f "$AGENT_DIR/.mcp.json" ] && MCP=(--mcp-config "$AGENT_DIR/.mcp.json" --strict-mcp-config)

# P4 guardrails: a PreToolUse hook (in .claude/settings.json) that blocks git / foreign-secret
# reads / live-publish — fires even under --dangerously-skip-permissions. Additive to settings.
SET=()
[ -f "$AGENT_DIR/.claude/settings.json" ] && SET=(--settings "$AGENT_DIR/.claude/settings.json")

# MAX_TURNS (optional, unset = no cap): bound the agentic turns in ONE tick. A structural guard against a
# tick that probe-storms or grinds — it wraps up and the loop continues next tick (smaller ticks = leaner
# context + no 40-min runaways). Tune per agent in agent.env; leave unset for unbounded ticks.
# TICK_TIMEOUT (default 40m) hard-caps a tick so a hung `claude -p` can't block the loop. macOS has NO
# GNU `timeout`; coreutils provides `gtimeout`. Use whichever exists, else run claude DIRECTLY (the
# per-agent lock + stale-lock reclaim still prevent a permanent wedge). Earlier this line died with
# `timeout: command not found` (exit 127) every tick — that's why nothing ran.
TO="$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)"

# Warm-session continuity (WARM_SESSION, default ON for BRAIN=claude): a tick RESUMES the same
# conversation instead of cold-starting, so the agent KEEPS its working memory across ticks — a tick
# becomes pause+resume, not a wipe. Pinned per-agent id (separate from chat_responder's own --resume
# sessions). Validated: --resume carries full context. Mechanism mirrors chat_responder.py.
WORK_SID_FILE="$AGENT_DIR/state/work-session.id"
# Agent-driven session lifecycle (ECC file-handoff + /clear): the agent runs WARM within a unit of work,
# but when it finishes a coherent unit and has banked state to durable files, it CLEARS its context so the
# session can't grow forever — it decides when, not a timer. It signals by writing {"session":"clear"}
# (or "fresh"/"reset"/"new") in state/tick-status.json; we then drop the pinned id so THIS tick starts a
# fresh session and reconstructs from the files. (The agent may also just rm this file itself.)
if [ -f "$WORK_SID_FILE" ] && [ -f "$AGENT_DIR/state/tick-status.json" ] \
   && grep -qiE '"session"[[:space:]]*:[[:space:]]*"(clear|fresh|reset|new)"' "$AGENT_DIR/state/tick-status.json" 2>/dev/null; then
  log "agent signalled session CLEAR (unit complete) — dropping warm context, fresh session this tick"
  rm -f "$WORK_SID_FILE"
fi
# SAFETY NET (occupancy floor): the ctx_budget HOOK (mid-tick warn) + the agent's `session:clear` are the
# PRIMARY controls. This is the net for when the agent IGNORES the warning. Uses the SAME metric the hook
# uses — the last turn's context OCCUPANCY from state/.ctx-budget.json — so it only fires on real overrun.
# If occupancy blew past the global hard floor (CTX_HARD_TOKENS, default 300k), auto-clear → next tick lean.
if [ -f "$WORK_SID_FILE" ] && [ -f "$AGENT_DIR/state/.ctx-budget.json" ] && [ "${CTX_HARD_TOKENS:-300000}" -gt 0 ]; then
  CTX="$(python3 -c "import json;print(int(json.load(open('$AGENT_DIR/state/.ctx-budget.json')).get('ctx_tokens',0) or 0))" 2>/dev/null || echo 0)"
  if [ "${CTX:-0}" -gt "${CTX_HARD_TOKENS:-300000}" ]; then
    log "context occupancy ${CTX} > ${CTX_HARD_TOKENS:-300000} hard floor — agent didn't self-clear; auto-clearing (fresh session next tick)"
    rm -f "$WORK_SID_FILE"
  fi
fi
# SAFETY NET ($ cost floor): same idea as the occupancy net above, but on cumulative session COST. A warm
# --resume re-charges the whole context cache on turn 1, so a long-lived session can arrive already at/over
# the hard $ budget BEFORE any work — the ctx_budget hook then blocks every work tool (including the Write
# that would signal session:clear): a deadlock the agent CANNOT escape (it burns ~$ doing nothing, forever).
# If the last turn's cumulative cost_est is at/over the hard $ cap, auto-clear → this tick starts a FRESH
# (cheap) session and reconstructs from handoff.md. Makes lean-resume self-healing, not agent-dependent.
if [ -f "$WORK_SID_FILE" ] && [ -f "$AGENT_DIR/state/.ctx-budget.json" ]; then
  read -r COST_OVER COST_NOW <<EOF
$(python3 -c "import json
try: c=float(json.load(open('$AGENT_DIR/state/.ctx-budget.json')).get('cost_est',0) or 0)
except Exception: c=0.0
h=float('${CTX_COST_HARD_USD:-4.5}')
print(('1' if c>=h else '0'), round(c,2))" 2>/dev/null || echo "0 0")
EOF
  if [ "${COST_OVER:-0}" = "1" ]; then
    log "session cumulative cost \$${COST_NOW} ≥ hard \$${CTX_COST_HARD_USD:-4.5} on resume (cache-rewarm deadlock) — auto-clearing → fresh lean session this tick"
    rm -f "$WORK_SID_FILE"
  fi
fi
SESS=()
if [ "${BRAIN:-claude}" = "claude" ] && [ "${WARM_SESSION:-1}" != "0" ]; then
  if [ -s "$WORK_SID_FILE" ]; then
    SESS=(--resume "$(cat "$WORK_SID_FILE")")
  else
    WSID="$(python3 -c 'import uuid;print(uuid.uuid4())' 2>/dev/null)"
    [ -n "$WSID" ] && { printf '%s\n' "$WSID" > "$WORK_SID_FILE"; SESS=(--session-id "$WSID"); }
  fi
fi
# COST-CUTOFF watchdog (ENFORCEMENT). The ctx_budget hook only WARNS — the agent can (and does) ignore it.
# This background watchdog polls the live cost (state/.ctx-budget.json) and, when it hits the hard budget
# (the agent's planned hard_usd, clamped to the CTX_COST_HARD_USD floor), KILLS the tick. The post-tick
# block then clears the session → next tick resumes lean from the handoff. The guarantee, agent-independent.
rm -f "$AGENT_DIR/state/.cost-cutoff"; CW_PID=""
# INJECT mode: graduated budget WARNINGS as injected user messages (the agent OBEYS them — proven) + a
# kill backstop, both owned by tick_feeder.py (it writes the prompt via a FIFO as claude's stream-json
# stdin). When on, it REPLACES the bash watchdog below (the feeder does the kill). Default on when the
# feeder + capture parser are present; INJECT_BUDGET=0 falls back to positional-prompt + bash watchdog.
INJECT=""
if [ "${INJECT_BUDGET:-1}" != "0" ] && [ -f "$FEEDER" ] && [ -f "$CAP" ]; then INJECT=1; fi
if [ -z "$INJECT" ] && [ "${COST_CUTOFF:-1}" != "0" ] && [ -f "$CAP" ]; then
  ( while sleep 8; do
      if python3 -c "
import json,sys
floor=float('${CTX_COST_HARD_USD:-3.5}'); hmax=float('${CTX_COST_HARD_MAX:-6.0}')
try:
 cost=float((json.load(open('$AGENT_DIR/state/.ctx-budget.json')) or {}).get('cost_est',0) or 0)
except Exception: sys.exit(1)
try:
 hard=min(max(float(json.load(open('$AGENT_DIR/state/budget.json')).get('hard_usd') or floor), floor), hmax)
except Exception: hard=floor
sys.exit(0 if cost>=hard else 1)
" 2>/dev/null; then
        touch "$AGENT_DIR/state/.cost-cutoff"
        log "COST CUTOFF — live spend hit the hard budget; killing the tick (resumes lean from handoff)"
        pkill -TERM -f "claude -p" 2>/dev/null; sleep 2; pkill -KILL -f "claude -p" 2>/dev/null
        break
      fi
    done ) & CW_PID=$!
fi
log "tick start (model=$MODEL_EFF, perm=$PERMISSION, mcp=${MCP:+qmd}, guard=${SET:+on}, timeout=${TO:+on}, usage=${CAP:+on}, session=${SESS:+warm}, cutoff=${CW_PID:+on}${INJECT:+, inject=on})"
FEED_PID=""
if [ -n "$INJECT" ]; then
  # INJECT path: prompt is delivered as a stream-json user message via a FIFO by tick_feeder.py, which
  # then injects graduated budget warnings (obeyed) + a kill backstop. claude reads the FIFO as stdin.
  FIFO="$AGENT_DIR/state/.tick-fifo"; rm -f "$FIFO"; mkfifo "$FIFO" 2>/dev/null
  CTX_COST_SOFT_USD="${CTX_COST_SOFT_USD:-2.0}" CTX_COST_HARD_USD="${CTX_COST_HARD_USD:-3.5}" \
  python3 "$FEEDER" --fifo "$FIFO" --prompt-file "$AGENT_DIR/tick.txt" --state "$AGENT_DIR/state" \
      --soft-floor "${CTX_COST_SOFT_USD:-2.0}" --hard-floor "${CTX_COST_HARD_USD:-3.5}" \
      --grace "${CTX_STOP_GRACE_SEC:-60}" 2>>"$LOG" & FEED_PID=$!
  ${TO:+$TO -k 30 ${TICK_TIMEOUT:-2400}} claude -p --input-format stream-json \
    --append-system-prompt "$(cat "$AGENT_DIR/CLAUDE.md")" \
    --model "$MODEL_EFF" \
    "${SESS[@]}" \
    ${MAX_TURNS:+--max-turns $MAX_TURNS} \
    --output-format stream-json --verbose \
    "${ADD_DIRS[@]}" \
    "${MCP[@]}" \
    "${SET[@]}" \
    "${PERM[@]}" <"$FIFO" 2>>"$LOG" \
    | MODEL_EFF="$MODEL_EFF" python3 "$CAP" --agent "$AGENT_ID" --reason "${TICK_REASON:-heartbeat}" \
        --model "$MODEL_EFF" --out "$USAGE_LOG" >> "$LOG"
  rc=${PIPESTATUS[0]}
  kill "$FEED_PID" 2>/dev/null; rm -f "$FIFO"
elif [ -f "$CAP" ]; then
  # Usage-accounting path (P1): stream-json → usage_capture.py. The parser renders the turn into
  # runner.log (no log regression) AND appends this tick's first-party usage to state/usage.jsonl.
  # claude's stderr still tees to runner.log; PIPESTATUS[0] keeps claude's OWN rc (not the parser's).
  ${TO:+$TO -k 30 ${TICK_TIMEOUT:-2400}} claude -p "$(cat "$AGENT_DIR/tick.txt")" \
    --append-system-prompt "$(cat "$AGENT_DIR/CLAUDE.md")" \
    --model "$MODEL_EFF" \
    "${SESS[@]}" \
    ${MAX_TURNS:+--max-turns $MAX_TURNS} \
    --output-format stream-json --verbose \
    "${ADD_DIRS[@]}" \
    "${MCP[@]}" \
    "${SET[@]}" \
    "${PERM[@]}" </dev/null 2>>"$LOG" \
    | MODEL_EFF="$MODEL_EFF" python3 "$CAP" --agent "$AGENT_ID" --reason "${TICK_REASON:-heartbeat}" \
        --model "$MODEL_EFF" --out "$USAGE_LOG" >> "$LOG"
  rc=${PIPESTATUS[0]}
else
  # Fallback (capture script absent): original raw-text tick — no usage accounting, never blocks.
  ${TO:+$TO -k 30 ${TICK_TIMEOUT:-2400}} claude -p "$(cat "$AGENT_DIR/tick.txt")" \
    --append-system-prompt "$(cat "$AGENT_DIR/CLAUDE.md")" \
    --model "$MODEL_EFF" \
    "${SESS[@]}" \
    ${MAX_TURNS:+--max-turns $MAX_TURNS} \
    "${ADD_DIRS[@]}" \
    "${MCP[@]}" \
    "${SET[@]}" \
    "${PERM[@]}" </dev/null >> "$LOG" 2>&1
  rc=$?
fi
# stop the cost-cutoff watchdog; note if it fired (so a deliberate kill isn't logged as an error)
[ -n "$CW_PID" ] && kill "$CW_PID" 2>/dev/null
CUTOFF=""; [ -f "$AGENT_DIR/state/.cost-cutoff" ] && { CUTOFF=1; rm -f "$AGENT_DIR/state/.cost-cutoff"; }
[ "$rc" -eq 124 ] && log "tick TIMED OUT — killed after ${TICK_TIMEOUT:-2400}s (loop recovers)"
[ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && [ -z "$CUTOFF" ] && log "tick error (exit $rc)"
# COST CUTOFF fired → clear session (lean resume) + ensure a handoff exists for the fresh tick
if [ -n "$CUTOFF" ]; then
  log "tick cost-cut-off at the hard budget — clearing session for a lean resume next tick"
  rm -f "$WORK_SID_FILE"
  [ ! -s "$AGENT_DIR/state/handoff.md" ] && printf '# handoff.md (harness fallback — no agent handoff before cutoff)\nOBJECTIVE: see inbox.md (open directive).\nNOW-DOING: the previous tick was cost-cut-off mid-work.\nEXACT NEXT STEP: reconstruct from state/rollup.md (top) + work.json + recent git commits, then continue — and THIS tick, write state/budget.json {"package","soft_usd","hard_usd"} FIRST and keep state/handoff.md current as you go.\n' > "$AGENT_DIR/state/handoff.md"
fi
# Warm-session self-heal: if a --resume failed because the session is gone (e.g. .claude volume reset),
# clear the pinned id so the NEXT tick starts a fresh session instead of erroring forever.
if [ ${#SESS[@]} -gt 0 ] && [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] \
   && tail -40 "$LOG" 2>/dev/null | grep -qiE "no conversation found|session.*not found|no session with"; then
  log "work session not found — resetting (fresh session next tick)"; rm -f "$WORK_SID_FILE"
fi
# Reactive overflow recovery (OpenHands pattern): if a tick died because the context window actually
# overflowed, clear the session so the next tick starts fresh instead of re-resuming the over-full one.
if [ -f "$WORK_SID_FILE" ] && [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] \
   && tail -60 "$LOG" 2>/dev/null | grep -qiE "context.{0,12}(length|window).{0,12}(exceed|too|limit)|request_too_large|prompt is too long|conversation is too long|maximum context"; then
  log "context overflow detected — clearing session (fresh + lean next tick)"; rm -f "$WORK_SID_FILE"
fi
log "tick end"

# Auto-snapshot the vault so memory is saved BY DEFAULT (survives a machine wipe). Only if the
# operator made home/ a git vault (`enclave init`); SCAN-GATED (a leaked credential blocks the commit,
# never reaches history). The agent can't git (guard-blocked) — the runtime owns this commit, like the
# master owns commits. FULLY ISOLATED (subshell + redirects + `|| true`) so it can NEVER abort the loop.
if [ "${VAULT_SNAPSHOT:-1}" = "1" ] && [ -d "$AGENT_DIR/.git" ]; then
  ( python3 "$(dirname "$0")/vault_snapshot.py" snapshot "$AGENT_DIR" --msg "tick $(date -u +%FT%TZ)" \
      >> "$LOG" 2>&1 && log "vault snapshot ok" ) || log "vault snapshot skipped (blocked or no-op)"
fi
