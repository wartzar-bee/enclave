"""Hermetic tests for the fleet health monitor — no docker, no network, temp homes.

Covers: the two real bugs this session surfaced (broken bin/memory.py path; delegation-verify loop)
match with the right cause/confidence; liveness playbooks; the dedup state machine + recovery; and an
end-to-end cycle (snapshot + diagnostics monkeypatched) that writes the expected escalation lines and
never enqueues under --dry-run.

Run: python3 test_monitor.py   (exits non-zero on any failure)
"""
import os
import sys
import json
import time
import pathlib
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from monitor import playbooks
from monitor import state as mstate
from monitor.policy import Policy
import fleet_monitor


def check(name, cond):
    if cond:
        print(f"ok: {name}")
    else:
        print(f"FAIL: {name}")
        check.failed += 1
check.failed = 0


def mkhome(runner="", memory_shim=True, supervise="auto", env_extra=None):
    """A throwaway agent home with the bits the playbooks read."""
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "logs").mkdir()
    (d / "state").mkdir()
    (d / "logs" / "runner.log").write_text(runner)
    if memory_shim:
        (d / "bin").mkdir()
        (d / "bin" / "memory.py").write_text("# shim\n")
    env = f"AGENT_ID=x\nSUPERVISE={supervise}\n"
    for k, v in (env_extra or {}).items():
        env += f"{k}={v}\n"
    (d / "agent.env").write_text(env)
    return str(d)


CTX = {"now": time.time(), "reachable": True, "no_tick_seconds": 6 * 3600}
def snap(up=True, status="running", port=8888, home="", last_seen=None):
    return {"id": "x", "up": up, "status": status, "port": port, "home": home,
            "last_seen": last_seen if last_seen is not None else time.time()}


# --- (1) memory_path_broken: matches on the log signature, asserts high-confidence cause ----------
home = mkhome(runner="2026-06-26 python3: can't open file '/agent/bin/memory.py': No such file or directory\n",
             memory_shim=False)
pb = playbooks.BY_KEY["memory_path_broken"]
check("memory_path_broken matches the log signature", pb.match({}, home, snap(home=home), CTX))
dx = pb.diagnose({}, home, snap(home=home), CTX)
check("memory_path_broken cause is high-confidence + deterministic",
      dx["confidence"] == "high" and dx["source"] == "deterministic" and "memory" in dx["cause"].lower())
check("memory_path_broken is suggest-only (no intent, not autofix)",
      pb.intent({}, home, snap(home=home), CTX) is None and pb.safe_to_autofix is False)
# negative: a healthy home with the shim present + clean log does NOT match
ok_home = mkhome(runner="all good\n", memory_shim=True)
check("memory_path_broken does NOT match a healthy home", not pb.match({}, ok_home, snap(home=ok_home), CTX))


# --- (2) delegation_loop: tool_failures anomaly + verify_failed signature ------------------------
dl_home = mkhome(runner='{"status":"verify_failed"} delegation-guard: Bulk implementation must be DELEGATED\n')
diag_tf = {"anomalies": [{"key": "tool_failures", "severity": "high"}]}
pb = playbooks.BY_KEY["delegation_loop"]
check("delegation_loop matches (tool_failures + verify_failed)", pb.match(diag_tf, dl_home, snap(home=dl_home), CTX))
dx = pb.diagnose(diag_tf, dl_home, snap(home=dl_home), CTX)
check("delegation_loop recommends DELEGATION_ENFORCE=off",
      "DELEGATION_ENFORCE=off" in dx["recommendation"])
check("delegation_loop intent is set-config DELEGATION_ENFORCE=off, but suggest-only",
      pb.intent(diag_tf, dl_home, snap(home=dl_home), CTX)["config"]["DELEGATION_ENFORCE"] == "off"
      and pb.safe_to_autofix is False)
# negative: verify_failed in log but NO tool_failures anomaly → not a loop
check("delegation_loop needs the anomaly too", not pb.match({"anomalies": []}, dl_home, snap(home=dl_home), CTX))


# --- (3) context_bloat: surfaces the diagnostics anomaly's own evidence/fix ----------------------
diag_ctx = {"anomalies": [{"key": "context_explosion", "severity": "high",
                           "cause": "context grew sharply", "evidence": "~4M tokens", "fix": "compact memory"}]}
