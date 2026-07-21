#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# runtime.sh — fires ONE headless tick for an assembled agent, on the host.
# Generalized from the host runner (same hardening: cap-guard + stale-
# lock reclaim + heartbeat + deadman gap-log). Spec-driven, not hardcoded.
#
#   bash platform/agentd/runtime.sh <agent-home>
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
# Persistent work root (framework fix 2026-07-21): the agent's HOME ($AGENT_DIR, bind-mounted) is the
# ONLY guaranteed-persistent writable location. Container paths like /tmp or /workspace/work are
# EPHEMERAL and wiped on restart — an agent that builds product there loses it (demopod lost a
# whole CI-guardrail build this way). Guarantee a persistent work dir and tell the agent to use it.
mkdir -p "$AGENT_DIR/work" 2>/dev/null; export WORK_PERSIST="$AGENT_DIR/work"
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

# Effective-config report (N1, 2026-07-20) — writes state/effective-config.json: every mechanism
# ACTIVE|INACTIVE + why + per-var provenance, and logs any ACTIVE→INACTIVE flip since the last tick.
# The three worst framework failures were mechanisms silently inactive (dead preflight, fail-open
# brain, nullified router); this makes the MERGED config visible every tick. Never fails the tick.
EFFCFG="$SCRIPT_DIR/effective_config.py"; [ -f "$EFFCFG" ] || EFFCFG="${TOOLS_ROOT:-/workspace}/platform/agentd/effective_config.py"
if [ -f "$EFFCFG" ]; then
  ( OUT="$(python3 "$EFFCFG" "$AGENT_DIR" 2>>"$LOG")" && [ -n "$OUT" ] && log "$OUT" ) || true
fi

# Deadman / gap detector — a silent death shows up loudly on the next fire.
NOW="$(date +%s)"
if [ -f "$HEARTBEAT" ]; then
  GAP=$(( NOW - $(cat "$HEARTBEAT" 2>/dev/null || echo "$NOW") ))
  [ "$GAP" -gt $(( 2 * ${INTERVAL_SECONDS:-10800} )) ] 2>/dev/null && \
    log "⚠ GAP: ${GAP}s ($((GAP/3600))h) since last fire — was DOWN, resuming"
fi
echo "$NOW" > "$HEARTBEAT"

# Stale-lock reclaim (can't wedge permanently after a killed tick). PID-liveness first (2026-07-04
# review fix #9): an UNGRACEFULLY killed tick (SIGKILL/OOM) skips the EXIT trap and used to freeze the
# agent for up to STALE_LOCK_SECS (45 min) of mtime-based waiting. The lock now records its holder's
# PID — a dead holder is reclaimed IMMEDIATELY; a live holder is respected (never start a second
# claude beside a running tick; TICK_TIMEOUT bounds the real work).
if ! mkdir "$LOCK" 2>/dev/null; then
  AGE=$(( NOW - $(_mtime "$LOCK") ))
  LOCK_PID="$(cat "$LOCK/pid" 2>/dev/null)"
  if [ -n "$LOCK_PID" ] && ! kill -0 "$LOCK_PID" 2>/dev/null; then
    log "lock holder (pid $LOCK_PID) is DEAD — reclaiming immediately (was age ${AGE}s)"
    rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { log "could not reclaim lock, skip"; exit 75; }   # 75 = DEFERRED
  elif [ -n "$LOCK_PID" ]; then
    [ "$AGE" -gt "$STALE_LOCK_SECS" ] 2>/dev/null && \
      log "⚠ lock holder pid $LOCK_PID still ALIVE after ${AGE}s (> ${STALE_LOCK_SECS}s) — a tick may be wedged; NOT reclaiming a live process"
    log "previous tick running (lock age ${AGE}s, pid $LOCK_PID), skip"; exit 75      # 75 = DEFERRED, not done
  elif [ "$AGE" -gt "$STALE_LOCK_SECS" ] 2>/dev/null; then
    # legacy lock without a pid file — fall back to the old age-based reclaim
    log "stale lock (age ${AGE}s, no pid) — reclaiming"; rmdir "$LOCK" 2>/dev/null || rm -rf "$LOCK" 2>/dev/null
    mkdir "$LOCK" 2>/dev/null || { log "could not reclaim lock, skip"; exit 75; }
  else
    log "previous tick running (lock age ${AGE}s), skip"; exit 75
  fi
