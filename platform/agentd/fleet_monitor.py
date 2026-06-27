#!/usr/bin/env python3
"""fleet_monitor.py — the Agent SRE daemon. Watches every agent, detects when one is off, troubleshoots
to a root cause, and (per a configurable per-agent policy) alerts / suggests / auto-fixes.

OFF-OPUS BY CONSTRUCTION: a plain host-side poll loop importing the pure-stdlib `diagnostics` engine.
No Claude SDK in its dependency tree. The novel-error LLM fallback (D2) will use a cheap/local
endpoint only — never Opus.

PRIVILEGE SEPARATION (the keystone): this daemon DETECTS and ENQUEUES. It never touches docker or
`enclave`. When policy permits an autofix it writes a control-spec into control_watcher's queue;
control_watcher (a separate process, the only docker-capable actor) re-validates and executes. Detect
≠ act, communicating only through an auditable on-disk queue.

Alerts ride the EXISTING channel: a line appended to an agent's state/escalations.log shows up in the
dashboard's "⚠ Needs your decision" inbox — zero console changes.

Usage:
  fleet_monitor.py [<control-queue-dir>] [--interval SECONDS] [--once] [--dry-run]
    <control-queue-dir>  the SAME queue control_watcher drains (default $ENCLAVE_CONTROL_QUEUE)
    --interval           poll seconds (default 60, or $MONITOR_INTERVAL)
    --once               run one cycle and exit
    --dry-run            never enqueue; log + record would-be remediations ($MONITOR_DRYRUN=1)

Per-agent policy is MONITOR_MODE in agent.env (off|observe|alert|suggest|autofix); the fleet default
and the playbook tuning live in policies/monitor.json ($ENCLAVE_MONITOR_POLICY).
"""
import os
import sys
import json
import time
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet            # snapshot() — agent discovery incl. down/stopped/standalone
import diagnostics      # the detection layer (pure stdlib)
from monitor import playbooks
from monitor import state as mstate
from monitor import intel        # D2b: off-Opus LLM cause/fix for novel anomalies (fail-open)
from monitor import notify       # D2b: critical-alert push (Telegram), fail-open
from monitor.policy import Policy, MODES

LLM_CALLS_PER_CYCLE = int(os.environ.get("MONITOR_LLM_MAX_PER_CYCLE", "2"))  # bound cycle latency