pb = playbooks.BY_KEY["context_bloat"]
check("context_bloat matches context_explosion", pb.match(diag_ctx, ok_home, snap(home=ok_home), CTX))
dx = pb.diagnose(diag_ctx, ok_home, snap(home=ok_home), CTX)
check("context_bloat carries the anomaly evidence + fix",
      dx["evidence"] == "~4M tokens" and "compact" in dx["recommendation"])


# --- (4) liveness: container_down / up_but_unreachable / stalled ---------------------------------
pb = playbooks.BY_KEY["container_down"]
check("container_down matches an exited container",
      pb.match({}, ok_home, snap(up=False, status="exited(137)", home=ok_home), CTX))
check("container_down does NOT match an operator-stopped (never-up) one",
      not pb.match({}, ok_home, snap(up=False, status="stopped", home=ok_home), CTX))
check("container_down is restart-capable (autofix candidate)", pb.safe_to_autofix is True)

pb = playbooks.BY_KEY["up_but_unreachable"]
check("up_but_unreachable matches up + unreachable",
      pb.match({}, ok_home, snap(up=True, home=ok_home), {**CTX, "reachable": False}))
check("up_but_unreachable does NOT match when reachable",
      not pb.match({}, ok_home, snap(up=True, home=ok_home), {**CTX, "reachable": True}))

pb = playbooks.BY_KEY["stalled"]
stale_home = mkhome(supervise="auto")
old = time.time() - 8 * 3600
check("stalled matches an auto agent with no recent tick",
      pb.match({}, stale_home, snap(up=True, home=stale_home, last_seen=old), CTX))
fresh_home = mkhome(supervise="auto")
check("stalled does NOT match a recently-ticked agent",
      not pb.match({}, fresh_home, snap(up=True, home=fresh_home, last_seen=time.time()), CTX))
off_home = mkhome(supervise="off")
check("stalled does NOT match a non-autonomous agent",
      not pb.match({}, off_home, snap(up=True, home=off_home, last_seen=old), CTX))


# --- (5) dedup state machine + recovery ----------------------------------------------------------
st = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s.json")
f = {"key": "tool_failures", "severity": "high", "cause": "loop"}
check("first observe -> alert", st.observe("a", f, now=1000) == "alert")
check("unchanged repeat -> suppress", st.observe("a", f, now=1001) == "suppress")
check("severity escalation -> alert again",
      st.observe("a", {**f, "severity": "high", "cause": "worse loop"}, now=1002) == "alert")
rec = st.reconcile_recoveries("a", set(), now=1003)   # no longer active
check("recovery transition fires", rec == ["tool_failures"])
check("re-occurrence after recovery -> alert", st.observe("a", f, now=1004) == "alert")
# rate limit + anti-loop
st.mark_remediated("b", now=2000)
st.record_remediation("b", "container_down", "restart", "autofix", "enqueued", now=2000)
check("fixed_recently true within window", st.fixed_recently("b", "container_down", now=2100) is True)
check("fixed_recently false outside window", st.fixed_recently("b", "container_down", now=2000 + 4000) is False)
for i in range(3):
    st.mark_remediated("c", now=3000 + i)
check("rate_ok false after 3 in window", st.rate_ok("c", per_agent=3, now=3005) is False)


# --- (6) end-to-end cycle: snapshot + diagnostics monkeypatched ----------------------------------
broken = mkhome(runner="python3: can't open file '/agent/bin/memory.py': No such file or directory\n",
                memory_shim=False)
healthy = mkhome(runner="tick end\n", memory_shim=True)
fake_snap = {"broken": {"id": "broken", "up": True, "status": "running", "port": 8801, "home": broken,
                        "last_seen": time.time()},
             "healthy": {"id": "healthy", "up": True, "status": "running", "port": 8802, "home": healthy,
                         "last_seen": time.time()}}
fleet_monitor.fleet.snapshot = lambda: fake_snap
fleet_monitor.diagnostics.from_home = lambda h: {"anomalies": [], "health": {}}
fleet_monitor.playbooks.probe_port = lambda host, port, timeout=0.4: True   # all reachable
os.environ["ENCLAVE_DOCTOR_BRIDGES"] = ""   # no bridges in the test