fi
echo $$ > "$LOCK/pid" 2>/dev/null
trap 'rm -rf "$LOCK" 2>/dev/null' EXIT

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

# ── Shared tick plumbing, ALL brains (2026-07-19 evaluation fix) ────────────────────────────────
# The three non-Claude brain paths used to `exit 0` unconditionally right after the tick, so the
# whole post-tick block — log/usage rotation, memory compaction, work-repo push, vault snapshot —
# was UNREACHABLE for BRAIN=api/local pods (both live pods never compacted memory). Every brain
# path now runs the same pre/post hooks; a wandered/errored tick also propagates a real exit code
# (agentloop only special-cases 75, so any other rc is telemetry, not a behavior change).
pre_tick_shared() {
  # Runtime-owned work-repo sync, pre-tick half (2026-07-04 review fix #10): the RUNTIME owns the
  # deploy key + all git NETWORK ops; the agent only commits locally and never touches the key.
  if [ -n "${WORK_GIT_DIR:-}" ] && [ -n "${WORK_GIT_KEY:-}" ] && [ -f "$WORK_GIT_KEY" ]; then
    (
      export GIT_SSH_COMMAND="ssh -i $WORK_GIT_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
      if [ ! -d "$WORK_GIT_DIR/.git" ]; then
        if [ -n "${WORK_GIT_URL:-}" ]; then
          git clone -q "$WORK_GIT_URL" "$WORK_GIT_DIR" >>"$LOG" 2>&1 \
            && log "work repo: cloned $WORK_GIT_URL" || log "work repo: clone FAILED (tick continues)"
        fi
      else
        git -C "$WORK_GIT_DIR" pull -q --ff-only >>"$LOG" 2>&1 || log "work repo: pull failed (dirty tree or diverged — tick continues)"
      fi
    ) || true
  fi
}

