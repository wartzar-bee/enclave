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
import json
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
    # A PAUSED pod not ticking is the operator's intent, not a stall — this false-alarmed for 383h
    # on a deliberately-parked agent (dashboard truth review T3, 2026-07-20). The snapshot's tick
    # state and the state/paused flag are both honored (belt + suspenders across fleet versions).
    if snap.get("tick") == "paused":
        return False
    try:
        import pathlib as _pl
        if home and (_pl.Path(home) / "state" / "paused").exists():
            return False
    except Exception:
        pass
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


# --- L2 work-product playbooks (analytics plan P1, 2026-07-19) --------------------------------
# All tick-denominated (a continuous pod hits these within the hour, never "after days") and all
# read state/tick-scorecard.jsonl — written by the harness, never by the pod's own reasoning.

def _jsonl_tail(home, rel, n):
    import json as _json
    try:
        lines = (pathlib.Path(home) / rel).read_text(errors="ignore").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            out.append(_json.loads(ln))
        except Exception:
            continue
    return out


def _scored(home, n=10):
    """Last n scorecard records with a WORKING product config (config==ok)."""
    return [r for r in _jsonl_tail(home, "state/tick-scorecard.jsonl", n) if r.get("config") == "ok"]


# (8) zero_product — N consecutive scored ticks with zero PRODUCT writes while the pod runs green.
# The logan-cross failure shape: 56 healthy L1 ticks, output = its own status file. Fires at 10
# scored ticks (hours). For queue-gated agents whose no-op is by design (ideas-scout), suppress
# per-agent via policy — don't blunt the default.
def _product_is_external(home):
    """A pod whose product ships to an EXTERNAL platform writes no local artifact when it publishes.
    logan-cross posts chapters to Royal Road: 10 "zero product" ticks, while the chapters were live
    (HTTP 200) — and this rule recommends "consider pause + board review", i.e. it was one step from
    pausing a productive pod. Declared in the pod's own state/scorecard-config.json, so the default
    stays sharp for pods that genuinely produce nothing."""
    try:
        cfg = json.loads((pathlib.Path(home) / "state" / "scorecard-config.json").read_text())
    except Exception:
        return False
    return bool(cfg.get("product_measured_externally"))


def _zeroprod_match(diag, home, snap, ctx):
    if not snap.get("up") or _product_is_external(home):
        return False
    recs = _scored(home, 10)
    return len(recs) >= 10 and all((r.get("writes", {}).get("product") or 0) == 0 for r in recs)

def _zeroprod_diag(diag, home, snap, ctx):
    recs = _scored(home, 10)
    ss = sum(r.get("writes", {}).get("self_state") or 0 for r in recs)
    tl = sum(r.get("writes", {}).get("tooling") or 0 for r in recs)
    return {"cause": "10 consecutive scored ticks produced ZERO product artifacts (kpi_artifacts "
                     f"globs) — output was plumbing: {ss} self-state + {tl} tooling writes",
            "confidence": "high",
            "evidence": "state/tick-scorecard.jsonl last 10 records, writes.product all 0",
            "recommendation": "pod-capability signal, not a directive gap — review task shape vs "
                              "the KPI (can this agent close its loop?); consider pause + board review.",
            "source": "deterministic"}

_zero_product = Playbook(
    "zero_product", "Zero product output across 10 scored ticks", "high",
    _zeroprod_match, _zeroprod_diag, intent=lambda *a: None, safe_to_autofix=False)


# (9) churn_spike — the same non-product file rewritten ≥3× in one tick or ≥5× across 10 ticks
# (the 33×-rollup day, caught at rewrite #3). scorecard.py sets churn_alarm per record.
def _churn_match(diag, home, snap, ctx):
    return snap.get("up") and any(r.get("churn_alarm") for r in _jsonl_tail(home, "state/tick-scorecard.jsonl", 10))

def _churn_diag(diag, home, snap, ctx):
    worst, wn = None, 0
    for r in _jsonl_tail(home, "state/tick-scorecard.jsonl", 10):
        for p, n in (r.get("churn") or {}).items():
            if n > wn:
                worst, wn = p, n
    return {"cause": f"churn: '{worst}' rewritten {wn}× — the agent is spinning on its own files "
                     "instead of producing",
            "confidence": "high",
            "evidence": "state/tick-scorecard.jsonl churn/churn_alarm (last 10 records)",
            "recommendation": "inspect the churned file's purpose; usually a cancelled/blocked task "
                              "the agent keeps re-attempting — close it via directives.json, or park the channel.",
            "source": "deterministic"}

_churn_spike = Playbook(
    "churn_spike", "File-churn spike (same file rewritten repeatedly)", "medium",
    _churn_match, _churn_diag, intent=lambda *a: None, safe_to_autofix=False)


# (10) off_directive — 3 consecutive scored ticks served no active directive. Per the standing rule
# this escalates as a POD-CAPABILITY signal (evidence for the board), never as "write another directive".
def _offdir_match(diag, home, snap, ctx):
    if not snap.get("up"):
        return False
    recs = _scored(home, 3)
    if len(recs) < 3:
        return False
    def _off(r):
        so = r.get("serves_observed")
        return so is False or (not r.get("serves") and (r.get("writes", {}).get("product") or 0) == 0)
    return all(_off(r) for r in recs)

