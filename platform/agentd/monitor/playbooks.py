"""monitor/playbooks.py — the troubleshooting runbook.

Each Playbook is PURE DETECTION + DIAGNOSIS that EMITS AN INTENT (a control-spec dict) — it never
acts. The daemon decides, from MONITOR_MODE + policy, whether to enqueue that intent (control_watcher
executes) or merely describe it. This keeps detection physically separate from action.

A playbook = {match, diagnose, intent}:
  match(diag, home, snap, ctx)    -> bool        cheap anomaly-key check, then a deterministic signature
  diagnose(diag, home, snap, ctx) -> {cause, confidence, evidence, recommendation, source}
  intent(diag, home, snap, ctx)   -> control-spec dict | None    (None = suggest/alert only)

Signatures are host-readable after the fact (grep runner.log, check a file/config) — no in-container
hook, matching the diagnostics engine's "no runtime change" property. Seeded from the two real bugs
this session surfaced (broken bin/memory.py path; delegation-verify loop) plus the obvious liveness set.

HONESTY: a cause is asserted (confidence "high") only when a deterministic signature fires; otherwise
the playbook carries the diagnostics anomaly's own evidence + a lower confidence.
"""
import re
import socket
import pathlib
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class Playbook:
    key: str
    title: str
    severity: str                 # default; policy may override
    match: Callable
    diagnose: Callable
    intent: Callable
    safe_to_autofix: bool         # DEFAULT capability; policy allowlist still gates actual autofix


# --- signature helpers (pure, host-readable) -------------------------------------------------
def anomaly_keys(diag):
    return {a.get("key") for a in (diag.get("anomalies") or [])}


def anomaly(diag, key):
    for a in (diag.get("anomalies") or []):
        if a.get("key") == key:
            return a
    return None


def grep_runnerlog(home, pattern, tail=400):
    """Return the most recent runner.log line matching `pattern`, or None."""
    if not home:
        return None
    try:
        lines = (pathlib.Path(home) / "logs" / "runner.log").read_text(errors="ignore").splitlines()[-tail:]
    except Exception:
        return None
    rx = re.compile(pattern)
    for ln in reversed(lines):
        if rx.search(ln):
            return ln.strip()[:200]
    return None


def file_missing(home, rel):
    return bool(home) and not (pathlib.Path(home) / rel).exists()


def env_get(home, key):
    if not home:
        return None
    try:
        import fleet_config
        return fleet_config.read_config(home)["env"].get(key)
    except Exception:
        return None