def _iso(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def escalate(home, msg):
    """Append a monitor alert to the agent's escalations.log → the dashboard inbox. Same format the
    supervisor uses, tagged [monitor:<key>] so it's filterable + recognizable as our own."""
    if not home:
        return
    f = pathlib.Path(home) / "state" / "escalations.log"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as h:
            h.write(f"{_iso()} ESCALATE :: {msg}\n")
    except Exception:
        pass


def enqueue(control_queue, spec):
    """Stage a control-spec for control_watcher (the only docker-capable actor). We never act."""
    inc = pathlib.Path(control_queue) / "incoming"
    inc.mkdir(parents=True, exist_ok=True)
    name = f"monitor-{spec['agent']}-{spec['action']}-{int(time.time() * 1000)}.json"
    (inc / name).write_text(json.dumps(spec))


def heartbeat_path():
    """Where the daemon publishes its liveness + live-findings snapshot for the dashboard to read.
    Defaults next to the monitor state so the studio launcher's single env override covers both."""
    p = os.environ.get("ENCLAVE_MONITOR_HEARTBEAT")
    if p:
        return pathlib.Path(p).expanduser()
    return pathlib.Path(mstate.STATE_PATH).expanduser().parent / "monitor-heartbeat.json"


def write_heartbeat(hb, path=None):
    """Atomic-replace the heartbeat JSON (single writer = the daemon). Fail-soft: a heartbeat write
    must never take the monitor loop down."""
    path = pathlib.Path(path or heartbeat_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(hb, indent=1))
        os.replace(tmp, path)
    except Exception as e:
        print(f"[monitor] heartbeat write failed: {e}")


def push_critical(policy, st, aid, title, detail, now, log):
    """On a NEW high-severity alert, push a one-liner to Telegram (rate-limited, fail-open). Caller
    guarantees this is a fresh transition (decision==alert), so this fires once per problem, not per cycle."""
    if not (policy.push_enabled() and notify.available()):
        return
    if not st.push_ok(per_hour=policy.threshold("push_per_hour", 10), now=now):
        return
    if notify.push(f"🚨 {aid}: {title}\n{detail}"):
        log(f"[monitor] PUSHED critical {aid} — {title}")


def effective_mode(snap_entry, policy):
    m = playbooks.env_get(snap_entry.get("home"), "MONITOR_MODE")
    return m if m in MODES else policy.default_mode()


def probe_reachable(snap_entry):
    """None if not applicable (down/no port), else True/False from a quick loopback connect."""
    port = snap_entry.get("port")
    if not snap_entry.get("up") or not port:
        return None
    return playbooks.probe_port("127.0.0.1", port)


def cycle(policy, st, control_queue, now=None, dryrun=False, log=print):
    """One monitoring pass over the whole fleet. Fail-soft per agent — one bad agent never stops the rest.
    Returns a heartbeat snapshot (live findings per agent) — the daemon publishes it for the dashboard;
    tests ignore it (the return is side-effect-free)."""
    now = now or time.time()
    snap = fleet.snapshot()
    hb_agents, fleet_findings = {}, []
    llm_budget = [LLM_CALLS_PER_CYCLE if intel.available() else 0]   # bound novel-anomaly LLM calls/cycle

    # Fleet-level: host-bridge reachability (no single agent home to escalate into → log + state only in D1).
    down = playbooks.check_bridges(os.environ.get("ENCLAVE_DOCTOR_BRIDGES", ""))
    if down:
        cause = "host bridge(s) down: " + ", ".join(down)
        fleet_findings.append({"key": "bridge_down", "title": "Host bridge down", "severity": "med",
                               "cause": cause, "confidence": "high", "evidence": ", ".join(down),
                               "recommendation": "restart the bridge service(s) host-side"})
        if st.observe("_fleet", {"key": "bridge_down", "severity": "med", "cause": cause}, now) == "alert":
            log(f"[monitor] FLEET ALERT bridge(s) down: {', '.join(down)}")
    else:
        for k in st.reconcile_recoveries("_fleet", set(), now):
            log(f"[monitor] FLEET RECOVERED: {k}")

    for aid, a in snap.items():
        mode = effective_mode(a, policy)
        entry = {"mode": mode, "up": bool(a.get("up")), "status": a.get("status"),
                 "port": a.get("port"), "findings": []}
        hb_agents[aid] = entry
        if mode == "off":
            continue
        home = a.get("home")
        try:
            diag = diagnostics.from_home(home) if home else {}
        except Exception as e:
            diag = {}
            log(f"[monitor] {aid} diagnostics error: {e}")
        ctx = {"now": now, "reachable": probe_reachable(a),
               "no_tick_seconds": policy.threshold("no_tick_hours", 6) * 3600}

        active, findings = set(), []
        for pb in playbooks.ALL:
            if not policy.enabled(pb.key):
                continue
            try:
                if pb.match(diag, home, a, ctx):
                    findings.append((pb, pb.diagnose(diag, home, a, ctx),
                                     policy.severity(pb.key, pb.severity)))
                    active.add(pb.key)
            except Exception as e:
                log(f"[monitor] {aid} playbook {pb.key} error: {e}")

        for pb, dx, sev in findings:
            decision = st.observe(aid, {"key": pb.key, "severity": sev, "cause": dx.get("cause")}, now)
            # In SUGGEST mode, surface the playbook's remediation as a one-click Apply (the dashboard
            # drops it into the control queue → control_watcher executes). Only when the playbook is
            # actually capable of remediating (has an intent). autofix mode applies it automatically below.
            suggest_intent = None
            if mode == "suggest":
                try:
                    spec = pb.intent(diag, home, a, ctx)
                    if spec and spec.get("action"):
                        suggest_intent = spec
                except Exception:
                    suggest_intent = None
            # Per-agent mute: a chronic, known, non-actionable finding stays VISIBLE here but never
            # reaches the inbox/push (e.g. context_bloat on a deliberately tool-heavy agent).
            suppressed = policy.suppressed(aid, pb.key)
            entry["findings"].append({
                "key": pb.key, "title": pb.title, "severity": sev, "cause": dx.get("cause"),
                "confidence": dx.get("confidence"), "evidence": dx.get("evidence", ""),
                "recommendation": dx.get("recommendation"),
                "escalated": not (mode == "observe" or decision != "alert" or suppressed),
                "suppressed": suppressed,
                "intent": suggest_intent,
            })
            if mode == "observe" or decision != "alert" or suppressed:
                continue
            escalate(home, f"[monitor:{pb.key}] {aid} — {pb.title}: {dx['cause']} "
                           f"(confidence {dx['confidence']}; {dx.get('evidence', '')}). "
                           f"→ {dx['recommendation']}")
            log(f"[monitor] ALERT {aid} {pb.key} ({sev}/{dx['confidence']})")
            if sev == "high":
                push_critical(policy, st, aid, pb.title, f"{dx['cause']} → {dx['recommendation']}", now, log)
            # Remediation (D1: only when mode=autofix AND policy allowlists it AND the playbook is capable;
            # the default allowlist is empty, so this stays dormant until D3 enables it).
            if mode == "autofix" and pb.safe_to_autofix and policy.autofix_allowed(pb.key):
                _maybe_autofix(pb, dx, aid, a, home, diag, ctx, st, control_queue, policy, now, dryrun, log)

        # D2b — off-Opus LLM hypothesis for NOVEL high-sev anomalies (no playbook matched). Gated by
        # policy, cached per problem, and capped per cycle so it never dominates latency or spend.
        if policy.llm_enabled() and llm_budget[0] > 0:
            for an in (diag.get("anomalies") or []):
                if not intel.is_novel(an) or an["key"] in active:
                    continue
                fp = mstate.fingerprint(an.get("severity"), "llm_" + an["key"], an.get("evidence"))
                lf = st.llm_cached(aid, fp, now=now)
                if lf is None and llm_budget[0] > 0:
                    llm_budget[0] -= 1
                    lf = intel.hypothesize(aid, an, diag)
                    if lf is not None:
                        st.llm_store(aid, fp, lf, now=now)
                        log(f"[monitor] LLM hypothesis {aid} {lf['key']} (conf {lf['confidence']})")
                if not lf:
                    continue
                decision = st.observe(aid, {"key": lf["key"], "severity": lf["severity"],
                                            "cause": lf.get("cause")}, now)
                escalated = not (mode == "observe" or decision != "alert")
                entry["findings"].append({**lf, "intent": None, "escalated": escalated})
                active.add(lf["key"])
                if escalated:
                    escalate(home, f"[monitor:{lf['key']}] {aid} — {lf['title']}: {lf.get('cause')} "
                                   f"(LLM hypothesis, confidence {lf['confidence']}). → {lf.get('recommendation')}")
                    log(f"[monitor] ALERT {aid} {lf['key']} (llm/{lf['confidence']})")
                    if lf["severity"] == "high":
                        push_critical(policy, st, aid, lf["title"],
                                      f"{lf.get('cause')} → {lf.get('recommendation')} (LLM)", now, log)

        for k in st.reconcile_recoveries(aid, active, now):
            escalate(home, f"[monitor:{k}] {aid} — RECOVERED: the '{k}' condition has cleared.")
            log(f"[monitor] RECOVERED {aid} {k}")

    st.flush()
    agents_attn = sum(1 for e in hb_agents.values() if e["findings"]) + (1 if fleet_findings else 0)
    open_alerts = sum(len(e["findings"]) for e in hb_agents.values()) + len(fleet_findings)
    return {
        "ts": _iso(now), "epoch": float(now), "dryrun": bool(dryrun),
        "agents_scanned": len(snap), "agents_need_attention": agents_attn,
        "open_alerts": open_alerts, "fleet_findings": fleet_findings, "agents": hb_agents,
    }


def operator_stopped(home):
    """True if the operator deliberately took this agent down (state/.operator-stopped, written by the
    console's down action). The autofix path honours it so we never fight an intentional stop."""
    return bool(home) and (pathlib.Path(home) / "state" / ".operator-stopped").exists()


def _maybe_autofix(pb, dx, aid, a, home, diag, ctx, st, control_queue, policy, now, dryrun, log):
    # D3 safety gate: a lifecycle restart/up must never resurrect a pod the operator deliberately
    # stopped. (Other autofix classes — e.g. a config change — are unaffected.)
    spec0 = pb.intent(diag, home, a, ctx) or {}
    if spec0.get("action") in ("restart", "up") and operator_stopped(home):
        escalate(home, f"[monitor:{pb.key}] {aid} — down, but the operator stopped it deliberately "
                       f"(.operator-stopped present); NOT auto-restarting. Start it to clear this.")
        return
    if st.fixed_recently(aid, pb.key, now=now):
        escalate(home, f"[monitor:{pb.key}] {aid} — auto-fix already attempted recently and the "
                       f"problem persists; needs manual attention.")
        return
    if not st.rate_ok(aid, per_agent=policy.threshold("rate_per_agent_hour", 3), now=now):
        escalate(home, f"[monitor:{pb.key}] {aid} — remediation rate limit hit; manual attention.")
        return
    spec = pb.intent(diag, home, a, ctx)
    if not spec:
        return
    spec = {"agent": aid, "requested_by": "fleet_monitor", **spec}
    if dryrun:
        st.record_remediation(aid, pb.key, spec["action"], "autofix", "dryrun", now, dryrun=True)
        log(f"[monitor] DRYRUN would enqueue {spec['action']} for {aid}")
    else:
        enqueue(control_queue, spec)
        st.mark_remediated(aid, now)
        st.record_remediation(aid, pb.key, spec["action"], "autofix", "enqueued", now)
        log(f"[monitor] AUTOFIX enqueued {spec['action']} for {aid}")


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def main():
    args = sys.argv[1:]
    pos = [a for a in args if not a.startswith("-")]
    control_queue = pathlib.Path(
        pos[0] if pos else os.environ.get("ENCLAVE_CONTROL_QUEUE",
                                          str(pathlib.Path.home() / ".enclave" / "control"))
    ).expanduser().resolve()
    interval = float(_flag(args, "--interval", os.environ.get("MONITOR_INTERVAL", "60")))
    once = "--once" in args
    dryrun = "--dry-run" in args or os.environ.get("MONITOR_DRYRUN") == "1"
    st = mstate.MonitorState.load()
    hb_path = heartbeat_path()
    print(f"fleet_monitor: control_queue={control_queue} interval={interval}s once={once} "
          f"dryrun={dryrun} heartbeat={hb_path}")
    while True:
        policy = Policy.load()   # reload each cycle so policy edits take effect without a restart
        hb = {"ts": _iso(), "error": "cycle did not complete"}
        try:
            hb = cycle(policy, st, control_queue, dryrun=dryrun)
        except Exception as e:
            print(f"[monitor] cycle error: {e}")
            hb["error"] = str(e)
        hb.update({"interval_s": interval, "pid": os.getpid(), "once": once,
                   "default_mode": policy.default_mode()})
        write_heartbeat(hb, hb_path)
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
