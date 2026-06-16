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
CAP="$SCRIPT_DIR/usage_capture.py"; [ -f "$CAP" ] || CAP="${TOOLS_ROOT:-/workspace}/platform/agentd/usage_capture.py"
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

# BRAIN=api: drive the tick with ANY OpenRouter-compatible API endpoint (DeepSeek, Gemini, Mistral…).
# Runs off the Anthropic subscription entirely — use this for GCloud / non-Mac deployments where
# BRAIN=claude (subscription) and BRAIN=local (Mac MLX) are both unavailable.
# Uses the SAME local_agent.py ReAct harness + guard hooks; only the brain endpoint changes.
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
  # Resolve OpenRouter API key from scoped secret mount or env
  if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    for SFILE in "$AGENT_DIR/.secrets/openrouter.env" "${TOOLS_ROOT:-/workspace}/.secrets/openrouter.env"; do
      [ -f "$SFILE" ] && OPENROUTER_API_KEY="$(grep '^OPENROUTER_API_KEY=' "$SFILE" 2>/dev/null | cut -d= -f2-)" && break
    done
  fi
  [ -z "${OPENROUTER_API_KEY:-}" ] && log "WARN: OPENROUTER_API_KEY not found — API calls will likely fail"
  # Pre-load recall (same as the Claude path)
  MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
  [ -f "$MEM" ] && { mkdir -p "$AGENT_DIR/state"; python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true; }
  LA="$SCRIPT_DIR/local_agent.py"; [ -f "$LA" ] || LA="${TOOLS_ROOT:-/workspace}/platform/agentd/local_agent.py"
  log "tick start (brain=api, model=${BRAIN_MODEL:-deepseek/deepseek-chat}, guard=on)"
  LOCAL_BRAIN_BASE="${BRAIN_API_BASE:-https://openrouter.ai/api/v1}" \
  LOCAL_BRAIN_MODEL="${BRAIN_MODEL:-deepseek/deepseek-chat}" \
  LOCAL_BRAIN_KEY="${OPENROUTER_API_KEY:-}" \
  ESCALATION_BASE="${ESCALATION_BASE:-https://openrouter.ai/api/v1}" \
  ESCALATION_MODEL="${ESCALATION_MODEL:-${BRAIN_MODEL:-deepseek/deepseek-chat}}" \
  ESCALATION_KEY="${OPENROUTER_API_KEY:-}" \
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

# TICK_TIMEOUT (default 40m) hard-caps a tick so a hung `claude -p` can't block the loop. macOS has NO
# GNU `timeout`; coreutils provides `gtimeout`. Use whichever exists, else run claude DIRECTLY (the
# per-agent lock + stale-lock reclaim still prevent a permanent wedge). Earlier this line died with
# `timeout: command not found` (exit 127) every tick — that's why nothing ran.
TO="$(command -v timeout 2>/dev/null || command -v gtimeout 2>/dev/null || true)"
log "tick start (model=$MODEL_EFF, perm=$PERMISSION, mcp=${MCP:+qmd}, guard=${SET:+on}, timeout=${TO:+on}, usage=${CAP:+on})"
if [ -f "$CAP" ]; then
  # Usage-accounting path (P1): stream-json → usage_capture.py. The parser renders the turn into
  # runner.log (no log regression) AND appends this tick's first-party usage to state/usage.jsonl.
  # claude's stderr still tees to runner.log; PIPESTATUS[0] keeps claude's OWN rc (not the parser's).
  ${TO:+$TO -k 30 ${TICK_TIMEOUT:-2400}} claude -p "$(cat "$AGENT_DIR/tick.txt")" \
    --append-system-prompt "$(cat "$AGENT_DIR/CLAUDE.md")" \
    --model "$MODEL_EFF" \
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
    "${ADD_DIRS[@]}" \
    "${MCP[@]}" \
    "${SET[@]}" \
    "${PERM[@]}" </dev/null >> "$LOG" 2>&1
  rc=$?
fi
[ "$rc" -eq 124 ] && log "tick TIMED OUT — killed after ${TICK_TIMEOUT:-2400}s (loop recovers)"
[ "$rc" -ne 0 ] && [ "$rc" -ne 124 ] && log "tick error (exit $rc)"
log "tick end"

# Auto-snapshot the vault so memory is saved BY DEFAULT (survives a machine wipe). Only if the
# operator made home/ a git vault (`enclave init`); SCAN-GATED (a leaked credential blocks the commit,
# never reaches history). The agent can't git (guard-blocked) — the runtime owns this commit, like the
# master owns commits. FULLY ISOLATED (subshell + redirects + `|| true`) so it can NEVER abort the loop.
if [ "${VAULT_SNAPSHOT:-1}" = "1" ] && [ -d "$AGENT_DIR/.git" ]; then
  ( python3 "$(dirname "$0")/vault_snapshot.py" snapshot "$AGENT_DIR" --msg "tick $(date -u +%FT%TZ)" \
      >> "$LOG" 2>&1 && log "vault snapshot ok" ) || log "vault snapshot skipped (blocked or no-op)"
fi
