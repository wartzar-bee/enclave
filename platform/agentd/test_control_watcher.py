"""Unit tests for control_watcher.py — the host-side executor that turns dropped control specs into
`enclave fleet <verb>` invocations (the manager-controls-pods privilege-separated actor).

Hermetic: NEVER runs docker / fleet.py. The execution seam is `control_watcher.subprocess.run`, which we
monkeypatch with a recorder that captures the argv and returns a fake completed-process. The audit log is
redirected to a temp file so we never touch ~/.config/enclave/fleet-audit.log. Queue dirs are temp.

Covers: spec parsing (_load_spec), verb building/dispatch (_build_verb) for every action, the full
_process round-trip (route to the right `enclave fleet` argv, drain the queue to processed/), malformed /
unknown-action / unsafe-id rejection (moved to failed/ WITHOUT invoking the executor), executor-failure
handling, and the operator-stopped safety gate.

IMPORTANT (operator-stopped gate): the gate is NOT enforced inside control_watcher — the watcher is a
pure executor and will run any well-formed restart spec dropped in its queue. The gate lives UPSTREAM in
fleet_monitor._maybe_autofix (it refuses to ENQUEUE a restart/up for an operator-stopped agent), via the
helper fleet_monitor.operator_stopped(home). We test that helper directly here and document the trust
boundary with an explicit assertion. See the BUGS FOUND note in the test runner's report.

Run: python3 test_control_watcher.py
"""
import json
import pathlib
import sys
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F
import control_watcher as CW

check = F.Check()


class Recorder:
    """Stand-in for subprocess.run: records argv, returns a fake CompletedProcess with the given rc."""

    def __init__(self, returncode=0, stderr="", stdout=""):
        self.calls = []
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return types.SimpleNamespace(returncode=self.returncode, stderr=self.stderr, stdout=self.stdout)

    @property
    def verbs(self):
        """The args after `... enclave fleet` for each captured call."""
        out = []
        for c in self.calls:
            # argv == [python, enclave_path, "fleet", *verb]
            out.append(c[3:] if len(c) >= 3 and c[2] == "fleet" else c)
        return out


def _mk_queue(tmp):
    q = pathlib.Path(tmp)
    for sub in ("incoming", "processed", "failed"):
        (q / sub).mkdir(parents=True, exist_ok=True)
    return q


def _drop(queue, name, payload):
    """Write a JSON control spec into incoming/ and return its path."""
    p = queue / "incoming" / name
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return p