cq = pathlib.Path(tempfile.mkdtemp()) / "control"
st2 = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s2.json")
pol = Policy({"default_mode": "alert", "playbooks": {}, "autofix_allowlist": [], "thresholds": {}})
logs = []
fleet_monitor.cycle(pol, st2, cq, now=time.time(), dryrun=True, log=logs.append)

esc = (pathlib.Path(broken) / "state" / "escalations.log")
check("e2e: broken agent got an escalation", esc.exists() and "memory_path_broken" in esc.read_text())
check("e2e: healthy agent got NO escalation",
      not (pathlib.Path(healthy) / "state" / "escalations.log").exists())
check("e2e: dry-run enqueued nothing", not (cq / "incoming").exists() or not list((cq / "incoming").glob("*")))
check("e2e: an ALERT was logged", any("ALERT" in l and "broken" in l for l in logs))

# second cycle: same problem -> suppressed (no duplicate escalation line)
before = esc.read_text().count("memory_path_broken")
fleet_monitor.cycle(pol, st2, cq, now=time.time() + 1, dryrun=True, log=logs.append)
check("e2e: repeat cycle suppresses the duplicate", esc.read_text().count("memory_path_broken") == before)


# --- (6b) per-agent mute is symmetric: a SUPPRESSED finding leaks no RECOVERED line either ---------
# Regression for the context_bloat flapping: the ALERT edge honoured per_agent.suppress but the
# RECOVERED edge did not, so a muted playbook still spammed "… RECOVERED" every time its condition
# cleared. A non-suppressed agent must still get its RECOVERED line (the fix is targeted, not blanket).
broken_runner = "python3: can't open file '/agent/bin/memory.py': No such file or directory\n"
sup_bad, ctl_bad = mkhome(runner=broken_runner, memory_shim=False), mkhome(runner=broken_runner, memory_shim=False)
sup_ok,  ctl_ok  = mkhome(runner="tick end\n", memory_shim=True),    mkhome(runner="tick end\n", memory_shim=True)
mk_snap = lambda sh, ch: {
    "sup": {"id": "sup", "up": True, "status": "running", "port": 8803, "home": sh, "last_seen": time.time()},
    "ctl": {"id": "ctl", "up": True, "status": "running", "port": 8804, "home": ch, "last_seen": time.time()}}
pol_sup = Policy({"default_mode": "alert", "playbooks": {}, "autofix_allowlist": [], "thresholds": {},
                  "per_agent": {"sup": {"suppress": ["memory_path_broken"]}}})
st6 = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s6.json")
# cycle 1: both agents broken. sup is muted (no ALERT); ctl alerts.
fleet_monitor.fleet.snapshot = lambda: mk_snap(sup_bad, ctl_bad)
fleet_monitor.cycle(pol_sup, st6, cq, now=5_000.0, dryrun=True, log=lambda *_: None)
sup_esc1 = pathlib.Path(sup_bad) / "state" / "escalations.log"
check("mute: suppressed agent gets NO alert line", not sup_esc1.exists() or "memory_path_broken" not in sup_esc1.read_text())
# cycle 2: both recover (homes now healthy). sup must stay silent; ctl must emit RECOVERED.
fleet_monitor.fleet.snapshot = lambda: mk_snap(sup_ok, ctl_ok)
fleet_monitor.cycle(pol_sup, st6, cq, now=5_060.0, dryrun=True, log=lambda *_: None)
sup_esc2, ctl_esc2 = pathlib.Path(sup_ok) / "state" / "escalations.log", pathlib.Path(ctl_ok) / "state" / "escalations.log"
check("mute: suppressed recovery emits NO line", not sup_esc2.exists() or "RECOVERED" not in sup_esc2.read_text())
check("mute: non-suppressed agent still gets RECOVERED", ctl_esc2.exists() and "RECOVERED" in ctl_esc2.read_text())
fleet_monitor.fleet.snapshot = lambda: fake_snap   # restore for downstream tests


# --- (7) D2b: off-Opus LLM layer (intel) — parse, gating, cache, cycle integration ---------------
from monitor import intel

check("intel.is_novel: high + uncovered key", intel.is_novel({"severity": "high", "key": "cost_spike"}))
check("intel.is_novel: covered key excluded",
      not intel.is_novel({"severity": "high", "key": "context_explosion"}))