post_tick_shared() {
  # Completion contracts (N2, 2026-07-20): if this tick CLAIMED to serve a directive that carries a
  # contract (state/contracts.json), run the check — a failing check logs CLAIMED-NOT-VERIFIED and
  # escalates. Runs BEFORE scorecard so tick-status.json is still present. FULLY ISOLATED.
  CONTRACTS="$SCRIPT_DIR/contracts.py"; [ -f "$CONTRACTS" ] || CONTRACTS="${TOOLS_ROOT:-/workspace}/platform/agentd/contracts.py"
  if [ -f "$CONTRACTS" ]; then
    ( OUT="$(python3 "$CONTRACTS" "$AGENT_DIR" 2>>"$LOG")" && [ -n "$OUT" ] && log "$OUT" ) || true
  fi
  # L2 work-product scorecard (analytics plan P0): one zero-LLM record per tick — product vs
  # plumbing classification, churn, directive service. $NOW (script start) = the tick's t0.
  # FULLY ISOLATED; a scorecard bug never aborts the loop.
  SCORECARD="$SCRIPT_DIR/scorecard.py"; [ -f "$SCORECARD" ] || SCORECARD="${TOOLS_ROOT:-/workspace}/platform/agentd/scorecard.py"
  if [ -f "$SCORECARD" ]; then
    ( OUT="$(python3 "$SCORECARD" "$AGENT_DIR" --t0 "$NOW" 2>>"$LOG")" && [ -n "$OUT" ] && log "$OUT" ) || true
  fi
  # Housekeeping (continuous mode makes stores grow fast): rotate the runner log + usage.jsonl and
  # run the daily memory compaction. FULLY ISOLATED (subshell + || true) — can NEVER abort the loop.
  (
    MEMH="$AGENT_DIR/bin/memory.py"; [ -f "$MEMH" ] || MEMH="$SCRIPT_DIR/memory.py"
    LOGMAX="${RUNNER_LOG_MAX_LINES:-2500}"
    if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt "$LOGMAX" ]; then
      tail -n "$LOGMAX" "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG" && log "rotated runner.log → last ${LOGMAX} lines"
    fi
    # usage.jsonl trailing-line cap (NOT monthly file-rotation: a monthly archive would drop the
    # late-previous-month ticks that a 7d/wtd window straddling the 1st still needs).
    UMAX="${USAGE_LOG_MAX_LINES:-5000}"
    if [ -f "$USAGE_LOG" ] && [ "$(wc -l < "$USAGE_LOG" 2>/dev/null || echo 0)" -gt "$UMAX" ]; then
      tail -n "$UMAX" "$USAGE_LOG" > "$USAGE_LOG.tmp" 2>/dev/null && mv "$USAGE_LOG.tmp" "$USAGE_LOG" && log "rotated usage.jsonl → last ${UMAX} lines"
    fi
    CSTAMP="$AGENT_DIR/state/.last-compact"
    if [ -f "$MEMH" ] && { [ ! -f "$CSTAMP" ] || [ "$(( $(date +%s) - $(_mtime "$CSTAMP") ))" -gt "${COMPACT_EVERY_SECS:-86400}" ]; }; then
      OUT="$(python3 "$MEMH" --base "$AGENT_DIR" compact 2>>"$LOG")" && [ -n "$OUT" ] && log "$OUT"
      : > "$CSTAMP"
    fi
  ) >/dev/null 2>&1 || true
  # Runtime-owned work-repo sync, post-tick half: push the agent's local commits (runtime holds the
  # key). Skips clean; a failed push is logged and retried next tick. FULLY ISOLATED.
  if [ -n "${WORK_GIT_DIR:-}" ] && [ -n "${WORK_GIT_KEY:-}" ] && [ -f "$WORK_GIT_KEY" ] && [ -d "$WORK_GIT_DIR/.git" ]; then
    (
      export GIT_SSH_COMMAND="ssh -i $WORK_GIT_KEY -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
      AHEAD="$(git -C "$WORK_GIT_DIR" rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)"
      if [ "${AHEAD:-0}" -gt 0 ] 2>/dev/null; then
        git -C "$WORK_GIT_DIR" push -q >>"$LOG" 2>&1 \
          && log "work repo: pushed ${AHEAD} agent commit(s)" \
          || log "work repo: push FAILED (${AHEAD} commit(s) stay local; retry next tick)"
      fi
    ) || true
  fi
  # Auto-snapshot the vault so memory is saved BY DEFAULT (SCAN-GATED; runtime owns the commit).
  if [ "${VAULT_SNAPSHOT:-1}" = "1" ] && [ -d "$AGENT_DIR/.git" ]; then
    python3 "$SCRIPT_DIR/vault_snapshot.py" snapshot "$AGENT_DIR" --msg "tick $(date -u +%FT%TZ)" \
      >> "$LOG" 2>&1; VS_RC=$?
    # A BLOCK must never read like a no-op: scribepod's vault stopped committing for DAYS behind
    # the old "blocked or no-op" line. exit 3 = the secret gate refused; the brain is NOT backed up.
    case "$VS_RC" in
      0) log "vault snapshot ok" ;;
      3) log "vault snapshot BLOCKED — credential in tracked memory; brain NOT backed up (see log above)" ;;
      *) log "vault snapshot FAILED (rc=$VS_RC) — brain NOT backed up" ;;
    esac
  fi
}

