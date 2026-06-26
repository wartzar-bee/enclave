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

print()
if check.failed:
    print(f"{check.failed} FAILED")
    raise SystemExit(1)
print("ALL PASS")
