"""Integration tests for console.py's HTTP API (the Enclave fleet console).

Boots the REAL console on an ephemeral loopback port against a temp fixture fleet, with no docker
needed (hermetic). Covers every GET endpoint's status + shape, the bad-id/traversal guards, the
static/shell routes, the token gate, and the POST CSRF/origin/validation gates. Docker-touching POST
paths (action/config) are asserted only for a structured no-crash response — never for success.

Run: python3 test_console_api.py
"""
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F

check = F.Check()


def _stream_status(base, path):
    """Open an SSE connection, grab the status, close immediately (don't block on the body)."""
    try:
        r = urllib.request.urlopen(base + path, timeout=5)
        st = r.status
        r.close()
        return st
    except urllib.error.HTTPError as e:
        return e.code


def _is_dict(x):
    return isinstance(x, dict)


def main():
    root = F.build_fleet(specs=[
        {"id": "alpha", "headline": "alpha builds the thing",
         "ext_spend": [{"model": "gemini", "usd": 0.02}]},
        {"id": "beta", "brain": "api", "model": "qwen",
         "cap": {"five_hour": {"pct": 12}, "seven_day": {"pct": 34}}},
    ])
    # give alpha a real skill file so /api/skills + /api/skillfile have a 200 path to hit
    skdir = pathlib.Path(root) / "alpha" / "home" / "skills"
    skdir.mkdir(parents=True, exist_ok=True)
    (skdir / "test-skill.md").write_text("# Test skill\nDoes a useful thing.\n")

    con, base, stop = F.boot_console(root, token="", hermetic=True)
    try:
        # ---------- shell + static (served BEFORE the token gate) ----------
        code, body = F.get(base, "/", raw=True)
        check.eq("/ returns 200", code, 200)
        check("/ is html with 'Enclave'", "Enclave" in body)
        code, _ = F.get(base, "/static/chart.umd.min.js", raw=True)
        check.eq("/static/chart.umd.min.js 200", code, 200)
        code, _ = F.get(base, "/static/force-graph.min.js", raw=True)
        check.eq("/static/force-graph.min.js 200", code, 200)
        code, _ = F.get(base, "/static/../console.py", raw=True)
        check.eq("/static traversal -> 404", code, 404)
        code, _ = F.get(base, "/static/nope.js", raw=True)
        check.eq("/static missing -> 404", code, 404)
        code, _ = F.get(base, "/api/does-not-exist")
        check.eq("unknown api path -> 404", code, 404)

        # ---------- /api/fleet ----------
        code, b = F.get(base, "/api/fleet")
        check.eq("/api/fleet 200", code, 200)
        check("/api/fleet shape {agents,ts,alerts}",
              _is_dict(b) and {"agents", "ts", "alerts"} <= set(b))
        check("/api/fleet lists fixture agents", {"alpha", "beta"} <= set(b["agents"]))

        # ---------- /api/overview ----------
        code, b = F.get(base, "/api/overview")
        check.eq("/api/overview 200", code, 200)
        check("/api/overview shape", _is_dict(b) and {"usage", "cap", "graph"} <= set(b))
        # VALUE assertions (not just shape): the fixture spreads 3 ticks/agent across ~6.5 days, so the
        # window-cutoff logic is exercised — 7d sees all 6 (2 agents x 3) and strictly more than 'today'
        # (only the ~1h tick per agent). Also proves aggregation isn't silently empty-but-shaped.
        u = b.get("usage", {})
        t7 = (u.get("7d", {}) or {}).get("fleet") or {}
        ttoday = (u.get("today", {}) or {}).get("fleet") or {}
        twtd = (u.get("wtd", {}) or {}).get("fleet") or {}
        check.eq("/api/overview 7d window sees all fixture ticks (2x3)", t7.get("ticks"), 6)
        check("/api/overview window cutoff distinguishes today<=wtd<=7d AND today<7d",
              ttoday.get("ticks", 0) <= twtd.get("ticks", 0) <= t7.get("ticks", 0)
              and t7.get("ticks", 0) > ttoday.get("ticks", 0),
              f"today={ttoday.get('ticks')} wtd={twtd.get('ticks')} 7d={t7.get('ticks')}")
        check("/api/overview 7d cost actually aggregated (>0)", (t7.get("cost_usd") or 0) > 0, f"{t7}")

        # ---------- /api/graph ----------
        code, b = F.get(base, "/api/graph")
        check.eq("/api/graph 200", code, 200)
        check("/api/graph shape {nodes,links}", _is_dict(b) and "nodes" in b and "links" in b)
        check("/api/graph nodes include the fixture agents (value, not shape)",
              {"alpha", "beta"} <= {n.get("id") for n in (b.get("nodes") or [])})

        # ---------- /api/usage.csv ----------
        code, b = F.get(base, "/api/usage.csv?window=wtd", raw=True)
        check.eq("/api/usage.csv 200", code, 200)
        check("/api/usage.csv header row", b.splitlines()[0].startswith("agent,cost_usd"))
        check("/api/usage.csv has FLEET total", "FLEET" in b)
        code, b = F.get(base, "/api/usage.csv?window=bogus", raw=True)
        check.eq("/api/usage.csv bad window falls back to 200", code, 200)

        # ---------- /api/logs (raw + activity) ----------
        code, b = F.get(base, "/api/logs?id=alpha&kind=raw&tail=50", raw=True)
        check.eq("/api/logs raw 200", code, 200)
        check("/api/logs raw has runner content", "tick" in b)
        code, b = F.get(base, "/api/logs?id=alpha&kind=activity", raw=True)
        check.eq("/api/logs activity 200", code, 200)
        check("/api/logs activity has rollup content", "rollup" in b or "alpha" in b)
        code, b = F.get(base, "/api/logs?id=../etc", raw=True)
        check.eq("/api/logs bad id -> 400", code, 400)
        check("/api/logs bad id no stack trace", "Traceback" not in b)
        code, b = F.get(base, "/api/logs?id=Bad%20Name", raw=True)
        check.eq("/api/logs bad id (space) -> 400", code, 400)

        # ---------- /api/diagnostics ----------
        code, b = F.get(base, "/api/diagnostics?id=alpha")
        check.eq("/api/diagnostics 200", code, 200)
        check("/api/diagnostics returns json", _is_dict(b))
        code, b = F.get(base, "/api/diagnostics?id=../etc", raw=True)
        check.eq("/api/diagnostics bad id -> 400", code, 400)
        check("/api/diagnostics bad id no stack trace", "Traceback" not in b)

        # ---------- /api/activity ----------
        code, b = F.get(base, "/api/activity?id=alpha")
        check.eq("/api/activity 200", code, 200)
        check("/api/activity shape", _is_dict(b) and "work" in b and "events" in b)
        code, b = F.get(base, "/api/activity?id=Bad%20Name", raw=True)
        check.eq("/api/activity bad id -> 400", code, 400)

        # ---------- /api/config ----------
        code, b = F.get(base, "/api/config?id=alpha")
        check.eq("/api/config 200", code, 200)
        check("/api/config shape {env,editable,path}",
              _is_dict(b) and {"env", "editable", "path"} <= set(b))
        code, b = F.get(base, "/api/config?id=../etc", raw=True)
        check.eq("/api/config bad id -> 400", code, 400)

        # ---------- /api/doctor ----------
        code, b = F.get(base, "/api/doctor?id=alpha")
        check.eq("/api/doctor 200", code, 200)
        check("/api/doctor shape {ok,checks}", _is_dict(b) and "ok" in b and "checks" in b)
        code, _ = F.get(base, "/api/doctor?id=ghost")
        check.eq("/api/doctor unknown agent -> 404", code, 404)
        code, _ = F.get(base, "/api/doctor?id=../etc")
        check.eq("/api/doctor bad id -> 400", code, 400)

        # ---------- /api/resources ----------
        code, b = F.get(base, "/api/resources?id=alpha")
        check.eq("/api/resources 200", code, 200)
        check("/api/resources shape {running}", _is_dict(b) and "running" in b)
        code, _ = F.get(base, "/api/resources?id=ghost")
        check.eq("/api/resources unknown agent -> 404", code, 404)
        code, _ = F.get(base, "/api/resources?id=../etc")
        check.eq("/api/resources bad id -> 400", code, 400)

        # ---------- /api/presets ----------
        code, b = F.get(base, "/api/presets")
        check.eq("/api/presets 200", code, 200)
        check("/api/presets shape",
              _is_dict(b) and {"presets", "brains", "modes", "models"} <= set(b))

        # ---------- /api/models ----------
        code, b = F.get(base, "/api/models")
        check.eq("/api/models 200", code, 200)
        check("/api/models has archetypes", _is_dict(b) and "archetypes" in b)

        # ---------- /api/monitor ----------
        code, b = F.get(base, "/api/monitor")
        check.eq("/api/monitor 200", code, 200)
        check("/api/monitor shape",
              _is_dict(b) and {"running", "pid_alive", "stale", "heartbeat"} <= set(b))

        # ---------- /api/escalations ----------
        code, b = F.get(base, "/api/escalations")
        check.eq("/api/escalations 200", code, 200)
        check("/api/escalations shape {items}", _is_dict(b) and isinstance(b.get("items"), list))

        # ---------- /api/secrets-available ----------
        code, b = F.get(base, "/api/secrets-available")
        check.eq("/api/secrets-available 200", code, 200)
        check("/api/secrets-available shape",
              _is_dict(b) and "available" in b and "lib_configured" in b)

        # ---------- /api/services ----------
        code, b = F.get(base, "/api/services")
        check.eq("/api/services 200", code, 200)
        check("/api/services shape", _is_dict(b) and isinstance(b.get("services"), list))

        # ---------- /api/skills ----------
        code, b = F.get(base, "/api/skills?id=alpha")
        check.eq("/api/skills 200", code, 200)
        check("/api/skills shape {skills,memory_index}",
              _is_dict(b) and "skills" in b and "memory_index" in b)
        check("/api/skills finds the skill file",
              any(s.get("name") == "test-skill.md" for s in b["skills"]))
        code, _ = F.get(base, "/api/skills?id=../etc")
        check.eq("/api/skills bad id -> 400", code, 400)

        # ---------- /api/skillfile (param is `name=`; path-traversal guarded) ----------
        code, b = F.get(base, "/api/skillfile?id=alpha&name=test-skill.md", raw=True)
        check.eq("/api/skillfile 200", code, 200)
        check("/api/skillfile returns content", "Test skill" in b)
        code, _ = F.get(base, "/api/skillfile?id=alpha&name=../console.py")
        check.eq("/api/skillfile traversal name -> 400", code, 400)
        code, _ = F.get(base, "/api/skillfile?id=../etc&name=x.md")
        check.eq("/api/skillfile bad id -> 400", code, 400)

        # ---------- /api/audit ----------
        code, b = F.get(base, "/api/audit")
        check.eq("/api/audit 200", code, 200)
        check("/api/audit shape {entries}", _is_dict(b) and isinstance(b.get("entries"), list))

        # ---------- /api/goal + /api/mission (GET) ----------
        code, b = F.get(base, "/api/goal?id=alpha")
        check.eq("/api/goal GET 200", code, 200)
        check("/api/goal shape {goal}", _is_dict(b) and "goal" in b)
        code, b = F.get(base, "/api/mission?id=alpha")
        check.eq("/api/mission GET 200", code, 200)
        check("/api/mission shape", _is_dict(b) and "claude_md" in b and "tick_txt" in b)

        # ---------- /api/usage (no such endpoint — documents current 404) ----------
        code, _ = F.get(base, "/api/usage?window=wtd")
        check.eq("/api/usage (no endpoint) -> 404", code, 404)

        # ---------- /api/stream (SSE — connect, assert 200, close) ----------
        check.eq("/api/stream connects 200", _stream_status(base, "/api/stream"), 200)

        # ================= POST: CSRF / origin / validation gates =================
        # CSRF header missing -> 403
        code, _ = F.post(base, "/api/action", {"action": "down", "id": "alpha"}, csrf=False)
        check.eq("POST without CSRF header -> 403", code, 403)
        # bad Origin -> 403
        code, _ = F.post(base, "/api/action", {"action": "down", "id": "alpha"},
                         origin="http://evil.com")
        check.eq("POST with foreign Origin -> 403", code, 403)
        # bad json -> 400
        req = urllib.request.Request(base + "/api/action", data=b"{not json",
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-Requested-With", "fetch")
        req.add_header("Origin", "http://127.0.0.1")
        try:
            r = urllib.request.urlopen(req, timeout=5)
            jcode = r.status
        except urllib.error.HTTPError as e:
            jcode = e.code
        check.eq("POST bad json -> 400", jcode, 400)
        # bad action -> 400
        code, _ = F.post(base, "/api/action", {"action": "explode", "id": "alpha"})
        check.eq("POST bad action -> 400", code, 400)
        # bad id -> 400
        code, _ = F.post(base, "/api/action", {"action": "down", "id": "../etc"})
        check.eq("POST action bad id -> 400", code, 400)

        # ---------- /api/config POST validation ----------
        code, _ = F.post(base, "/api/config", {"id": "../etc", "preset": "x"})
        check.eq("POST config bad id -> 400", code, 400)
        code, _ = F.post(base, "/api/config", {"id": "alpha"})  # no preset|brain|mode|updates
        check.eq("POST config no directive -> 400", code, 400)

        # ---------- /api/goal POST ----------
        code, _ = F.post(base, "/api/goal", {"id": "../etc", "text": "x"})
        check.eq("POST goal bad id -> 400", code, 400)
        code, b = F.post(base, "/api/goal", {"id": "alpha", "text": "ship the MVP"})
        check.eq("POST goal valid -> 200", code, 200)
        check("POST goal ok", _is_dict(b) and b.get("ok") is True)
        gf = pathlib.Path(root) / "alpha" / "home" / "state" / "phase-goal.txt"
        check("POST goal wrote phase-goal.txt", gf.exists() and "ship the MVP" in gf.read_text())
        code, b = F.post(base, "/api/goal", {"id": "ghost", "text": "x"})  # no home
        check.eq("POST goal unknown agent -> 400", code, 400)

        # ---------- /api/mission POST ----------
        code, _ = F.post(base, "/api/mission", {"id": "../etc", "claude_md": "x"})
        check.eq("POST mission bad id -> 400", code, 400)
        code, _ = F.post(base, "/api/mission", {"id": "alpha"})  # nothing writable
        check.eq("POST mission nothing to write -> 400", code, 400)
        code, b = F.post(base, "/api/mission", {"id": "alpha", "claude_md": "# Mission\nbe good\n"})
        check.eq("POST mission valid -> 200", code, 200)
        check("POST mission wrote", _is_dict(b) and "CLAUDE.md" in (b.get("wrote") or []))

        # ---------- /api/diag-mute then /api/diag-fix ----------
        code, _ = F.post(base, "/api/diag-mute", {"id": "alpha", "key": "BAD KEY"})
        check.eq("POST diag-mute bad key -> 400", code, 400)
        code, b = F.post(base, "/api/diag-mute", {"id": "alpha", "key": "duration_spike",
                                                  "severity": "high"})
        check.eq("POST diag-mute valid -> 200", code, 200)
        check("POST diag-mute ok + muted list",
              _is_dict(b) and b.get("ok") is True and "duration_spike" in (b.get("muted") or []))
        mf = pathlib.Path(root) / "alpha" / "home" / "state" / ".diag-mute.json"
        check("diag-mute wrote .diag-mute.json", mf.exists())

        code, _ = F.post(base, "/api/diag-fix", {"id": "alpha", "key": "no_such_anomaly"})
        check.eq("POST diag-fix unknown key -> 400", code, 400)
        code, b = F.post(base, "/api/diag-fix", {"id": "alpha", "key": "duration_spike"})
        check.eq("POST diag-fix valid -> 200", code, 200)
        check("POST diag-fix applied contract",
              _is_dict(b) and b.get("ok") is True and "applied" in b and "label" in b)
        envf = pathlib.Path(root) / "alpha" / "home" / "agent.env"
        check("diag-fix patched agent.env", "MAX_TURNS=40" in envf.read_text())

        # ---------- /api/create ----------
        code, _ = F.post(base, "/api/create", {"name": "Bad Name"})
        check.eq("POST create bad name -> 400", code, 400)
        code, b = F.post(base, "/api/create", {"name": "test-new", "brain": "claude"})
        check.eq("POST create valid -> 200", code, 200)
        check("POST create ack contract",
              _is_dict(b) and b.get("ok") is True and "queued" in b)
        qf = pathlib.Path(b["queued"])
        check("create wrote spec to queue", qf.exists() and json.loads(qf.read_text())["name"] == "test-new")
        # re-queue same name -> 409
        code, _ = F.post(base, "/api/create", {"name": "test-new", "brain": "claude"})
        check.eq("POST create duplicate -> 409", code, 409)

        # ---------- docker-touching POSTs: must NOT 500. Hermetically (no real container) the verb
        # FAILS gracefully -> HTTP 200 with ok:false. A 500 = unhandled server exception = test failure
        # (external review: accepting 500 made the suite "lie"). ----------
        code, b = F.post(base, "/api/action", {"action": "down", "id": "alpha"})
        check.eq("POST action down -> 200 (NOT 500)", code, 200)
        check("POST action down -> structured {ok: bool}",
              _is_dict(b) and isinstance(b.get("ok"), bool), f"{code} {b}")
        code, b = F.post(base, "/api/config", {"id": "alpha", "updates": {"MAX_TURNS": "30"}})
        check.eq("POST config updates -> 200 (NOT 500)", code, 200)
        check("POST config updates -> structured {ok: bool}",
              _is_dict(b) and isinstance(b.get("ok"), bool), f"{code} {b}")

        # ---------- /api/services/restart: allowlist-gated host-bridge restart ----------
        # unknown / non-controllable service -> 400 (no bridge-control map configured by default)
        code, b = F.post(base, "/api/services/restart", {"name": "nope"})
        check.eq("services/restart unknown service -> 400", code, 400)
        code, b = F.post(base, "/api/services/restart", {})
        check.eq("services/restart no name -> 400", code, 400)
        # controllable via a harmless `cmd` (run `true`, no real bridge touched) -> ok:true, no crash
        con.MON_BRIDGE_CONTROL = {"safesvc": {"cmd": "true"}, "badlabel": {"label": "bad label!"}}
        code, b = F.post(base, "/api/services/restart", {"name": "safesvc"})
        check("services/restart cmd path -> ok",
              code == 200 and _is_dict(b) and b.get("ok") is True and b.get("service") == "safesvc")
        code, b = F.post(base, "/api/services/restart", {"name": "badlabel"})
        check.eq("services/restart rejects bad launchd label -> 400", code, 400)
        con.MON_BRIDGE_CONTROL = {}
    finally:
        stop()

    # ================= token gate (separate console; module globals are shared) =================
    root2 = F.build_fleet(specs=[{"id": "gamma"}])
    con2, base2, stop2 = F.boot_console(root2, token="secret", hermetic=True)
    try:
        code, b = F.get(base2, "/api/fleet")  # no token
        check.eq("token gate: no token -> 401", code, 401)
        code, b = F.get(base2, "/api/fleet", token="secret")
        check.eq("token gate: with token -> 200", code, 200)
        check("token gate: authed fleet has gamma", _is_dict(b) and "gamma" in b.get("agents", {}))
        code, b = F.get(base2, "/api/fleet", token="wrong")
        check.eq("token gate: wrong token -> 401", code, 401)
        # shell + static stay open even with a token set
        code, _ = F.get(base2, "/", raw=True)
        check.eq("token gate: / still open", code, 200)
        # POST also requires the token
        code, _ = F.post(base2, "/api/goal", {"id": "gamma", "text": "x"})
        check.eq("token gate: POST no token -> 401", code, 401)
    finally:
        stop2()

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