finish_local_tick() {   # $1 = local_agent.py exit code — the shared tail for the local/api/pool paths
  case "$1" in
    0) ;;
    4) log "tick WANDERED to max_steps (rc=4; subtype=max_steps in state/usage.jsonl)" ;;
    *) log "tick error (exit $1)" ;;
  esac
  log "tick end"
  post_tick_shared
  exit "$1"
}

# ── Out-of-pocket spend cap, ALL brains (2026-07-04 review fix #8) ──────────────────────────────
# api_spending.jsonl is REAL money (image-gen, paid LLM routing, BRAIN=api ticks) regardless of which
# brain drives the tick — the old cap only guarded BRAIN=api, so a BRAIN=claude agent held the same
# metered key uncapped. WINDOWED (last 7d), not cumulative-forever: a lifetime sum can never reset, so
# it eventually bricks the agent no matter how modest the burn rate (the old $10 default was already
# permanently exceeded). At the wall: escalate LOUDLY (deduped 6h) + DEFER, never silently die.
# API_BUDGET_WEEKLY_USD (default 15); a legacy explicit API_BUDGET_USD is honored as the weekly value.
WEEKLY_CAP="${API_BUDGET_WEEKLY_USD:-${API_BUDGET_USD:-15}}"
if [ -n "$WEEKLY_CAP" ] && [ "$WEEKLY_CAP" != "0" ] && [ -f "$AGENT_DIR/state/api_spending.jsonl" ]; then
  WK_SPENT="$(python3 - "$AGENT_DIR/state/api_spending.jsonl" <<'PY' 2>/dev/null || echo 0
import sys, json, time, datetime
cut = time.time() - 7 * 86400
tot = 0.0
try:
    for l in open(sys.argv[1]):
        l = l.strip()
        if not l:
            continue
        try:
            r = json.loads(l)
        except Exception:
            continue
        ts = r.get("ts")
        try:
            e = float(ts) if isinstance(ts, (int, float)) else \
                datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if e >= cut:
            tot += float(r.get("usd") or 0)
except Exception:
    pass
print(round(tot, 2))
PY
)"
  if python3 -c "import sys; sys.exit(0 if float('${WK_SPENT:-0}') < float('${WEEKLY_CAP}') else 1)" 2>/dev/null; then
    log "external spend (7d): \$${WK_SPENT} of \$${WEEKLY_CAP} weekly cap"
    # Alarm LIFECYCLE (dashboard truth review T2, 2026-07-20): the runtime knows the moment the
    # condition clears (window rolled / cap raised) — write the resolution so the console's red
    # banner drains itself. A stale "ticks DEFER" alarm sat pinned on every tab for hours after
    # the cap was raised because only the RAISE side was ever automated.
    SPEND_STAMP="$AGENT_DIR/state/.spend-cap-alerted"
    if [ -f "$SPEND_STAMP" ]; then
      echo "$(date -u +%FT%TZ) NOTE :: RESOLVED [budget:external] ${AGENT_ID} — spend \$${WK_SPENT} is back under the \$${WEEKLY_CAP} weekly cap; ticks resume." >> "$AGENT_DIR/state/escalations.log"
      rm -f "$SPEND_STAMP"
    fi
  else
    log "EXTERNAL SPEND CAP: \$${WK_SPENT} ≥ \$${WEEKLY_CAP} in the last 7d — DEFER (raise API_BUDGET_WEEKLY_USD or wait for the window to roll)"
    SPEND_STAMP="$AGENT_DIR/state/.spend-cap-alerted"
    if [ ! -f "$SPEND_STAMP" ] || [ $(( NOW - $(_mtime "$SPEND_STAMP") )) -gt 21600 ] 2>/dev/null; then
      echo "$(date -u +%FT%TZ) ESCALATE :: [budget:external] ${AGENT_ID} — out-of-pocket spend \$${WK_SPENT} over the last 7d ≥ the \$${WEEKLY_CAP} weekly cap (state/api_spending.jsonl); ticks DEFER until the window rolls or the operator raises API_BUDGET_WEEKLY_USD." >> "$AGENT_DIR/state/escalations.log"
      : > "$SPEND_STAMP"
    fi
    exit 75
  fi