def main():
    import tempfile
    root = pathlib.Path(tempfile.mkdtemp(prefix="cw-test-"))
    # redirect the audit log so we never touch the real ~/.config/enclave one
    CW.AUDIT = root / "audit.log"

    # ---------------------------------------------------------------- pure helpers: SAFE / ACTIONS
    check("SAFE accepts a normal id", bool(CW.SAFE.match("data-worker")))
    check("SAFE accepts digits/underscore", bool(CW.SAFE.match("a1_b-2")))
    check("SAFE rejects leading dash", not CW.SAFE.match("-bad"))
    check("SAFE rejects path-escape", not CW.SAFE.match("../etc"))
    check("SAFE rejects slash", not CW.SAFE.match("a/b"))
    check("SAFE rejects spaces", not CW.SAFE.match("bad id"))
    check("SAFE rejects empty", not CW.SAFE.match(""))
    check("ACTIONS allowlist has lifecycle verbs", {"up", "down", "restart", "kick"} <= CW.ACTIONS)
    check("ACTIONS allowlist has config verbs",
          {"set-config", "set-brain", "set-mode", "preset", "send"} <= CW.ACTIONS)

    # ---------------------------------------------------------------- _load_spec
    q = _mk_queue(root / "q_load")
    p = _drop(q, "myagent.json", {"agent": "data-worker", "action": "ReStArT", "requested_by": "boss"})
    agent, action, data = CW._load_spec(p)
    check.eq("_load_spec reads agent", agent, "data-worker")
    check.eq("_load_spec lowercases action", action, "restart")
    check.eq("_load_spec returns full data dict", data.get("requested_by"), "boss")

    # agent falls back to the file stem when omitted
    p2 = _drop(q, "stem-agent.json", {"action": "up"})
    agent2, action2, _ = CW._load_spec(p2)
    check.eq("_load_spec agent falls back to file stem", agent2, "stem-agent")

    # malformed JSON -> empty data, empty action (no crash)
    p3 = _drop(q, "broken.json", "{not valid json")
    agent3, action3, data3 = CW._load_spec(p3)
    check.eq("_load_spec bad json -> empty action", action3, "")
    check.eq("_load_spec bad json -> data is {}", data3, {})
    check.eq("_load_spec bad json -> agent is stem", agent3, "broken")

    # non-dict JSON (a list) -> coerced to {}
    p4 = _drop(q, "listspec.json", [1, 2, 3])
    _, action4, data4 = CW._load_spec(p4)
    check.eq("_load_spec non-dict json -> data {}", data4, {})
    check.eq("_load_spec non-dict json -> action empty", action4, "")

    # ---------------------------------------------------------------- _build_verb (per action)
    v, e = CW._build_verb("restart", "w", {})
    check("build_verb restart -> [restart, w]", v == ["restart", "w"] and e is None)
    v, e = CW._build_verb("up", "w", {})
    check("build_verb up -> [up, w]", v == ["up", "w"] and e is None)
    v, e = CW._build_verb("down", "w", {})
    check("build_verb down -> [down, w]", v == ["down", "w"] and e is None)
    v, e = CW._build_verb("kick", "w", {})
    check("build_verb kick -> [kick, w]", v == ["kick", "w"] and e is None)

    v, e = CW._build_verb("send", "w", {"text": "resume the swap"})
    check("build_verb send -> [send, w, text]", v == ["send", "w", "resume the swap"] and e is None)
    v, e = CW._build_verb("send", "w", {"text": "   "})
    check("build_verb send empty text -> error", v is None and "non-empty" in (e or ""))
    v, e = CW._build_verb("send", "w", {})
    check("build_verb send missing text -> error", v is None and e is not None)

    v, e = CW._build_verb("set-config", "w", {"config": {"ROUTER": "on", "INTERVAL_SECONDS": 600}})
    check("build_verb set-config -> k=v pairs",
          v == ["set-config", "w", "ROUTER=on", "INTERVAL_SECONDS=600"] and e is None)
    v, e = CW._build_verb("set-config", "w", {"config": {}})
    check("build_verb set-config empty -> error", v is None and e is not None)
    v, e = CW._build_verb("set-config", "w", {"config": "notamap"})
    check("build_verb set-config non-map -> error", v is None and e is not None)

    v, e = CW._build_verb("set-brain", "w", {"brain": "local", "model": "qwen/x"})
    check("build_verb set-brain w/ model", v == ["set-brain", "w", "local", "qwen/x"] and e is None)
    v, e = CW._build_verb("set-brain", "w", {"brain": "api"})
    check("build_verb set-brain no model", v == ["set-brain", "w", "api"] and e is None)
    v, e = CW._build_verb("set-brain", "w", {})
    check("build_verb set-brain missing brain -> error", v is None and e is not None)

    v, e = CW._build_verb("set-mode", "w", {"mode": "autonomous", "interval": 3600})
    check("build_verb set-mode w/ interval", v == ["set-mode", "w", "autonomous", "3600"] and e is None)
    v, e = CW._build_verb("set-mode", "w", {"mode": "chat"})
    check("build_verb set-mode no interval", v == ["set-mode", "w", "chat"] and e is None)
    v, e = CW._build_verb("set-mode", "w", {})
    check("build_verb set-mode missing mode -> error", v is None and e is not None)

    v, e = CW._build_verb("preset", "w", {"preset": "claude-managed"})
    check("build_verb preset", v == ["preset", "w", "claude-managed"] and e is None)
    v, e = CW._build_verb("preset", "w", {})
    check("build_verb preset missing -> error", v is None and e is not None)

    # ---------------------------------------------------------------- _process: happy-path dispatch
    q = _mk_queue(root / "q_ok")
    rec = Recorder(returncode=0)
    CW.subprocess.run = rec
    sp = _drop(q, "data-worker.json", {"agent": "data-worker", "action": "restart", "requested_by": "m"})
    CW._process(sp, q)
    check("process restart: executor called exactly once", len(rec.calls) == 1)
    check("process restart: routed to `enclave fleet restart data-worker`",
          rec.verbs and rec.verbs[0] == ["restart", "data-worker"])
    check("process restart: argv[2] is the fleet subcommand", rec.calls[0][2] == "fleet")
    check("process restart: incoming drained", list((q / "incoming").glob("*")) == [])
    check("process restart: spec moved to processed/",
          len(list((q / "processed").glob("*data-worker.json"))) == 1)
    check("process restart: no failed artifacts", list((q / "failed").glob("*")) == [])

    # send action carries the text through
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "send1.json", {"agent": "worker", "action": "send", "text": "resume the swap"})
    CW._process(sp, q)
    check("process send: routes text", rec.verbs and rec.verbs[0] == ["send", "worker", "resume the swap"])

    # set-brain with model
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "brain1.json", {"agent": "worker", "action": "set-brain",
                                  "brain": "local", "model": "qwen/qwen3-next-80b-a3b-instruct"})
    CW._process(sp, q)
    check("process set-brain: routes brain+model",
          rec.verbs and rec.verbs[0] == ["set-brain", "worker", "local", "qwen/qwen3-next-80b-a3b-instruct"])

    # ---------------------------------------------------------------- _process: rejection paths
    # (a) unsafe agent id -> failed, executor NEVER called
    q = _mk_queue(root / "q_unsafe")
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "bad.json", {"agent": "../etc/passwd", "action": "restart"})
    CW._process(sp, q)
    check("process unsafe-id: executor NOT called", rec.calls == [])
    check("process unsafe-id: spec moved to failed/", len(list((q / "failed").glob("*bad.json"))) == 1)
    check("process unsafe-id: .error written", len(list((q / "failed").glob("*.error"))) == 1)
    check("process unsafe-id: incoming drained", list((q / "incoming").glob("*")) == [])

    # (b) unknown action -> failed, executor NEVER called
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "weird.json", {"agent": "worker", "action": "self-destruct"})
    CW._process(sp, q)
    check("process unknown-action: executor NOT called", rec.calls == [])
    check("process unknown-action: spec failed", len(list((q / "failed").glob("*weird.json"))) == 1)

    # (c) malformed JSON -> empty action -> rejected (not in ACTIONS), executor NEVER called
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "garbage.json", "{this is : not json")
    CW._process(sp, q)
    check("process bad-json: executor NOT called", rec.calls == [])
    check("process bad-json: spec failed (no crash)", len(list((q / "failed").glob("*garbage.json"))) == 1)

    # (d) send without text -> _build_verb error -> failed, executor NEVER called
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "notext.json", {"agent": "worker", "action": "send"})
    CW._process(sp, q)
    check("process send-no-text: executor NOT called", rec.calls == [])
    check("process send-no-text: failed", len(list((q / "failed").glob("*notext.json"))) == 1)

    # ---------------------------------------------------------------- _process: executor reports failure
    q = _mk_queue(root / "q_execfail")
    rec = Recorder(returncode=2, stderr="boom: no such agent"); CW.subprocess.run = rec
    sp = _drop(q, "data-worker.json", {"agent": "data-worker", "action": "restart"})
    CW._process(sp, q)
    check("process exec-fail: executor was called", len(rec.calls) == 1)
    check("process exec-fail: spec moved to failed/",
          len(list((q / "failed").glob("*data-worker.json"))) == 1)
    check("process exec-fail: .error captures stderr",
          any("boom" in (f.read_text()) for f in (q / "failed").glob("*.error")))
    check("process exec-fail: NOT in processed/", list((q / "processed").glob("*")) == [])

    # ---------------------------------------------------------------- the operator-stopped GATE
    # The gate lives in fleet_monitor (the autofix PLANNER), not in control_watcher (the EXECUTOR).
    # Test the helper directly + document the trust boundary.
    import fleet_monitor as FM
    fleet_root = F.build_fleet(specs=[{"id": "gated"}])
    home = pathlib.Path(fleet_root) / "gated" / "home"
    marker = home / "state" / ".operator-stopped"
    check("operator_stopped: False without marker", FM.operator_stopped(str(home)) is False)
    marker.write_text("2026-06-27T00:00:00Z")
    check("operator_stopped: True with marker present", FM.operator_stopped(str(home)) is True)
    check("operator_stopped: False on empty home", FM.operator_stopped("") is False)
    check("operator_stopped: False on None home", FM.operator_stopped(None) is False)

    # TRUST-BOUNDARY assertion (documents the finding): control_watcher itself does NOT consult the
    # marker. If a restart spec for an operator-stopped agent reaches the queue by ANY path, the watcher
    # executes it. Safety therefore depends entirely on the producer (fleet_monitor) refusing to enqueue.
    q = _mk_queue(root / "q_gate")
    rec = Recorder(returncode=0); CW.subprocess.run = rec
    sp = _drop(q, "gated.json", {"agent": "gated", "action": "restart"})
    CW._process(sp, q)
    check("FINDING: control_watcher has NO operator-stopped gate (it executed the restart)",
          rec.verbs and rec.verbs[0] == ["restart", "gated"],
          "watcher is a pure executor; gate must hold upstream in fleet_monitor before enqueue")

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