check("intel.is_novel: med severity excluded", not intel.is_novel({"severity": "med", "key": "cost_spike"}))
check("intel._parse_json: extracts JSON from prose",
      (intel._parse_json('sure! {"cause":"x","fix":"y","confidence":"high"} done') or {}).get("fix") == "y")
check("intel._parse_json: junk -> None", intel._parse_json("no json here") is None)

# hypothesize with a monkeypatched worker (no network) — verifies shape + honesty defaults
intel._resolve_key = lambda: "fake-key"
intel.local_agent.chat = lambda ep, msgs, **k: '{"cause":"runaway retry loop","fix":"cap retries","confidence":"med"}'
lf = intel.hypothesize("a", {"severity": "high", "key": "cost_spike", "title": "Cost 4x", "evidence": "$0.4/tick"}, {})
check("intel.hypothesize: source=llm finding", lf and lf["source"] == "llm" and lf["key"] == "llm_cost_spike")
check("intel.hypothesize: carries cause+fix", lf["cause"] == "runaway retry loop" and lf["recommendation"] == "cap retries")
intel.local_agent.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("endpoint down"))
check("intel.hypothesize: fail-open on transport error",
      intel.hypothesize("a", {"severity": "high", "key": "cost_spike", "title": "t"}, {}) is None)

# cycle integration: a novel high-sev anomaly -> an LLM finding lands in the heartbeat + escalates
fleet_monitor.intel.available = lambda: True
fleet_monitor.intel.local_agent.chat = lambda ep, msgs, **k: '{"cause":"unbounded log","fix":"rotate it","confidence":"high"}'
fleet_monitor.intel._resolve_key = lambda: "fake-key"
fleet_monitor.diagnostics.from_home = lambda h: {"anomalies": [
    {"severity": "high", "key": "cost_spike", "title": "Cost spiked 4x", "evidence": "$0.4/tick"}], "health": {}}
st3 = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s3.json")
logs3 = []
hb = fleet_monitor.cycle(pol, st3, cq, now=time.time(), dryrun=True, log=logs3.append)
brk = hb["agents"]["broken"]["findings"]
llm_f = [f for f in brk if f.get("source") == "llm"]
check("cycle: LLM finding present in heartbeat", len(llm_f) == 1 and llm_f[0]["key"] == "llm_cost_spike")
check("cycle: LLM finding escalated (alert mode)", llm_f[0]["escalated"] is True)
check("cycle: LLM hypothesis logged", any("LLM hypothesis" in l for l in logs3))
# second cycle reuses the cache (no second chat call) — flip chat to raise; cached finding still served
fleet_monitor.intel.local_agent.chat = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("should not be called"))
hb2 = fleet_monitor.cycle(pol, st3, cq, now=time.time() + 2, dryrun=True, log=logs3.append)
llm_f2 = [f for f in hb2["agents"]["broken"]["findings"] if f.get("source") == "llm"]
check("cycle: LLM result cached (no re-call)", len(llm_f2) == 1 and llm_f2[0]["cause"] == "unbounded log")

# --- (8) D2b: critical push (notify) — rate limit + fail-open + cycle gating --------------------
from monitor import notify
st4 = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s4.json")
oks = [st4.push_ok(per_hour=3, now=5000 + i) for i in range(5)]
check("push_ok: allows up to the cap then blocks", oks == [True, True, True, False, False])
check("notify.push: fail-open when unconfigured",
      notify.push("x") is False or notify.available())  # no-op unless a channel is wired in this env