def _offdir_diag(diag, home, snap, ctx):
    return {"cause": "3 consecutive scored ticks wrote nothing that serves any ACTIVE directive",
            "confidence": "medium",
            "evidence": "state/tick-scorecard.jsonl serves/serves_observed, last 3 scored records",
            "recommendation": "pod-capability signal (standing rule: no new directive) — bring to "
                              "the board with the scorecard evidence; candidate for early kill/checkpoint.",
            "source": "deterministic"}

_off_directive = Playbook(
    "off_directive", "Ticks not serving any active directive (3 in a row)", "high",
    _offdir_match, _offdir_diag, intent=lambda *a: None, safe_to_autofix=False)


# (11) wander_rate — ≥3 of the last 5 work ticks exhausted the step cap (subtype=max_steps).
# fireforge lifetime rate was 65% and nothing fired; this fires the same afternoon.
def _wander_match(diag, home, snap, ctx):
    recs = [r for r in _jsonl_tail(home, "state/usage.jsonl", 15) if r.get("reason") != "chat"][-5:]
    return snap.get("up") and len(recs) >= 5 and \
        sum(1 for r in recs if r.get("subtype") == "max_steps") >= 3

def _wander_diag(diag, home, snap, ctx):
    return {"cause": "the brain wandered to the step cap in ≥3 of the last 5 ticks — researching "
                     "instead of producing (the anti-wander forcing function is not sufficient here)",
            "confidence": "high",
            "evidence": "state/usage.jsonl subtype=max_steps, last 5 non-chat records",
            "recommendation": "task shape or brain tier is wrong for this work: shrink the tick "
                              "brief, raise the judgment tier, or delegate the labour differently.",
            "source": "deterministic"}

_wander_rate = Playbook(
    "wander_rate", "Wandering to the step cap (3 of last 5 ticks)", "medium",
    _wander_match, _wander_diag, intent=lambda *a: None, safe_to_autofix=False)


# (12) self_certification — most 'done' work items carry NO verify command: the deterministic
# completion gate exists but is being bypassed by omission (self-certified doneness).
def _selfcert_match(diag, home, snap, ctx):
    if not snap.get("up"):
        return False                    # a stopped/archived pod's old work queue is history, not a finding
    import json as _json
    try:
        items = _json.loads((pathlib.Path(home) / "work.json").read_text())
    except Exception:
        return False
    done = [i for i in items if isinstance(i, dict) and i.get("status") == "done"][-20:]
    if len(done) < 5:
        return False
    unverified = sum(1 for i in done if not (i.get("verify") or "").strip())
    return unverified / len(done) > 0.5

def _selfcert_diag(diag, home, snap, ctx):
    return {"cause": ">50% of recently completed work items have no verify command — 'done' is "
                     "self-certified, the anti-fabrication gate is being bypassed by omission",
            "confidence": "high",
            "evidence": "work.json done items, empty verify field",
            "recommendation": "require verify commands at work-add time (constitution/skill nudge); "
                              "treat unverified dones as claims in board reports.",
            "source": "deterministic"}

_self_certification = Playbook(
    "self_certification", "Work marked done without verify commands", "medium",
    _selfcert_match, _selfcert_diag, intent=lambda *a: None, safe_to_autofix=False)


# (13) scorecard_blind — a GOVERNED pod (term-sheet present) with no working scorecard config:
# product output is not being measured at all. Loud-when-blind, enforced (D-100).
def _blind_match(diag, home, snap, ctx):
    if not snap.get("up") or not _term_sheet(home):
        return False
    recs = _jsonl_tail(home, "state/tick-scorecard.jsonl", 3)
    if not recs:                       # no scorecard yet (old image / first tick) — count as blind
        return (pathlib.Path(home) / "state" / "usage.jsonl").exists()
    return any(r.get("config") == "missing" for r in recs)

def _blind_diag(diag, home, snap, ctx):
    return {"cause": "governed pod (term sheet present) has no working scorecard config — product "
                     "output is UNMEASURED (blind), which reads as green while producing nothing",
            "confidence": "high",
            "evidence": "state/scorecard-config.json missing/empty or tick-scorecard.jsonl config=missing",
            "recommendation": "write state/scorecard-config.json (kpi_artifacts globs) — the spawn "
                              "gate requires it for new pods; this one predates it.",
            "source": "deterministic"}

_scorecard_blind = Playbook(
    "scorecard_blind", "Governed pod with unmeasured product output", "medium",
    _blind_match, _blind_diag, intent=lambda *a: None, safe_to_autofix=False)


# The per-agent runbook (bridge_down is a FLEET-level check the daemon runs separately — it has no
# single agent home to escalate into; full inbox surfacing is D2).
ALL = [_memory_path_broken, _delegation_loop, _context_bloat,
       _container_down, _up_but_unreachable, _stalled, _kill_line,
       _zero_product, _churn_spike, _off_directive, _wander_rate,
       _self_certification, _scorecard_blind]

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
