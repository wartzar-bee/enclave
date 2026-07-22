#!/usr/bin/env python3
"""Unit tests for fleet.py — the Enclave fleet control plane (discovery + snapshot).

Covers the PURE / disk-only surface (the docker-shelling cmd_* are skipped; snapshot's docker calls are
neutralized so this passes WITH OR WITHOUT a docker daemon running — CI == laptop):
  _SAFE regex, _env parsing, _port, _is_deployment/_is_enclave_deployment, _state, _scan_deployments
  (incl. skip rules), snapshot() (down-marking, brain/model/home, standalone-vs-fleet kind), _fmt_age.

Hermetic — builds a temp fleet, points fleet.STACKS_ROOTS at it, monkeypatches the docker calls. Run:
    python3 test_fleet.py
"""
import json
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F
import fleet

check = F.Check()


def _point_at(root):
    """Aim fleet at the fixture root and neutralize all docker access for hermeticity."""
    fleet.STACKS_ROOTS = [pathlib.Path(root).resolve()]
    fleet._scan_cache = {"ts": 0.0, "data": {}}
    fleet._compose_ls = lambda: []        # no live docker projects
    fleet._manifest = lambda: {}          # no host manifest


def _write_dep(path, aid, enclave_marker=True, with_env=True):
    """Drop a minimal compose deployment dir at `path` (used for skip-rule + marker tests)."""
    path.mkdir(parents=True, exist_ok=True)
    compose = "services:\n  agent:\n    image: " + ("enclave-agent:latest\n" if enclave_marker else "busybox\n")
    (path / "docker-compose.yml").write_text(compose)
    if with_env:
        (path / ".env").write_text(f"AGENT_ID={aid}\nBRAIN=claude\nMODEL=claude-sonnet-4-6\n")