fi

# Deterministic capability PREFLIGHT (off-Opus, ALL brains) — FUNCTIONALLY verify the toolchain works
# BEFORE spending a model turn (writes state/capabilities.json; escalates broken required caps). Cached
# (24h / --force / tooling change). With PREFLIGHT_GATE=1 a broken REQUIRED capability DEFERS the tick
# (exit 75 → agentloop re-queues, zero tokens burned) instead of the model discovering + mis-diagnosing
# it mid-tick. ALWAYS runs (fixed 2026-07-20): tick.txt tells every agent to READ capabilities.json
# FIRST, and this used to sit below the brain dispatch gated on REQUIRES — so BRAIN=api/local pods
# never got the file at all (labpod polled for it 4x/tick), and unset REQUIRES silently skipped
# it everywhere else. With no REQUIRES it probes an advisory baseline (web, qmd) and never gates.
PREFLIGHT="$SCRIPT_DIR/preflight.py"; [ -f "$PREFLIGHT" ] || PREFLIGHT="${TOOLS_ROOT:-/workspace}/platform/agentd/preflight.py"
if [ -f "$PREFLIGHT" ]; then
  if REQUIRES="${REQUIRES:-}" python3 "$PREFLIGHT" --state "$AGENT_DIR/state" >>"$LOG" 2>&1; then :; else
    log "preflight: a REQUIRED capability is broken (see state/capabilities.json + state/escalations.log)"
    if [ "${PREFLIGHT_GATE:-0}" = "1" ]; then
      log "PREFLIGHT_GATE=1 → deferring this tick (no token burn) until the toolchain is fixed/escalation cleared"
      exit 75
    fi
  fi
fi