# channel preference + Slack webhook path (monkeypatched transport, no network)
sent = {}
notify._resolve = lambda: {"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x",
                           "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}
notify._post = lambda url, payload, timeout: sent.update(url=url, payload=payload) or b"ok"
check("notify.channel: prefers slack", notify.channel() == "slack")
check("notify.push: posts to the slack webhook",
      notify.push("hi") is True and sent["url"] == "https://hooks.slack.test/x" and sent["payload"]["text"] == "hi")
notify._resolve = lambda: {"SLACK_WEBHOOK_URL": "", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "5"}
check("notify.channel: falls back to telegram", notify.channel() == "telegram")
# cycle gating: a high-sev deterministic alert triggers exactly one push (broken agent → memory_path)
pushed = []
fleet_monitor.notify.available = lambda: True
fleet_monitor.notify.push = lambda text, **k: (pushed.append(text), True)[1]
fleet_monitor.intel.available = lambda: False   # isolate the deterministic push path
fleet_monitor.diagnostics.from_home = lambda h: {"anomalies": [], "health": {}}
st5 = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s5.json")
fleet_monitor.cycle(pol, st5, cq, now=time.time(), dryrun=True, log=lambda *_: None)
check("cycle: high-sev alert pushed once", len(pushed) == 1 and "memory" in pushed[0].lower())
fleet_monitor.cycle(pol, st5, cq, now=time.time() + 1, dryrun=True, log=lambda *_: None)
check("cycle: suppressed repeat does NOT re-push", len(pushed) == 1)


# --- (9) D3: safe autofix allowlist + operator-stopped gate --------------------------------------
fleet_monitor.notify.available = lambda: False     # isolate the autofix path from push
fleet_monitor.intel.available = lambda: False
downhome = mkhome(runner="boom\n", memory_shim=True)
down_snap = {"crashed": {"id": "crashed", "up": False, "status": "exited(1)", "port": 8809,
                         "home": downhome, "last_seen": time.time()}}
fleet_monitor.fleet.snapshot = lambda: down_snap
fleet_monitor.diagnostics.from_home = lambda h: {"anomalies": [], "health": {}}
fleet_monitor.effective_mode = lambda a, p: "autofix"          # operator opted this agent into autofix
pol_af = Policy({"default_mode": "autofix", "playbooks": {},
                 "autofix_allowlist": ["container_down"], "thresholds": {}})
cq9 = pathlib.Path(tempfile.mkdtemp()) / "control"
st9 = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s9.json")
fleet_monitor.cycle(pol_af, st9, cq9, now=time.time(), dryrun=False, log=lambda *_: None)
specs = list((cq9 / "incoming").glob("*.json")) if (cq9 / "incoming").exists() else []
check("autofix: allowlisted container_down enqueues a restart", len(specs) == 1)
check("autofix: spec is a restart for the agent",
      json.loads(specs[0].read_text()).get("action") == "restart")

# operator-stopped marker present -> NO autofix (escalates instead)
check("operator_stopped: false without marker", fleet_monitor.operator_stopped(downhome) is False)
(pathlib.Path(downhome) / "state" / ".operator-stopped").write_text("2026-06-26T00:00:00Z")
check("operator_stopped: true with marker", fleet_monitor.operator_stopped(downhome) is True)
cq9b = pathlib.Path(tempfile.mkdtemp()) / "control"
st9b = mstate.MonitorState(path=pathlib.Path(tempfile.mkdtemp()) / "s9b.json")
fleet_monitor.cycle(pol_af, st9b, cq9b, now=time.time(), dryrun=False, log=lambda *_: None)
specs_b = list((cq9b / "incoming").glob("*.json")) if (cq9b / "incoming").exists() else []
check("autofix: operator-stopped pod is NOT auto-restarted", len(specs_b) == 0)
esc_b = (pathlib.Path(downhome) / "state" / "escalations.log").read_text()
check("autofix: escalation explains the operator-stopped skip", "operator stopped it deliberately" in esc_b)

# ── events_dark: a pod that is ticking but has STOPPED REPORTING (2026-07-22) ────────────────
# Three of five live pods had event capture dead for 27.5h — all froze within 18 seconds of each
# other, kept ticking, and kept showing green, because nothing asked whether the stream was alive.
# preflight checks the same thing at BOOT, but this outage began mid-run and survived 27h without a
# restart: only a continuously-running check catches that.
def _dark_home(gap_h, paused=False, wired=False):
    d = pathlib.Path(tempfile.mkdtemp()); (d / "state").mkdir(); (d / ".claude").mkdir()
    (d / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PostToolUse": [
        {"hooks": [{"command": "python3 /agent/.claude/hooks/event_log.py" if wired else "x"}]}]}}))
    (d / "state" / "events.jsonl").write_text("{}\n")
    (d / "state" / "tick-scorecard.jsonl").write_text("{}\n")
    os.utime(d / "state" / "events.jsonl", (time.time() - gap_h * 3600,) * 2)
    if paused:
        (d / "state" / "paused").write_text("x")
    return str(d)

check("events_dark: fires on 27.5h of silence while ticking",
      playbooks._events_dark.match({}, _dark_home(27.5), {"up": True}, {}) is True)
check("events_dark: silent when the stream is fresh",
      playbooks._events_dark.match({}, _dark_home(0.1), {"up": True}, {}) is False)