def probe_port(host, port, timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except Exception:
        return False
    finally:
        s.close()


# --- the seed playbooks ----------------------------------------------------------------------

# (1) memory_path_broken — the brain's CLAUDE.md calls /agent/bin/memory.py but the shim is absent →
#     every recall/remember/work-list fails, the agent runs blind, errors inflate context. (This
#     session's bug; fixed durably in bin/enclave init, commit ad7680d.)
_MEM_SIG = r"memory\.py.*No such file|bin/memory\.py.*not found"

def _mem_match(diag, home, snap, ctx):
    # Gate on the CURRENT state: if the shim is present, the problem is fixed regardless of stale log
    # lines (else a paused agent whose log still holds the old error would alert forever). Shim missing
    # + corroborating evidence (the log signature or a failure anomaly) => real, now.
    if not file_missing(home, "bin/memory.py"):
        return False
    return bool(grep_runnerlog(home, _MEM_SIG)) or bool(anomaly_keys(diag) & {"tool_failures", "failures"})

def _mem_diag(diag, home, snap, ctx):
    line = grep_runnerlog(home, _MEM_SIG)
    return {"cause": "the brain's memory helper /agent/bin/memory.py is missing — every "
                     "recall/remember/work-list call fails, so the agent runs blind and the failures "
                     "inflate per-tick context",
            "confidence": "high",
            "evidence": line or "bin/memory.py absent + tool failures",
            "recommendation": "create the bin/memory.py shim (agent-init now writes it; see commit "
                              "ad7680d). For a live agent: drop a shim that execs "
                              "/workspace/platform/agentd/memory.py.",
            "source": "deterministic"}

_memory_path_broken = Playbook(
    "memory_path_broken", "Agent memory is broken (missing bin/memory.py)", "high",
    _mem_match, _mem_diag, lambda *_: None, safe_to_autofix=False)


# (2) delegation_loop — the delegation guard blocks the brain's own bulk edits while the cheap worker
#     fails verify → write→block→delegate→fail→retry, inflating turns/context/cost. (This session's
#     second bug.)
_DELEG_SIG = r"verify_failed|delegation-guard.*Bulk|delegation_guard"

def _deleg_match(diag, home, snap, ctx):
    # If the guard is already disabled, the loop can't be happening — don't alert on a stale log.
    if env_get(home, "DELEGATION_ENFORCE") == "off":
        return False
    return bool(anomaly_keys(diag) & {"tool_failures", "failures"}) and bool(grep_runnerlog(home, _DELEG_SIG))

def _deleg_diag(diag, home, snap, ctx):
    return {"cause": "the delegation guard is blocking the brain's own edits while the cheap worker "
                     "fails verify — a write→block→delegate→fail→retry loop that inflates "
                     "turns/context/cost",
            "confidence": "high",
            "evidence": grep_runnerlog(home, _DELEG_SIG),
            "recommendation": "set DELEGATION_ENFORCE=off for this agent (let the brain write directly) "
                              "or point its delegate worker at a stronger model.",
            "source": "deterministic"}

_delegation_loop = Playbook(
    "delegation_loop", "Delegation guard is causing a retry loop", "high",
    _deleg_match, _deleg_diag,
    intent=lambda *_: {"action": "set-config", "config": {"DELEGATION_ENFORCE": "off"}},
    safe_to_autofix=False)   # behavior change → suggest-only, never default-autofix


# (3) context_bloat — surface the diagnostics context anomalies with their own evidence/fix.
def _ctx_match(diag, home, snap, ctx):
    return bool(anomaly_keys(diag) & {"context_explosion", "prompt_creep"})

def _ctx_diag(diag, home, snap, ctx):
    an = anomaly(diag, "context_explosion") or anomaly(diag, "prompt_creep") or {}
    return {"cause": an.get("cause") or "context is growing / a large always-loaded state is re-sent "
                                        "every turn",
            "confidence": an.get("confidence", "med"),
            "evidence": an.get("evidence", ""),
            "recommendation": an.get("fix") or "compact memory / trim auto-loaded files & inbox.",
            "source": "deterministic"}

_context_bloat = Playbook(
    "context_bloat", "Context is bloating (expensive every tick)", "med",
    _ctx_match, _ctx_diag, lambda *_: None, safe_to_autofix=False)   # touches brain content → suggest


# (4) container_down — ran then exited unexpectedly (distinct from an operator stop / never-started).
def _down_match(diag, home, snap, ctx):
    return (not snap.get("up")) and "exited" in (snap.get("status", "") or "").lower()

def _down_diag(diag, home, snap, ctx):
    return {"cause": "the agent container exited unexpectedly",
            "confidence": "high", "evidence": f"status: {snap.get('status')}",
            "recommendation": "check logs/runner.log for the crash, then restart "
                              "(cd fleet/<id> && docker compose up -d).",
            "source": "deterministic"}

_container_down = Playbook(
    "container_down", "Agent container exited unexpectedly", "high",
    _down_match, _down_diag,
    intent=lambda *a: {"action": "restart"},
    safe_to_autofix=True)    # liveness/restart → autofix-CAPABLE (D3, behind the allowlist)


# (5) up_but_unreachable — container up but its chat port won't accept connections.
def _unreach_match(diag, home, snap, ctx):
    return bool(snap.get("up")) and ctx.get("reachable") is False

def _unreach_diag(diag, home, snap, ctx):
    return {"cause": "container is up but its chat port is not accepting connections",
            "confidence": "high", "evidence": f"port :{snap.get('port')} unreachable",
            "recommendation": "restart the agent.",
            "source": "deterministic"}

_up_but_unreachable = Playbook(
    "up_but_unreachable", "Agent up but chat port unreachable", "med",
    _unreach_match, _unreach_diag,
    intent=lambda *a: {"action": "restart"}, safe_to_autofix=True)


# (6) stalled — an autonomous (SUPERVISE=auto) agent that hasn't ticked in N hours.
def _stalled_match(diag, home, snap, ctx):
    if not snap.get("up") or env_get(home, "SUPERVISE") != "auto":
        return False
    last = snap.get("last_seen") or 0
    return last > 0 and (ctx.get("now", 0) - last) > ctx.get("no_tick_seconds", 6 * 3600)

def _stalled_diag(diag, home, snap, ctx):
    hrs = (ctx.get("now", 0) - (snap.get("last_seen") or 0)) / 3600.0
    return {"cause": "an autonomous agent has not produced a tick in a long time (possibly wedged)",
            "confidence": "med", "evidence": f"no rollup update in ~{hrs:.1f}h",
            "recommendation": "check runner.log for a stuck tool/loop; restart if wedged.",
            "source": "deterministic"}

_stalled = Playbook(
    "stalled", "Autonomous agent has stalled (no recent tick)", "med",
    _stalled_match, _stalled_diag,
    intent=lambda *a: {"action": "restart"}, safe_to_autofix=True)


# (7) kill_line — governance: an agent whose term-sheet kill-line date has passed is still running.
# Term sheet lives at <home>/state/term-sheet.json: {"kill_line": "YYYY-MM-DD", "kpi": ..., ...}.
# Discretion happens when the line is SET, never at execution time — a passed line always fires.
def _term_sheet(home):
    if not home:
        return None
    try:
        import json
        return json.loads((pathlib.Path(home) / "state" / "term-sheet.json").read_text())
    except Exception:
        return None

def _killline_match(diag, home, snap, ctx):
    if not snap.get("up"):
        return False
    ts = _term_sheet(home)
    line = (ts or {}).get("kill_line")
    if not line:
        return False
    import time as _t
    try:
        return _t.mktime(_t.strptime(line, "%Y-%m-%d")) + 86400 < ctx.get("now", 0)
    except Exception:
        return False

def _killline_diag(diag, home, snap, ctx):
    ts = _term_sheet(home) or {}
    return {"cause": f"term-sheet kill-line {ts.get('kill_line')} has passed and the agent is still running",
            "confidence": "high",
            "evidence": f"state/term-sheet.json kill_line={ts.get('kill_line')}; kpi={ts.get('kpi', '?')}",
            "recommendation": "stop the agent; the board decides renewal (a new kill-line) explicitly, never by drift.",
            "source": "deterministic"}

_kill_line = Playbook(
    "kill_line", "Term-sheet kill-line passed (agent still running)", "high",
    _killline_match, _killline_diag,
    intent=lambda *a: {"action": "down"}, safe_to_autofix=True)


# The per-agent runbook (bridge_down is a FLEET-level check the daemon runs separately — it has no
# single agent home to escalate into; full inbox surfacing is D2).
ALL = [_memory_path_broken, _delegation_loop, _context_bloat,
       _container_down, _up_but_unreachable, _stalled, _kill_line]

BY_KEY = {p.key: p for p in ALL}


def check_bridges(bridges_env):
    """Fleet-level: probe ENCLAVE_DOCTOR_BRIDGES ("name:host:port,…"). Returns [down-name,…]."""
    down = []
    for spec in (bridges_env or "").split(","):
        spec = spec.strip()
        if not spec or spec.count(":") < 2:
            continue
        name, host, port = spec.rsplit(":", 2)
        if not probe_port(host, port):
            down.append(name)
    return down