# BRAIN=local (D-061 Phase-2, offline): drive the tick with the LOCAL model brain instead of the
# Claude Code harness — runs off the Anthropic cap entirely (skip the cap-guard below) and keeps the
# agent working offline. Same assembled package (CLAUDE.md/tick.txt/memory/qmd/guard hooks/secrets);
# only the brain differs. Judgment escalates to an external reasoning API, not the interactive session.
if [ "${BRAIN:-claude}" = "local" ]; then
  pre_tick_shared
  # Pre-load recall (same as the Claude path) so a fresh tick doesn't re-derive the world.
  MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
  [ -f "$MEM" ] && { mkdir -p "$AGENT_DIR/state"; python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true; }
  LA="$SCRIPT_DIR/local_agent.py"; [ -f "$LA" ] || LA="${TOOLS_ROOT:-/workspace}/platform/agentd/local_agent.py"
  log "tick start (brain=local, model=${LOCAL_BRAIN_MODEL:-policy-default}, guard=on)"
  ROLE="${ROLE:-}" GUARD_HOOK="${GUARD_HOOK:-}" python3 "$LA" "$AGENT_DIR" >> "$LOG" 2>&1
  finish_local_tick $?
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
  # LOUD-FAIL on missing brain config (L-310): the old default silently ran a PAID model
  # (openrouter + deepseek/deepseek-chat) whenever BRAIN_MODEL was unset — both live pods burned
  # real money for a day behind docs claiming "$0". Undetectable fail-open is banned (D-100):
  # refuse the tick, escalate once per 6h, DEFER (75) so an operator fix resumes it cleanly.
  BSTAMP="$AGENT_DIR/state/.brain-config-alerted"
  if [ -z "${BRAIN_MODEL:-}" ]; then
    log "FATAL: BRAIN=api but BRAIN_MODEL is unset — refusing to default to a paid model. Set BRAIN_MODEL/BRAIN_API_BASE/BRAIN_API_KEY_ENV."
    if [ ! -f "$BSTAMP" ] || [ $(( NOW - $(_mtime "$BSTAMP") )) -gt 21600 ] 2>/dev/null; then
      echo "$(date -u +%FT%TZ) ESCALATE :: [brain:config] ${AGENT_ID} — BRAIN=api with BRAIN_MODEL unset; ticks DEFER until the pod env sets BRAIN_MODEL (+ BRAIN_API_BASE/BRAIN_API_KEY_ENV)." >> "$AGENT_DIR/state/escalations.log"
      : > "$BSTAMP"
    fi
    exit 75
  fi
  # Lifecycle: config fixed → resolve the alarm (T2).
  if [ -f "$BSTAMP" ]; then
    echo "$(date -u +%FT%TZ) NOTE :: RESOLVED [brain:config] ${AGENT_ID} — BRAIN_MODEL is set (${BRAIN_MODEL}); ticks resume." >> "$AGENT_DIR/state/escalations.log"
    rm -f "$BSTAMP"
  fi
  # Spend cap: handled by the brain-agnostic WEEKLY gate above (fix #8) — the old cumulative-forever
  # API_BUDGET_USD check lived here and permanently bricked an agent once lifetime spend crossed it.
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
  pre_tick_shared
  MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
  [ -f "$MEM" ] && { mkdir -p "$AGENT_DIR/state"; python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true; }
  LA="$SCRIPT_DIR/local_agent.py"; [ -f "$LA" ] || LA="${TOOLS_ROOT:-/workspace}/platform/agentd/local_agent.py"
  log "tick start (brain=api, model=${BRAIN_MODEL}, key=$API_KEY_ENV, guard=on)"
  LOCAL_BRAIN_BASE="${BRAIN_API_BASE:-https://openrouter.ai/api/v1}" \
  LOCAL_BRAIN_MODEL="${BRAIN_MODEL}" \
  LOCAL_BRAIN_KEY="${API_KEY:-}" \
  ESCALATION_BASE="${ESCALATION_BASE:-${BRAIN_API_BASE:-https://openrouter.ai/api/v1}}" \
  ESCALATION_MODEL="${ESCALATION_MODEL:-${BRAIN_MODEL}}" \
  ESCALATION_KEY="${ESCALATION_KEY:-${API_KEY:-}}" \
  LOCAL_MAX_TOKENS="${LOCAL_MAX_TOKENS:-8192}" \
  LOCAL_MAX_STEPS="${LOCAL_MAX_STEPS:-32}" \
  LOCAL_REQ_TIMEOUT="${LOCAL_REQ_TIMEOUT:-120}" \
  SPEND_LOG="$AGENT_DIR/state/api_spending.jsonl" \
  ROLE="${ROLE:-}" GUARD_HOOK="${GUARD_HOOK:-}" python3 "$LA" "$AGENT_DIR" >> "$LOG" 2>&1
  finish_local_tick $?
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
    finish_local_tick $?
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
# (exit 66: no token / offline / no headers / stale cache). Uses ccusage's own block token-limit;
# STUDIO_MIN_CAP_REMAINING / STUDIO_SESSION_LIMIT_PCT_FLOOR set the % threshold (see above).
# If ccusage ALSO has no data, the tick still proceeds (fail-open) but the blindness is ALARMED —
# an unguarded agent must be visible, not silent (2026-07-04 review fix #5).
if [ "$USAGE_GUARDED" = 0 ] && [ "$MIN_CAP_REMAINING" -gt 0 ] 2>/dev/null; then
  CC_RAW="$(npx -y ccusage@latest blocks --json --token-limit max 2>/dev/null || true)"
  if [ -z "$CC_RAW" ]; then
    log "⚠ SPEND-GUARD BLIND: subscription headers AND ccusage both unavailable — tick proceeds UNGUARDED"
    BLIND_STAMP="$AGENT_DIR/state/.guard-blind-alerted"
    if [ ! -f "$BLIND_STAMP" ] || [ $(( NOW - $(_mtime "$BLIND_STAMP") )) -gt 21600 ] 2>/dev/null; then
      echo "$(date -u +%FT%TZ) ESCALATE :: [guard:blind] ${AGENT_ID} — spend guard has NO usage reading (ratelimit-header probe + ccusage both failed); the agent is running with NO subscription ceiling. Check CLAUDE_CODE_OAUTH_TOKEN / network, then verify 'claude_usage.py fetch' works." >> "$AGENT_DIR/state/escalations.log"
      : > "$BLIND_STAMP"
    fi
  else
    REMAIN="$(printf '%s' "$CC_RAW" \
      | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const j=JSON.parse(s);const b=(j.blocks||[]).find(x=>x.isActive);if(!b){console.log(100);return;}const l=(b.tokenLimitStatus||{}).limit,t=b.totalTokens;console.log(l&&t!=null?Math.max(0,Math.round((1-t/l)*100)):100);}catch(e){console.log(100);}});' 2>/dev/null)"
    REMAIN="${REMAIN:-100}"
    if [ "$REMAIN" -lt "$MIN_CAP_REMAINING" ] 2>/dev/null; then
      log "session cap guard (fallback): 5h block ${REMAIN}% remaining (used $((100-REMAIN))% ≥ $((100-MIN_CAP_REMAINING))% floor) — DEFER"; exit 75
    elif [ "$REMAIN" -le 15 ] 2>/dev/null; then
      log "session WARN (fallback): 5h block ${REMAIN}% remaining (≥85% used)"
    else
      log "guard OK (ccusage fallback): 5h block ${REMAIN}% remaining"
    fi
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

