"""Unit tests for console.py's pure-ish helper functions (no HTTP server needed for most).

Covers the data-shaping logic the dashboard depends on: uptime parsing, log tailing, cap reading,
alert thresholds, the fleet graph builder, per-agent activity (event noise-filtering + work split +
recent ticks), recent-commits, and the operator-stopped marker. Hermetic — builds a temp fleet.

Run: python3 test_console_helpers.py
"""
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F

check = F.Check()


def main():
    root = F.build_fleet(specs=[
        {"id": "alpha", "headline": "alpha builds the thing",
         "ext_spend": [{"model": "gemini", "usd": 0.02}]},
        {"id": "beta", "brain": "api", "model": "qwen",
         "cap": {"five_hour": {"pct": 12}, "seven_day": {"pct": 34}}},
    ])
    con, base, stop = F.boot_console(root)
    try:
        # ---- _uptime_s: docker RFC3339 w/ nanoseconds ----
        u = con._uptime_s("2020-01-01T00:00:00.123456789Z")
        check("_uptime_s parses nanosecond RFC3339", isinstance(u, int) and u > 0)
        check("_uptime_s None on garbage", con._uptime_s("not-a-date") is None)
        check("_uptime_s None on empty", con._uptime_s("") is None)

        # ---- _tail_lines ----
        f = pathlib.Path(root) / "tail.txt"
        f.write_text("\n".join(str(i) for i in range(100)))
        tl = con._tail_lines(f, 5)
        check.eq("_tail_lines returns last n", tl, ["95", "96", "97", "98", "99"])
        check.eq("_tail_lines missing file -> []", con._tail_lines(pathlib.Path(root) / "nope", 5), [])

        # ---- _snap_homes / _discover_homes ----
        paths, snap = con._snap_homes()
        check("_snap_homes finds both agents", set(paths) >= {"alpha", "beta"})
        check("_snap_homes path ends usage.jsonl", all(p.endswith("usage.jsonl") for p in paths.values()))
        check("_discover_homes finds agents", set(con._discover_homes()) >= {"alpha", "beta"})

        # ---- _read_cap: falls back to the per-agent claude-usage.json ----
        # force the fallback path (the live OAuth probe is host-dependent — None it out for hermeticity)
        con._capusage.fetch = lambda *a, **k: None
        cap = con._read_cap(paths)
        check("_read_cap reads fixture cap", isinstance(cap, dict)
              and (cap.get("seven_day") or {}).get("pct") == 34)

        # ---- _alerts: thresholds + up-but-unreachable ----
        snap2 = {"x": {"up": True, "reachable": False}}
        al = con._alerts(snap2, {}, {"seven_day": {"pct": 92}, "five_hour": {"pct": 95}})
        msgs = " ".join(a["msg"] for a in al)
        check("_alerts crit on weekly>=90", any(a["level"] == "crit" for a in al))
        check("_alerts flags unreachable", "unreachable" in msgs)
        check("_alerts quiet when healthy", con._alerts({}, {}, {}) == con._alerts({}, {}, {})
              and all(a["level"] != "crit" for a in con._alerts({}, {}, {})))

        # ---- _build_graph ----
        g = con._build_graph(snap, paths, {})
        ids = {n["id"] for n in g["nodes"]}
        check("_build_graph nodes include agents", {"alpha", "beta"} <= ids)
        check("_build_graph shape", isinstance(g.get("links"), list))

        # ---- _agent_activity: event noise filtering + work split + recent ticks ----
        home = pathlib.Path(paths["alpha"]).parent.parent
        act = con._agent_activity(home)
        summaries = " ".join(e["summary"] for e in act["events"])
        check("activity drops rollup-write noise", "rollup.md" not in summaries)
        check("activity keeps real edits", "game.js" in summaries or "build" in summaries)
        check("activity work split (doing/todo/done)",
              act["work"]["done"] == 1 and len(act["work"]["doing"]) == 1 and len(act["work"]["todo"]) == 1)
        check("activity recent_ticks populated", len(act["recent_ticks"]) >= 1)
        check("activity loop config read", act["loop"].get("BRAIN") in ("claude", "api"))

        # ---- _recent_commits: real git repo under the work dir ----
        if _have_git():
            wd = pathlib.Path(root) / "wd"
            repo = wd / "myrepo"
            repo.mkdir(parents=True)
            _git(repo, "init", "-q")
            _git(repo, "config", "user.email", "t@t.t")
            _git(repo, "config", "user.name", "t")
            (repo / "a.txt").write_text("hi")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-q", "-m", "first commit here")
            commits = con._recent_commits(str(wd))
            check("_recent_commits finds the commit",
                  any("first commit" in c["msg"] for c in commits))
        else:
            print("  (skip _recent_commits: git not available)")
        check("_recent_commits empty dir -> []", con._recent_commits(str(pathlib.Path(root) / "void")) == [])

        # ---- _set_operator_stopped marker ----
        marker = home / "state" / ".operator-stopped"
        con._set_operator_stopped("alpha", True)
        check("_set_operator_stopped writes marker", marker.exists())
        con._set_operator_stopped("alpha", False)
        check("_set_operator_stopped clears marker", not marker.exists())

        # ---- _monitor_alerts: reads the heartbeat, respects observe/off mode ----
        import json
        hb = con.MON_HEARTBEAT
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(json.dumps({"agents": {
            "alpha": {"mode": "alert", "findings": [{"severity": "high", "title": "stalled"}]},
            "beta": {"mode": "observe", "findings": [{"severity": "high", "title": "ignored"}]},
        }}))
        ma = con._monitor_alerts()
        text = " ".join(a["msg"] for a in ma)
        check("_monitor_alerts surfaces alerting agent", "alpha" in text and "stalled" in text)
        check("_monitor_alerts silences observe-mode agent", "beta" not in text)
    finally:
        stop()

    raise SystemExit(check.report())


def _have_git():
    try:
        subprocess.run(["git", "--version"], capture_output=True, timeout=4)
        return True
    except Exception:
        return False


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=10)


if __name__ == "__main__":
    main()