check("events_dark: a PAUSED pod is silent on purpose, not broken",
      playbooks._events_dark.match({}, _dark_home(27.5, paused=True), {"up": True}, {}) is False)
check("events_dark: a DOWN container is container_down's job",
      playbooks._events_dark.match({}, _dark_home(27.5), {"up": False}, {}) is False)
_dx = playbooks._events_dark.diagnose({}, _dark_home(27.5), {"up": True}, {})
check("events_dark: names the real root cause (hook not wired)", "NOT WIRED" in _dx["evidence"])
check("events_dark: recommends wiring when unwired", "wire event_log" in _dx["recommendation"])
_dx2 = playbooks._events_dark.diagnose({}, _dark_home(27.5, wired=True), {"up": True}, {})
check("events_dark: recommends a RESTART when wired but stale (a hook change misses a live loop)",
      "restart the pod" in _dx2["recommendation"])
check("events_dark: registered in the runbook",
      "events_dark" in playbooks.BY_KEY and playbooks._events_dark in playbooks.ALL)


# ── churn_spike must CLEAR when the pod stops churning (2026-07-22) ──────────────────────────
# churn_alarm is already a 10-tick windowed verdict; the matcher then took any() over 10 of those,
# so a transient spike latched the finding for ~20 ticks. scribepod showed churn_spike while its
# own newest three records all said churn_alarm=False.
def _churn_home(alarms, churn=None):
    d = pathlib.Path(tempfile.mkdtemp()); (d / "state").mkdir()
    with (d / "state" / "tick-scorecard.jsonl").open("w") as f:
        for a in alarms:
            f.write(json.dumps({"ts": "2026-07-22T10:00:00Z", "churn_alarm": a,
                                "churn": (churn or {}) if a else {}}) + "\n")
    return str(d)

check("churn_spike fires while the newest record is alarming",
      playbooks._churn_spike.match({}, _churn_home([True] * 3, {"state/rollup.md": 9}),
                                   {"up": True}, {}) is True)
check("churn_spike CLEARS once the newest record is clean (no latch)",
      playbooks._churn_spike.match({}, _churn_home([True, True, False, False, False]),
                                   {"up": True}, {}) is False)
check("churn_spike is silent on a down container",
      playbooks._churn_spike.match({}, _churn_home([True] * 3, {"x": 9}), {"up": False}, {}) is False)
_cd = playbooks._churn_spike.diagnose({}, _churn_home([True] * 3, {"state/rollup.md": 9}), {"up": True}, {})
check("churn_spike names the file churning in the CURRENT record", "state/rollup.md" in _cd["cause"])


print()
if check.failed:
    print(f"{check.failed} FAILED")
    raise SystemExit(1)
print("ALL PASS")

# ── off_directive must not fire on a pod whose product ships EXTERNALLY (2026-07-21) ─────────
# serves_observed is derived from LOCAL product writes, so such a pod (which publishes to
# Royal Road) could never show as serving anything and the rule stayed lit on a working pod. Pods
# that declare product_measured_externally now report serves_observed=None — "cannot observe" — and
# the rule must treat unknown as not-a-failure while still catching the real cases.
def test_offdir_external_product():
    import json as _j, tempfile as _t, pathlib as _p
    from monitor.playbooks import _offdir_match

    def home(observed, serves):
        d = _p.Path(_t.mkdtemp())
        (d / "state").mkdir()
        recs = [{"ts": "2026-07-21T1%d:00:00" % i, "config": "ok", "serves": serves,
                 "serves_observed": observed, "writes": {"product": 0}} for i in range(3)]
        (d / "state" / "tick-scorecard.jsonl").write_text("\n".join(_j.dumps(r) for r in recs))
        return str(d)

    assert not _offdir_match({}, home(None, ["lc-x"]), {"up": True}, {}), \
        "unknown observation on an externally-measured pod must NOT read as off-directive"
    assert _offdir_match({}, home(False, ["lc-x"]), {"up": True}, {}), \
        "a pod observed NOT serving its directive must still fire"
    assert _offdir_match({}, home(None, []), {"up": True}, {}), \
        "declaring nothing while writing nothing must still fire"
    print("ok: off_directive tolerates unobservable (external) product, still catches real drift")


test_offdir_external_product()