# Runtime-owned work-repo sync, pre-tick half (2026-07-04 review fix #10; shared function — the
# agent only commits locally, guard.py blocks GIT_SSH_COMMAND, the runtime owns the key + network).
pre_tick_shared

# Memory recall (P3): pre-load the agent's open work + most-relevant past memory into
# state/recall.md so a context-wiped tick doesn't re-derive the world (agents forget — lean
# on durable files). Best-effort; the agent also recalls semantically (qmd MCP) in-tick.
MEM="$AGENT_DIR/bin/memory.py"; [ -f "$MEM" ] || MEM="$SCRIPT_DIR/memory.py"
if [ -f "$MEM" ]; then
  mkdir -p "$AGENT_DIR/state"
  python3 "$MEM" --base "$AGENT_DIR" digest > "$AGENT_DIR/state/recall.md" 2>>"$LOG" || true
fi

# (Preflight moved ABOVE the brain dispatch, 2026-07-20 — it ran only on the Claude path here, so
# BRAIN=api/local pods never got a capabilities.json no matter what they declared.)

# (Housekeeping — log/usage rotation + daily memory compaction — moved to post_tick_shared, which
# runs at the end of EVERY brain path, so non-Claude pods stopped being exempt from it.)

# Model-tier router (P2, D-071): downgrade routine/mechanical ticks to a cheaper model,
# reserve the top MODEL for judgment. Safe-by-default — ROUTER!=on, no router, or any error
# → the configured top MODEL. TICK_REASON/TICK_TIER are passed by agentloop.
MODEL_EFF="$MODEL"
if [ "${ROUTER:-off}" = "on" ]; then
  # Router-nullification tripwire: ROUTER=on with MODEL_ROUTINE==MODEL means every "cheap" tick still
  # runs the top model — a silent config error that cost weeks of Opus-on-heartbeat before it was seen.
  [ -n "${MODEL_ROUTINE:-}" ] && [ "$MODEL_ROUTINE" = "$MODEL" ] && \
    log "⚠ ROUTER NULLIFIED: MODEL_ROUTINE == MODEL ($MODEL) — routine ticks are NOT downgraded; set MODEL_ROUTINE to a cheaper tier"
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
# SAFETY NET ($ cost floor): cumulative session COST. A warm --resume re-charges the whole context cache on
# turn 1, so a long-lived session can arrive already OVER budget BEFORE any work — the ctx_budget hook then
# fires WARN1 ("wrap up, no new sub-task") immediately, so the agent can't open a HARD/multi-file task at all
# and does easy filler instead (the "avoid the difficult task" incentive, operator 2026-07-01). Clearing at
# the SOFT cap (not hard) kills that dead-zone: if the resumed session is already over SOFT it can't do
# meaningful work anyway, so drop it → this tick COLD-starts CHEAP (<soft) with FULL budget to advance the
# next tick-sized CHUNK of the top task (reconstructs from handoff.md). A hard task then gets DONE across
# fresh ticks; difficulty stops mattering. Bounds warm-session growth to ~soft. Self-healing, not agent-dependent.
if [ -f "$WORK_SID_FILE" ] && [ -f "$AGENT_DIR/state/.ctx-budget.json" ]; then
  read -r COST_OVER COST_NOW <<EOF