def main():
    root = F.build_fleet(specs=[{"id": "alpha"}, {"id": "beta", "brain": "api", "model": "qwen"}])
    rootp = pathlib.Path(root)
    rrootp = rootp.resolve()   # scan/snapshot return paths under the .resolve()'d STACKS_ROOTS (macOS /var -> /private/var)
    _point_at(root)

    # ---------------------------------------------------------------- _SAFE regex
    for good in ("alpha", "my-agent", "a1_b", "x", "agent-007"):
        check(f"_SAFE accepts {good!r}", bool(fleet._SAFE.match(good)))
    for bad in ("../etc", "Bad Name", "", ".hidden", "UPPER", "has space", "-leading"):
        check(f"_SAFE rejects {bad!r}", not fleet._SAFE.match(bad))

    # ---------------------------------------------------------------- _env parsing
    envdir = rootp / "_envtest"
    envdir.mkdir()
    (envdir / ".env").write_text(
        "# a comment\n"
        "\n"
        "AGENT_ID=zeta\n"
        '  BRAIN="api"  \n'                       # quotes + surrounding whitespace
        "COMMS_URL=http://host:18999/x=y\n"       # '=' inside the value must survive
        "EMPTY=\n"
        "noeq line ignored\n"
        "MODEL='qwen-3'\n")
    env = fleet._env(str(envdir))
    check.eq("_env parses simple key", env.get("AGENT_ID"), "zeta")
    check.eq("_env strips quotes + whitespace", env.get("BRAIN"), "api")
    check.eq("_env keeps '=' in value", env.get("COMMS_URL"), "http://host:18999/x=y")
    check.eq("_env empty value -> ''", env.get("EMPTY"), "")
    check.eq("_env strips single quotes", env.get("MODEL"), "qwen-3")
    check("_env skips comments", "# a comment" not in env)
    check("_env skips non-kv lines", "noeq line ignored" not in env and len([k for k in env if " " in k]) == 0)
    check.eq("_env missing dir -> {}", fleet._env(str(rootp / "nope")), {})

    # ---------------------------------------------------------------- _port
    check.eq("_port host:port form", fleet._port({"WEB_CHAT_BIND": "0.0.0.0:8890"}), "8890")
    check.eq("_port bare port", fleet._port({"WEB_CHAT_BIND": "8891"}), "8891")
    check.eq("_port default", fleet._port({}), "8888")

    # ---------------------------------------------------------------- _is_deployment / _is_enclave_deployment
    dep = rootp / "alpha"
    check("_is_deployment true for compose+.env", fleet._is_deployment(dep))
    check("_is_deployment false for bare dir", not fleet._is_deployment(rootp / "_envtest_missing"))

    # enclave marker via AGENT_ID in .env
    check("_is_enclave_deployment true (AGENT_ID marker)", fleet._is_enclave_deployment(dep))
    # marker via 'enclave' in compose, no AGENT_ID
    edir = rootp / "_enclave_compose"
    edir.mkdir()
    (edir / "docker-compose.yml").write_text("services:\n  agent:\n    image: enclave-agent:latest\n")
    (edir / ".env").write_text("BRAIN=claude\n")   # no AGENT_ID
    check("_is_enclave_deployment true (compose mentions enclave)", fleet._is_enclave_deployment(edir))
    # compose + .env but NO enclave marker -> not an enclave deployment
    plain = rootp / "_plain_compose"
    plain.mkdir()
    (plain / "docker-compose.yml").write_text("services:\n  web:\n    image: nginx\n")
    (plain / ".env").write_text("FOO=bar\n")
    check("_is_enclave_deployment false (no marker)", not fleet._is_enclave_deployment(plain))
    # missing .env -> not a deployment at all
    check("_is_enclave_deployment false (missing .env)", not fleet._is_enclave_deployment(rootp / "alpha" / "home"))

    # ---------------------------------------------------------------- _state
    home = dep / "home"
    st = fleet._state(home)
    check.eq("_state headline from rollup.md first body line", st["headline"], "alpha is working on something")
    check.eq("_state work_open counts todo+doing", st["work_open"], 2)   # fixture: doing+todo open, done closed
    check.eq("_state tick=working (start after last end)", st["tick"], "working")
    check("_state last_seen is a positive mtime", st["last_seen"] > 0)
    # An orphaned start (tick crashed without writing "tick end") must DECAY to idle, never latch the
    # badge to "working" forever. This was only ever covered by accident — the fixture's hardcoded
    # 2026-06-27 timestamps aged past the tick window and inverted the test above.
    stale = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 30 * 3600))
    (home / "logs" / "runner.log").write_text(f"{stale} tick start\n")
    check.eq("_state tick=idle (orphaned start older than the tick window)",
             fleet._state(home)["tick"], "idle")
    check.eq("_state no home -> defaults", fleet._state(None),
             {"headline": "", "work_open": 0, "tick": "", "last_seen": 0, "rollup_age_s": None})

    # ---- headline + last_seen: both used to report stale values as if they were current ----
    # `enclave init` seeds "(no ticks yet)" and agents append BELOW it, so the two most productive
    # pods in the fleet both displayed that placeholder as their status.
    check.eq("_headline skips the init placeholder",
             fleet._headline("# t\n\n(no ticks yet)\n2026-07-20 did a thing\n"),
             "2026-07-20 did a thing")
    # Ordering is not a shared convention: some agents append, others prepend. Pick by DATE.
    check.eq("_headline picks the newest date when the rollup APPENDS (oldest first)",
             fleet._headline("2026-07-19 old\n2026-07-21 newest\n"), "2026-07-21 newest")
    check.eq("_headline picks the newest date when the rollup PREPENDS (newest first)",
             fleet._headline("2026-07-21 newest\n2026-07-19 old\n"), "2026-07-21 newest")
    check.eq("_headline falls back to first line when no dates are present",
             fleet._headline("# h\n\nplain line\nsecond\n"), "plain line")

    # ---- _loop_wait: runner.log interleaves the AGENT's transcript with the LOOP's decisions ----
    h3 = rootp / "loopagent"
    (h3 / "logs").mkdir(parents=True)
    lg = h3 / "logs" / "runner.log"
    lg.write_text(
        "2026-07-22T09:00:00Z — [x] loop: no tick-status + open work → continue in 900s\n"
        "  ⏴ ⚠ PreToolUse:Bash hook error: [agent-guard] BLOCKED: git is disabled for agents\n")
    # The guard message is the AGENT being denied one Bash call — not the loop being blocked.
    # logan-cross was reported `blocked` on exactly this while it was mid-tick and healthy.
    check.eq("_loop_wait ignores a guard BLOCKED inside the tick transcript",
             fleet._loop_wait(h3)["kind"], "continue")
    lg.write_text("2026-07-22T09:00:00Z — [x] loop: ... → backing off to 9600s (cap 10800s)\n")
    check.eq("_loop_wait reads a backoff", fleet._loop_wait(h3), {"kind": "backoff", "wait_s": 9600})
    lg.write_text("2026-07-22T09:00:00Z — [x] loop: tick deferred (cap/lock) — retry in 600s\n")
    check.eq("_loop_wait surfaces a deferred tick with its retry", fleet._loop_wait(h3),
             {"kind": "deferred", "wait_s": 600})
    (h3 / "state").mkdir(parents=True, exist_ok=True)
    (h3 / "state" / "paused").write_text("stopped by operator directive\n")
    # A pod parked on purpose must say so: the generic "deferred (cap/lock)" line explains nothing,
    # and stoneforge (stopped by operator directive) showed an EMPTY status column because of it.
    check.eq("_loop_wait reports a deliberately paused pod as paused",
             fleet._loop_wait(h3)["kind"], "paused")

    h2 = rootp / "seenagent"
    (h2 / "state").mkdir(parents=True)
    (h2 / "state" / "rollup.md").write_text("2026-07-21 stale rollup\n")
    os.utime(h2 / "state" / "rollup.md", (time.time() - 27 * 3600,) * 2)
    tick_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600))
    (h2 / "state" / "tick-scorecard.jsonl").write_text(
        json.dumps({"ts": tick_ts, "writes": {"product": 1}}) + "\n")
    # last_seen must follow the TICK (10m), not the rollup's mtime (27h) — channel-lab displayed
    # seen:27h while it had ticked ten minutes earlier and had 6 productive ticks in 2h.
    check("_state last_seen comes from the tick record, not rollup mtime",
             abs(fleet._state(h2)["last_seen"] - (time.time() - 600)) < 120)
    check("_state still exposes rollup age so a stale headline can be labelled",
             fleet._state(h2)["rollup_age_s"] > 24 * 3600)

    # ---------------------------------------------------------------- _scan_deployments (+ skip rules)
    # plant deployments that MUST be skipped: vcs/vendor dir, .hidden dir, backup dir
    _write_dep(rootp / "node_modules" / "skipme", "skipme")          # _SKIP_DIRS
    _write_dep(rootp / ".hidden" / "hiddenagent", "hiddenagent")     # dot-dir
    _write_dep(rootp / "backups" / "backupagent", "backupagent")     # 'backup' in name
    _write_dep(rootp / "nested" / "deep" / "realagent", "realagent")  # legit, nested -> should be found
    fleet._scan_cache = {"ts": 0.0, "data": {}}
    found = fleet._scan_deployments()
    check("_scan finds fixture agents", {"alpha", "beta"} <= set(found))
    check("_scan finds nested legit agent", "realagent" in found)
    check("_scan SKIPS node_modules dir", "skipme" not in found)
    check("_scan SKIPS .hidden dir", "hiddenagent" not in found)
    check("_scan SKIPS backup dir", "backupagent" not in found)
    check("_scan maps id -> dir", found.get("alpha") == str(rrootp / "alpha"))
    # TTL cache: a second call (cache warm) returns the same set even after we wipe disk markers
    cached = fleet._scan_deployments()
    check("_scan TTL-cached returns same data", set(cached) == set(found))

    # ---------------------------------------------------------------- snapshot() (hermetic, down-marked)
    # remove the stray dirs from earlier so snapshot's scan is the clean fixture set
    import shutil
    for junk in ("node_modules", ".hidden", "backups", "nested", "_envtest", "_enclave_compose",
                 "_plain_compose"):
        shutil.rmtree(rootp / junk, ignore_errors=True)
    _point_at(root)
    snap = fleet.snapshot()
    check("snapshot finds fixture agents", {"alpha", "beta"} <= set(snap))
    a = snap["alpha"]
    check.eq("snapshot alpha down (no docker)", a["up"], False)
    check.eq("snapshot alpha tick=down", a["tick"], "down")
    check.eq("snapshot alpha brain", a["brain"], "claude")
    check.eq("snapshot alpha model", a["model"], "claude-sonnet-4-6")
    check.eq("snapshot beta brain=api", snap["beta"]["brain"], "api")
    check.eq("snapshot beta model=qwen", snap["beta"]["model"], "qwen")
    check.eq("snapshot home points at <dir>/home", a["home"], str(rrootp / "alpha" / "home"))
    check.eq("snapshot headline carried through", a["headline"], "alpha is working on something")
    check("snapshot work_open carried through", a["work_open"] == 2)
    check.eq("snapshot kind standalone (no manager)", a["kind"], "standalone")

    # ---------------------------------------------------------------- snapshot() kind=fleet via manifest
    _point_at(root)
    fleet._manifest = lambda: {"beta": {"manager": "alpha", "tags": ["sub"]}}
    snap2 = fleet.snapshot()
    check.eq("snapshot beta gets manager", snap2["beta"]["manager"], "alpha")
    check.eq("snapshot beta tags", snap2["beta"]["tags"], ["sub"])
    check.eq("snapshot managed agent kind=fleet", snap2["beta"]["kind"], "fleet")
    check.eq("snapshot manager itself kind=fleet", snap2["alpha"]["kind"], "fleet")
    _point_at(root)   # reset manifest

    # ---------------------------------------------------------------- _fmt_age
    check.eq("_fmt_age 0 -> dash", fleet._fmt_age(0), "—")
    now = time.time()
    check.eq("_fmt_age seconds", fleet._fmt_age(now - 30), "30s")
    check.eq("_fmt_age minutes", fleet._fmt_age(now - 600), "10m")
    check.eq("_fmt_age hours", fleet._fmt_age(now - 7200), "2h")

    # ------------------------------------------------------- cmd_start is state-aware
    # `up` and `restart` were two buttons with an invisible difference and a real trap: `compose
    # restart` bounces the SAME container, so an agent.env edit silently does not apply. cmd_start
    # is the single verb the console sends — it must pick the right compose call from live state.
    calls = []
    _orig_compose, _orig_resolve = fleet._compose, fleet._resolve
    fleet._compose = lambda a, *args, **kw: calls.append(list(args))
    try:
        fleet._resolve = lambda aid: {"id": "alpha", "dir": str(root), "up": False}
        fleet.cmd_start("alpha")
        check.eq("cmd_start on a STOPPED agent brings the stack up", calls[-1], ["up", "-d"])

        fleet._resolve = lambda aid: {"id": "alpha", "dir": str(root), "up": True}
        fleet.cmd_start("alpha")
        check("cmd_start on a RUNNING agent force-recreates it (applies config)",
              calls[-1][:3] == ["up", "-d", "--force-recreate"], f"{calls[-1]}")
        check("cmd_start on a RUNNING agent leaves chat/relay alone (--no-deps agent)",
              "--no-deps" in calls[-1] and calls[-1][-1] == "agent", f"{calls[-1]}")
        check("cmd_start never uses bare `restart` (it would skip config changes)",
              not any(c and c[0] == "restart" for c in calls), f"{calls}")
    finally:
        fleet._compose, fleet._resolve = _orig_compose, _orig_resolve

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