$(python3 -c "import json
try: c=float(json.load(open('$AGENT_DIR/state/.ctx-budget.json')).get('cost_est',0) or 0)
except Exception: c=0.0
h=float('${CTX_COST_SOFT_USD:-20.0}')
print(('1' if c>=h else '0'), round(c,2))" 2>/dev/null || echo "0 0")
EOF
  if [ "${COST_OVER:-0}" = "1" ]; then
    log "session cost \$${COST_NOW} ≥ soft \$${CTX_COST_SOFT_USD:-20.0} on resume — auto-clearing → fresh CHEAP tick with full budget to advance the next chunk (no dead-zone / no hard-task avoidance)"
    rm -f "$WORK_SID_FILE"
  fi
fi
# OPERATOR OVERRIDE (one-lever redirect): a NEW open [tier:top] inbox directive must WIN over a warm session +
# stale handoff/plan. Without this, the tick warm-resumes the previous unit of work and its handoff.md steers
# the OLD task, so a fresh operator directive is silently ignored (the "had to hand-sync 4 files" bug,
# 2026-07-01). Fix: if inbox.md has an OPEN `- [ ] … [tier:top] …` item AND inbox changed since we last acted
# on it (ack marker), drop the warm session so THIS tick COLD-starts and reconstructs from inbox/recall (which
# override the default priority). Fires ONCE per new directive (ack mtime), then resumes warm normally — so
# routine inbox notes don't thrash the session, only a top-priority redirect does. Result: operator writes
# ONLY inbox (dashboard Send / `enclave fleet send`); it always wins.
INBOX_F="$AGENT_DIR/inbox.md"; ACK_F="$AGENT_DIR/state/.inbox-override-acked"
if [ -f "$WORK_SID_FILE" ] && [ -f "$INBOX_F" ] \
   && grep -qiE '^- \[ \].*\[tier:top\]' "$INBOX_F" 2>/dev/null \
   && { [ ! -f "$ACK_F" ] || [ "$INBOX_F" -nt "$ACK_F" ]; }; then
  log "NEW [tier:top] inbox directive — dropping warm session so this tick cold-starts on the operator override"
  rm -f "$WORK_SID_FILE"
fi
[ -f "$INBOX_F" ] && touch "$ACK_F" 2>/dev/null || true
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
        # Scoped kill (fix #9): match THIS agent's claude (its cmdline carries --add-dir $AGENT_DIR).
        # A bare 'claude -p' pattern killed EVERY agent's in-flight tick when host-run multi-agent.
        pkill -TERM -f "claude .*$AGENT_DIR" 2>/dev/null; sleep 2; pkill -KILL -f "claude .*$AGENT_DIR" 2>/dev/null
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

# Post-tick housekeeping + work-repo push + vault snapshot (shared with every brain path).
post_tick_shared
