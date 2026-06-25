#!/usr/bin/env python3
"""
console.py — Enclave fleet console (P2): one web panel to see + steer 20-100 agents.

Two panes (NOT a table): a left RAIL of agents grouped by manager (the master-agent -> sub-agents
hierarchy) with live status dots, and a right DETAIL pane (Chat / Status / Logs + a directive box).

Architecture (per FLEET-CONSOLE-PLAN.md v2, post-critique):
  • ONE background snapshot thread is the only reader of agent state — it calls fleet.snapshot()
    (disk reads, no per-consumer backend calls) + a bounded TCP probe sweep, into a lock-guarded cache.
    Every /api/fleet, SSE push, and rail read serves that cache. Page loads probe zero backends.
  • Chat is NOT transparently proxied (that couples to every child-UI detail + isn't a real security
    boundary since web_chat is loopback-open). Instead the detail pane IFRAMEs each agent's real chat
    at http://127.0.0.1:<port>/ — clean, robust, and consistent with the operator's "loopback-trusted,
    console is not the only door (the comms bridge is the multi-party steering plane)" decision.
  • MUTATIONS (up/down/restart/send) delegate to fleet.py — the validated+audited privilege helper —
    via subprocess; the web process never calls docker directly.
  • Security: binds 127.0.0.1 ONLY (refused otherwise); optional CONSOLE_TOKEN gate; Origin check +
    session on state-changing POSTs; bounded SSE with heartbeats.
  • Status + hierarchy (see docs/FLEET-CONSOLE-PLAN.md): every view reads ONE canonical status model
    (JS `STATUS`/`statusKey` over up/tick/reachable → Working/Idle/Unreachable/Offline, colour+label
    consistent). Standalone-vs-fleet (`kind`) and the master/manager ♛ tree are auto-derived from the
    manager hierarchy; the fleet auto-discovers via `docker compose ls` + fleet.py's recursive scan.

Usage: console.py [--port 8700] [--host 127.0.0.1]    Env: CONSOLE_TOKEN (optional), ENCLAVE_STACKS_ROOTS
"""
import os, sys, json, time, threading, socket, subprocess, pathlib, hmac, re, secrets as _secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet   # the control-plane helper (snapshot + lifecycle); read-only here, mutations via subprocess
import usage as _usage          # cost rollups (in-process; reuse aggregate/series/last_record)
import claude_usage as _capusage  # subscription cap % (5h / 7d), cached

def _uptime_s(started):
    """Seconds since a docker StartedAt (RFC3339, e.g. '2026-06-25T19:00:00.123456789Z'). None on
    failure. Docker emits nanoseconds; Python's fromisoformat wants ≤6 fractional digits."""
    try:
        from datetime import datetime as _dt
        s = (started or "").strip().replace("Z", "+00:00")
        if "." in s:
            head, rest = s.split(".", 1)
            digits = ""
            for ch in rest:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            tz = rest[len(digits):]
            s = head + "." + digits[:6] + tz
        return max(0, int(time.time() - _dt.fromisoformat(s).timestamp()))
    except Exception:
        return None


TOKEN = os.environ.get("CONSOLE_TOKEN", "")
PROBE_SECS = 4.0
COST_SECS = float(os.environ.get("CONSOLE_COST_SECS", "45"))  # cost changes per-tick (minutes), not per-4s
_cache = {"agents": {}, "ts": 0}
_lock = threading.Lock()
_sessions = {}   # token -> expiry (process-local; re-auth is one POST)

# Cost/monitoring cache — a SECOND, slower loop (the agent snapshot stays at 4s). Fully fail-open:
# any error here must never wedge the snapshot or the page. Read by /api/overview + /api/fleet(alerts).
STATIC = HERE / "static"
CAP_CACHE = HERE / "state" / "claude-usage.json"
_cost = {"usage": {}, "external": {}, "cap": {}, "series": {}, "alerts": [], "last": {}, "graph": {"nodes": [], "links": []}, "ts": 0}
_cost_lock = threading.Lock()


def _discover_homes():
    """Filesystem fallback so cost telemetry survives docker being down (and agents being stopped):
    use the same deployment scanner as the snapshot (handles standalone agents + correct ids — no
    mistaking a deployment's own home/ for an agent), then locate each one's usage.jsonl."""
    paths = {}
    try:
        for aid, dep in fleet._scan_deployments().items():
            for cand in (pathlib.Path(dep) / "home" / "state" / "usage.jsonl",
                         pathlib.Path(dep) / "state" / "usage.jsonl"):
                if cand.exists():
                    paths.setdefault(aid, str(cand))
                    break
    except Exception:
        pass
    return paths


def _snap_homes():
    """Map agent-id → its usage.jsonl path. Prefer the live snapshot's resolved homes (authoritative,
    layout-agnostic); merge in a filesystem scan so cost still shows when docker/snapshot is empty."""
    with _lock:
        snap = dict(_cache["agents"])
    paths = {aid: str(pathlib.Path(a["home"]) / "state" / "usage.jsonl")
             for aid, a in snap.items() if a.get("home")}
    for aid, p in _discover_homes().items():
        paths.setdefault(aid, p)
    return paths, snap


def _read_cap(paths):
    """Subscription cap (5h/7d %). Refresh via the network probe only if an OAuth token is reachable +
    the cache is stale; otherwise fall back to the freshest agent-written claude-usage.json (which sits
    beside each agent's usage.jsonl). Never blocks (fail-open to {})."""
    try:
        data = _capusage.fetch(str(CAP_CACHE), max_age=300)
    except Exception:
        data = None
    if data and "five_hour" in data:
        return data
    best = {}
    for usage_path in paths.values():
        f = pathlib.Path(usage_path).parent / "claude-usage.json"
        try:
            d = json.loads(f.read_text())
            if d.get("ts", 0) > best.get("ts", -1):
                best = d
        except Exception:
            pass
    return best


def _alerts(snap, wtd, cap):
    """Server-side health/cost alerts for the banner. Thresholds mirror the runtime guards (warn 70/85,
    floor 90) so the dashboard and the throttle agree."""
    al = []
    fh = (cap.get("five_hour") or {}).get("pct")
    sd = (cap.get("seven_day") or {}).get("pct")
    if sd is not None and sd >= 90: al.append({"level": "crit", "msg": f"Weekly cap at {sd}% — at/over the defer floor"})
    elif sd is not None and sd >= 85: al.append({"level": "warn", "msg": f"Weekly cap at {sd}% (warn ≥85%)"})
    if fh is not None and fh >= 90: al.append({"level": "crit", "msg": f"5h session at {fh}% — at/over the defer floor"})
    elif fh is not None and fh >= 70: al.append({"level": "warn", "msg": f"5h session at {fh}% (warn ≥70%)"})
    agents = (wtd or {}).get("agents", {})
    for aid, a in agents.items():
        if len(agents) >= 2 and a.get("cost_share_pct", 0) >= 60:
            al.append({"level": "warn", "msg": f"{aid} is {a['cost_share_pct']}% of fleet spend (wtd)"})
    for aid, a in snap.items():
        if a.get("up") and a.get("reachable") is False:
            al.append({"level": "warn", "msg": f"{aid}: container up but chat port unreachable"})
    return al


_PEER_RE = re.compile(r"via comms \((?:peer|ceo):([a-z0-9][a-z0-9_-]*)\)", re.I)


def _build_graph(snap, paths, wtd_agents):
    """Fleet topology for the Graph view: nodes = agents (status/model/spend/work), edges = the manager
    hierarchy (from the snapshot + fleet.json manifest) + peer comms (parsed from each agent's inbox.md
    `via comms (peer:X)` lines). Cached in the slow loop — never parsed per request. Fail-open."""
    try:
        man = fleet._manifest()
    except Exception:
        man = {}
    ids = set(snap) | set(paths) | set(man)
    try:
        ids |= set(fleet._scan_deployments())     # include down / never-started / standalone agents
    except Exception:
        pass
    nodes, have = [], set()
    for aid in sorted(ids):
        a = snap.get(aid, {})
        nodes.append({
            "id": aid,
            "manager": a.get("manager") or man.get(aid, {}).get("manager", "") or "",
            "up": bool(a.get("up")),
            "reachable": bool(a.get("reachable")),
            "tick": a.get("tick") or ("idle" if a else "down"),
            "model": (a.get("model") or "").replace("claude-", ""),
            "cost": round((wtd_agents.get(aid, {}) or {}).get("cost_usd", 0), 2),
            "work_open": a.get("work_open", 0),
        })
        have.add(aid)
    for n in list(nodes):                      # surface a manager that isn't itself a discovered agent
        if n["manager"] and n["manager"] not in have:
            nodes.append({"id": n["manager"], "manager": "", "up": False, "reachable": False, "tick": "down", "model": "", "cost": 0, "work_open": 0})
            have.add(n["manager"])
    links = [{"source": n["manager"], "target": n["id"], "kind": "manager", "count": 1}
             for n in nodes if n["manager"]]
    peer = {}
    for aid, up in paths.items():
        inbox = pathlib.Path(up).parent.parent / "inbox.md"
        try:
            txt = inbox.read_text(errors="ignore")
        except Exception:
            continue
        for m in _PEER_RE.finditer(txt):
            src = m.group(1).lower()
            if src != aid and src in have:
                peer[(src, aid)] = peer.get((src, aid), 0) + 1
    for (src, tgt), c in peer.items():
        links.append({"source": src, "target": tgt, "kind": "peer", "count": c})
    return {"nodes": nodes, "links": links}


def _cost_loop():
    """Compute cost rollups (today/wtd/7d), 7d time-series (by agent/model/reason), the cap reading,
    last-tick per agent, topology graph, and alerts — into one cached dict. Slow cadence; fail-open."""
    while True:
        try:
            paths, snap = _snap_homes()
            wins = {}
            ext = {}
            for w in ("today", "wtd", "7d"):
                cut, _ = _usage.window_cutoff(w)
                fleet_t, agents_t = _usage.aggregate(paths, cut)
                wins[w] = {"fleet": fleet_t, "agents": agents_t}
                # REAL external-API spend ($ out of pocket) from each agent's api_spending.jsonl
                fusd, agext, bym = 0.0, {}, {}
                for aid, up in paths.items():
                    r = _usage.api_rollup(str(pathlib.Path(up).parent / "api_spending.jsonl"), cut)
                    if r["calls"]:
                        agext[aid] = r
                    fusd += r["usd"]
                    for m, v in r["by_model"].items():
                        b = bym.setdefault(m, {"usd": 0.0, "calls": 0})
                        b["usd"] = round(b["usd"] + v["usd"], 4); b["calls"] += v["calls"]
                ext[w] = {"fleet": {"usd": round(fusd, 4), "by_model": bym}, "agents": agext}
            cut7, _ = _usage.window_cutoff("7d")
            ser = {by: _usage.series(paths, cut7, "day", by) for by in ("agent", "model", "reason")}
            last = {aid: _usage.last_record(p) for aid, p in paths.items()}
            cap = _read_cap(paths)
            graph = _build_graph(snap, paths, wins.get("wtd", {}).get("agents", {}))
            alerts = _alerts(snap, wins.get("wtd", {}), cap)
            with _cost_lock:
                _cost.update(usage=wins, external=ext, cap=cap, series=ser, alerts=alerts, last=last, graph=graph, ts=time.time())
        except Exception as e:
            sys.stderr.write(f"[console] cost loop error: {e}\n")
        time.sleep(COST_SECS)


def _probe(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except Exception:
        return False


def _snapshot_loop():
    """The single state reader. fleet.snapshot() = disk reads; add a bounded TCP probe for chat-port
    reachability. Writes one cached dict; all consumers read it. No request thread ever does this work."""
    from concurrent.futures import ThreadPoolExecutor
    while True:
        try:
            snap = fleet.snapshot()
            ports = {aid: a.get("port") for aid, a in snap.items()}
            with ThreadPoolExecutor(max_workers=16) as ex:
                reach = dict(zip(ports, ex.map(_probe, ports.values())))
            for aid, a in snap.items():
                a["reachable"] = reach.get(aid, False)
            with _lock:
                _cache["agents"] = snap
                _cache["ts"] = time.time()
        except Exception as e:
            sys.stderr.write(f"[console] snapshot error: {e}\n")
        time.sleep(PROBE_SECS)


def _fleet_cmd(*args, timeout=60):
    """Delegate a mutation to the validated+audited helper — the web process never calls docker."""
    return subprocess.run([sys.executable, str(HERE / "fleet.py"), *args],
                          capture_output=True, text=True, timeout=timeout)


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Enclave Fleet</title><script src="/static/chart.umd.min.js"></script><script src="/static/force-graph.min.js"></script><style>
/* palette matches web_chat exactly so the console frame + the embedded chat are ONE UI */
:root{--bg:#262624;--card:#30302e;--bd:#3f3f3b;--tx:#ececec;--mut:#9a988f;--accent:#d97757;--hover:#3a3a37;--sel:#403f3b;--ok:#3fbf6f;--idle:#c9a23f;--err:#c2603f;--off:#6f6e68}
body.light{--bg:#faf9f5;--card:#ffffff;--bd:#e7e3d8;--tx:#28261f;--mut:#73726c;--accent:#d97757;--hover:#f3f1ea;--sel:#ece7dc;--off:#b4b2a8}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--tx);height:100vh;display:flex;flex-direction:column}
#nav{display:flex;align-items:center;gap:6px;padding:9px 14px;background:var(--card);border-bottom:1px solid var(--bd);flex:0 0 auto}
#nav .brand{font-size:12.5px;font-weight:700;letter-spacing:.05em;color:var(--mut);margin-right:8px}
#newmodal .nl{display:block;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.03em;margin:11px 0 3px}
.newsecrow input{background:var(--hover);color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:5px 7px;font-size:12px}
.secdrop{position:absolute;left:0;right:0;top:calc(100% + 2px);z-index:60;max-height:190px;overflow:auto;border:1px solid var(--bd);border-radius:8px;background:var(--card);box-shadow:0 8px 26px rgba(0,0,0,.45);display:none}
.secdrop.open{display:block}
.secdrop .opt{padding:7px 11px;cursor:pointer;font-size:12.5px;color:var(--tx);font-family:var(--mono,monospace)}
.secdrop .opt:hover{background:var(--hover)}
.secchips{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--sel);border:1px solid var(--bd);border-radius:14px;padding:3px 7px 3px 11px;font-size:12px;color:var(--tx)}
.chip .x{cursor:pointer;color:var(--mut);font-weight:700;font-size:14px;line-height:1}.chip .x:hover{color:var(--err)}
#newmodal input,#newmodal select,#newmodal textarea{width:100%;box-sizing:border-box;background:var(--hover);color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:7px 9px;font-size:13px;font-family:inherit}
.cfgi{width:100%;box-sizing:border-box;background:var(--hover);color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:4px 7px;font-size:12px}
.info{display:inline-block;width:15px;height:15px;line-height:14px;text-align:center;border-radius:50%;border:1px solid var(--mut);color:var(--mut);font-size:10px;font-style:normal;cursor:pointer;margin-left:6px;font-weight:700;vertical-align:middle;user-select:none}
.info:hover{color:var(--tx);border-color:var(--tx)}
.infopop{position:fixed;z-index:100;max-width:300px;background:var(--card);color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:9px 11px;font-size:12px;line-height:1.5;box-shadow:0 8px 26px rgba(0,0,0,.45)}
.navtab{padding:6px 13px;border-radius:9px;cursor:pointer;color:var(--mut);font-weight:600;font-size:13px}
.navtab:hover{background:var(--hover);color:var(--tx)}.navtab.sel{background:var(--sel);color:var(--tx)}
#nav select,#nav .btn{background:var(--hover);border:1px solid var(--bd);color:var(--tx);border-radius:8px;padding:6px 10px;cursor:pointer;font:inherit;font-size:12.5px}
#body{flex:1;min-height:0;position:relative}
.view{position:absolute;inset:0}
#alertbar{display:flex;flex-direction:column}
.alert{padding:7px 16px;font-size:12.5px;border-bottom:1px solid var(--bd)}
.alert.warn{background:#3a3320;color:#e9d27a}.alert.crit{background:#3a2420;color:#f0a08a}
body.light .alert.warn{background:#fbf3d6}body.light .alert.crit{background:#fbe0d8}
/* ---- Agents view (rail + detail) ---- */
#view-agents{display:flex}
#rail{width:300px;flex:0 0 300px;background:var(--card);border-right:1px solid var(--bd);display:flex;flex-direction:column;overflow:hidden;transition:flex-basis .18s ease,width .18s ease}
body.railcollapsed #rail{width:0;flex-basis:0;border-right:none}
body.railcollapsed #rail>*{opacity:0;pointer-events:none}
#railtoggle{display:none;background:transparent;border:1px solid var(--bd);color:var(--mut);border-radius:8px;width:30px;height:30px;cursor:pointer;font-size:15px;line-height:1;flex:0 0 30px}
body.railcollapsed #railtoggle{display:inline-flex;align-items:center;justify-content:center}
.railx{background:transparent;border:none;color:var(--mut);cursor:pointer;font-size:18px;line-height:1;padding:0 2px;flex:0 0 auto}
.railx:hover{color:var(--tx)}
#railtoggle:hover{background:var(--sel);color:var(--tx)}
#rail h1{font-size:13px;margin:0;padding:13px 14px;color:var(--mut);letter-spacing:.04em;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:9px}
#search{margin:8px;padding:7px 10px;background:var(--bg);border:1px solid var(--bd);border-radius:9px;color:var(--tx);font:inherit}
#list{flex:1;overflow:auto;padding:4px}
.grp{font-size:11px;color:var(--mut);padding:8px 10px 3px;text-transform:uppercase;letter-spacing:.05em}
.row{display:flex;align-items:center;gap:9px;padding:9px 10px;border-radius:9px;cursor:pointer}
.row:hover{background:var(--hover)}.row.sel{background:var(--sel)}
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 9px;display:inline-block}
.dot.working{background:var(--ok)}.dot.idle{background:var(--idle)}.dot.unreachable{background:var(--err)}.dot.offline{background:var(--off)}
.dot.working{box-shadow:0 0 0 3px color-mix(in srgb,var(--ok) 22%,transparent)}
.slabel{font-weight:600}.slabel.working{color:var(--ok)}.slabel.idle{color:var(--idle)}.slabel.unreachable{color:var(--err)}.slabel.offline{color:var(--off)}
.rid{font-weight:600}.rmeta{font-size:11.5px;color:var(--mut)}
/* manager / master hierarchy markers */
.crown{color:var(--accent);margin-right:5px}
.mgrbadge{font-size:9px;font-weight:700;color:var(--accent);border:1px solid var(--accent);border-radius:6px;padding:0 5px;margin-left:7px;letter-spacing:.04em;vertical-align:middle;white-space:nowrap}
.row.master{background:linear-gradient(90deg,color-mix(in srgb,var(--accent) 9%,transparent),transparent)}
.tree{color:var(--bd);font-size:13px;flex:0 0 auto;margin-right:-3px;user-select:none}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#bar{padding:11px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px}
#bar .t{font-weight:700;font-size:15px}#bar .m{color:var(--mut);font-size:12.5px}
.btn{background:var(--hover);border:1px solid var(--bd);color:var(--tx);border-radius:8px;padding:6px 11px;cursor:pointer;font:inherit;font-size:12.5px}
.btn:hover{background:var(--sel)}.btn.danger:hover{background:#3a2420;border-color:#c2603f}
.tabs{display:flex;gap:4px;padding:8px 14px 0}.tab{padding:6px 12px;border-radius:8px 8px 0 0;cursor:pointer;color:var(--mut)}.tab.sel{background:var(--card);color:var(--tx)}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:7px;overflow:hidden}
.segb{padding:3px 11px;border:none;background:transparent;color:var(--mut);cursor:pointer;font-size:12px;font-weight:600}
.segb.sel{background:var(--sel);color:var(--tx)}.segb:hover:not(.sel){background:var(--hover)}
#pane{flex:1;background:var(--card);margin:0 0 0 0;overflow:auto;min-height:0;display:flex;flex-direction:column}
iframe{flex:1;border:0;width:100%;background:var(--bg)}
#status,#logs{padding:16px;white-space:pre-wrap;font:12.5px ui-monospace,Menlo,monospace;color:var(--tx);overflow:auto}
#dbox{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--bd)}
#dtext{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:9px;color:var(--tx);padding:8px 11px;font:inherit}
.empty{margin:auto;color:var(--mut)}
/* ---- Overview view (compact, width-capped) ---- */
#view-overview{display:none;overflow:auto;padding:14px}
.ovwrap{max-width:880px;margin:0 auto}
.fleetstrip{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.fchip{background:var(--card);border:1px solid var(--bd);border-radius:11px;padding:8px 12px;display:flex;align-items:center;gap:8px;min-width:90px;cursor:pointer}
.fchip:hover{background:var(--hover)}.fchip .fn{font-size:19px;font-weight:800;font-variant-numeric:tabular-nums}
.fchip .fl{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.fchip.tot{cursor:default}.fchip.tot:hover{background:var(--card)}
.fchip.zero{opacity:.45}
.toprow{display:flex;gap:10px;align-items:stretch;margin-bottom:12px}
.gaugewrap{display:flex;flex-direction:column;gap:5px;flex:0 0 auto}
.gaugerow{display:flex;gap:8px}
.creditschip{font-size:9px;color:var(--mut);text-align:center;white-space:nowrap;letter-spacing:.02em}
.gaugecard{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:8px 10px;display:flex;flex-direction:column;align-items:center;min-width:88px}
.gauge{width:66px;height:66px}.gv{font-size:20px;font-weight:800}
.glabel{font-size:10px;color:var(--tx);font-weight:600;margin-top:3px;text-align:center}
.gsub{font-size:9.5px;color:var(--mut);margin-top:1px;text-align:center}
.ovgrid{flex:1;min-width:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
@media(max-width:720px){.toprow{flex-wrap:wrap}.ovgrid{grid-template-columns:repeat(2,minmax(0,1fr))}}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:9px 11px}
.card .k{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.03em}
.card .v{font-size:18px;font-weight:700;margin-top:2px}
.card .s{font-size:10.5px;color:var(--mut);margin-top:2px}
.badge{display:inline-block;font-size:10px;padding:1px 7px;border-radius:20px;border:1px solid var(--bd);color:var(--mut);margin-top:4px}
.sectit{font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.05em;margin:6px 2px}
table.cost{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--bd);border-radius:12px;overflow:hidden;margin-bottom:14px;font-size:12.5px}
table.cost th{font-size:10px;color:var(--mut);text-transform:uppercase;letter-spacing:.03em;text-align:right;padding:7px 9px;cursor:pointer;border-bottom:1px solid var(--bd);user-select:none;white-space:nowrap}
table.cost th:first-child,table.cost td:first-child{text-align:left}
table.cost th:hover{color:var(--tx)}
table.cost td{padding:6px 9px;text-align:right;border-bottom:1px solid var(--bd);font-variant-numeric:tabular-nums}
table.cost tr:last-child td{border-bottom:none}table.cost tbody tr{cursor:pointer}table.cost tbody tr:hover{background:var(--hover)}
.mono{font:11.5px ui-monospace,Menlo,monospace}
.chartsgrid{display:grid;grid-template-columns:2fr 1fr;gap:10px}
.chartcard{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:10px 12px;height:190px}
.chartcard.full{grid-column:1/-1;height:200px}
.chartcard h3{margin:0 0 6px;font-size:11.5px;color:var(--mut);font-weight:600}
.chartcard canvas{max-height:160px}
@media(max-width:760px){.chartsgrid{grid-template-columns:1fr}}
.stale{font-size:11px;color:var(--mut);margin-left:auto}
/* ---- Graph view ---- */
#view-graph{display:none}#graphbox{position:absolute;inset:0}
#glegend{position:absolute;left:14px;bottom:12px;background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:9px 12px;font-size:11.5px;color:var(--mut);z-index:5}
#glegend b{color:var(--tx)}#glegend .li{display:flex;align-items:center;gap:7px;margin-top:4px}
#glegend .sw{width:10px;height:10px;border-radius:50%}
</style></head><body>
<nav id="nav">
  <span class="brand">ENCLAVE FLEET</span>
  <span class="navtab sel" data-v="overview" onclick="view('overview')">Overview</span>
  <span class="navtab" data-v="agents" onclick="view('agents')">Agents</span>
  <span class="navtab" data-v="graph" onclick="view('graph')">Graph</span>
  <span class="navtab" data-v="activity" onclick="view('activity')">Audit</span>
  <span class="navtab" data-v="models" onclick="view('models')">Models</span>
  <span id="winwrap"><select id="win" onchange="renderOverview()"><option value="today">Today</option><option value="wtd" selected>Week-to-date</option><option value="7d">Last 7 days</option></select>
    <button class="btn" onclick="exportCsv()" title="Download usage as CSV">⬇ CSV</button></span>
  <span class="stale" id="stale"></span>
  <button class="btn" onclick="openNew()" title="Create a new agent">+ New Agent</button>
  <button class="btn" id="themebtn" title="Toggle light/dark" onclick="toggleTheme()">🌙</button>
</nav>
<div id="newmodal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:50">
  <div style="max-width:520px;margin:6vh auto;background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:20px;max-height:86vh;overflow:auto">
    <h2 style="margin:0 0 12px">Create agent</h2>
    <label class="nl">name (kebab-case)<span class="info" onclick="showInfo(event,'Becomes the agent id, folder, and container name. Lowercase letters, digits and dashes only.')">i</span></label><input id="n_name" placeholder="my-new-agent">
    <label class="nl">template<span class="info" onclick="showInfo(event,'Starter brain + skills: venture (builds products), autonomous (self-driving), orchestrator (manages sub-agents), ops / analyst / support (focused task agents).')">i</span></label><select id="n_template"><option>venture</option><option>autonomous</option><option>orchestrator</option><option>ops</option><option>analyst</option><option>support</option></select>
    <label class="nl">brain<span class="info" onclick="showInfo(event,'Model tier: claude (Anthropic) | api (OpenAI-compatible provider) | local (model on the Mac) | optimize (start on Claude, drop to the cheapest reachable pool as the cap fills).')">i</span></label><select id="n_brain" onchange="fillNewModels()"><option>claude</option><option>api</option><option>local</option><option>optimize</option></select>
    <label class="nl">model (optional)<span class="info" onclick="showInfo(event,'Pick from the models for the chosen brain, or ✏️ custom… to type one. Leave on (template default) to use the template model.')">i</span></label><select id="n_model" onchange="newModelPick()"></select>
    <label class="nl">heartbeat interval seconds (optional)<span class="info" onclick="showInfo(event,'Max idle seconds between ticks when there is no message. 10800 = 3h. Blank = template default.')">i</span></label><input id="n_interval" placeholder="10800">
    <label class="nl">mission (appended to CLAUDE.md)<span class="info" onclick="showInfo(event,'Plain-English description of what this agent does and how it should behave. Appended to its CLAUDE.md system prompt.')">i</span></label><textarea id="n_mission" rows="4" placeholder="What this agent does…"></textarea>
    <label class="nl">secrets — scoped credentials<span class="info" onclick="showInfo(event,'Credential env files the agent mounts read-only. Search + click to grant existing ones from your library, and/or add a new one (filename + KEY=VALUE). They are written into the agent at creation, so it runs immediately — nothing to fill in later.')">i</span></label>
    <div style="position:relative"><input id="n_secsearch" placeholder="search credentials to add…" autocomplete="off" oninput="secSearch()" onfocus="secSearch()"><div id="n_secdrop" class="secdrop"></div></div>
    <div id="n_secchips" class="secchips"></div>
    <div id="n_newsec"></div>
    <button type="button" class="btn" onclick="addNewSecret()" style="margin-top:7px">➕ new secret</button>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
      <button class="btn" onclick="closeNew()">Cancel</button>
      <button class="btn danger" onclick="submitNew()">Queue create</button></div>
    <div class="s" id="n_msg" style="margin-top:8px"></div>
  </div></div>
<div id="alertbar"></div>
<div id="body">
<section id="view-overview" class="view"><div class="ovwrap">
  <div id="escbox"></div>
  <div class="sectit">Fleet status</div>
  <div class="fleetstrip" id="fleethealth"></div>
  <div class="sectit">Spend &amp; subscription</div>
  <div class="toprow"><div class="gaugewrap" id="gauges"></div><div class="ovgrid" id="cards"></div></div>
  <div class="sectit">Per-agent consumption</div>
  <table class="cost"><thead id="costhead"></thead><tbody id="costbody"></tbody></table>
  <div class="chartsgrid">
    <div class="chartcard full"><h3>Claude cost over time (by agent, $)</h3><canvas id="chTime"></canvas></div>
    <div class="chartcard"><h3>Claude cost by tick reason ($)</h3><canvas id="chReason"></canvas></div>
    <div class="chartcard"><h3>Claude cost by model ($)</h3><canvas id="chModel"></canvas></div>
  </div>
</div></section>
<section id="view-agents" class="view">
  <aside id="rail"><h1><button class="railx" onclick="toggleRail()" title="Collapse panel">−</button><span>AGENTS</span><span id="count" style="margin-left:auto"></span></h1>
  <input id="search" placeholder="filter agents…" autocomplete="off"><div id="list"></div></aside>
  <main id="main">
    <div id="bar"><button id="railtoggle" title="Show agents" onclick="toggleRail()">☰</button>
      <span class="t" id="bt">—</span><span class="m" id="bm"></span><span style="flex:1"></span>
      <button class="btn" onclick="act('restart')">Restart</button>
      <button class="btn danger" onclick="act('down')">Stop</button>
      <button class="btn" onclick="act('up')">Start</button>
      <button class="btn" onclick="openChat()">↗ Chat tab</button></div>
    <div class="tabs"><span class="tab sel" data-t="chat" onclick="tab('chat')">Chat</span>
      <span class="tab" data-t="status" onclick="tab('status')">Status</span>
      <span class="tab" data-t="diag" onclick="tab('diag')">Diagnostics</span>
      <span class="tab" data-t="config" onclick="tab('config')">Config</span>
      <span class="tab" data-t="skills" onclick="tab('skills')">Skills</span>
      <span class="tab" data-t="logs" onclick="tab('logs')">Logs</span></div>
    <div id="pane"><div class="empty">Select an agent from the rail.</div></div>
    <div id="dbox"><input id="dtext" placeholder="Send a directive to this agent (wakes its tick)…"><button class="btn" onclick="sendD()">Send</button></div>
  </main>
</section>
<section id="view-graph" class="view">
  <div id="graphbox"></div>
  <div id="glegend"><b>Fleet topology</b> · node size = wtd spend
    <div class="li"><span class="sw" style="background:var(--ok)"></span>working</div>
    <div class="li"><span class="sw" style="background:var(--idle)"></span>idle</div>
    <div class="li"><span class="sw" style="background:var(--err)"></span>unreachable</div>
    <div class="li"><span class="sw" style="background:var(--off)"></span>offline</div>
    <div class="li"><span style="color:var(--accent)">♛</span> manager (runs a fleet of sub-agents)</div>
    <div class="li"><span class="sw" style="background:#c9a23f;border-radius:2px"></span>manager link · <span class="sw" style="background:#56b6c2;border-radius:2px"></span>peer comms</div>
  </div>
</section>
<section id="view-activity" class="view"><div class="ovwrap">
  <div class="sectit">Audit log <span class="s" style="font-weight:400">— control-plane actions (spawn / lifecycle / config), who &amp; when, newest first</span></div>
  <table class="cost"><thead><tr><th>when</th><th>who</th><th>action</th><th>agent</th><th>detail</th></tr></thead><tbody id="auditbody"></tbody></table>
</div></section>
<section id="view-models" class="view"><div class="ovwrap"><div id="modelsbox"></div></div></section>
</div>
<script>
const TOK=new URLSearchParams(location.search).get("token")||"";
const qs=p=>TOK?(p+(p.includes("?")?"&":"?")+"token="+encodeURIComponent(TOK)):p;
const PAL=["#d97757","#79c0ff","#3fbf6f","#c9a23f","#b58cf0","#e06c9f","#56b6c2","#d0a35c","#8fbf6f","#f08a8a"];
let agents={},sel=null,curtab="chat",curview="overview",ov={},sortKey="claude",sortDir=-1;
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
/* ---------- click (i) -> explanation popover ---------- */
function showInfo(ev,text){ev.stopPropagation();const old=document.getElementById("infopop");if(old)old.remove();
  const p=document.createElement("div");p.id="infopop";p.className="infopop";p.textContent=text;document.body.appendChild(p);
  const r=ev.target.getBoundingClientRect();
  p.style.left=Math.max(8,Math.min(r.left,window.innerWidth-p.offsetWidth-12))+"px";
  p.style.top=(r.bottom+window.innerHeight-r.bottom>p.offsetHeight+10?r.bottom+6:r.top-p.offsetHeight-6)+"px";
  setTimeout(()=>document.addEventListener("click",()=>{const e=document.getElementById("infopop");if(e)e.remove();},{once:true}),0);}
function ic(text){return `<span class="info" onclick="showInfo(event,'${esc(text).replace(/'/g,"\\'")}')">i</span>`;}
const KEY_HELP={
  BRAIN:"Model tier that runs the agent: claude | api (OpenAI-compatible, e.g. NVIDIA free) | local (MLX/Ollama on the Mac) | optimize (start on Claude, drop to the cheapest reachable pool as the cap fills).",
  MODEL:"The top model id for this brain — e.g. claude-opus-4-8, or an NVIDIA model id for api.",
  MODEL_ROUTINE:"Cheaper model used for routine/heartbeat & mechanical ticks when ROUTER=on (e.g. claude-sonnet-4-6).",
  ROUTER:"on = route judgment ticks to MODEL (top) and mechanical ticks to MODEL_ROUTINE (cheap). off = always MODEL.",
  INTERVAL_SECONDS:"Heartbeat: max idle seconds between ticks when there's no message. 10800 = 3h.",
  SUPERVISE:"auto = continuous work loop (prep→do→continue). off = only ticks on a message or the heartbeat.",
  CONTINUOUS_COOLDOWN:"Minimum seconds between back-to-back ticks in auto mode — guards against runaway token burn.",
  TICK_TIMEOUT:"Hard limit (seconds) for a single tick before it's killed.",
  DELEGATION_ENFORCE:"on = a BRAIN=claude manager must hand bulk code to a worker (delegate.py) instead of writing it itself.",
  DELEGATION_MAX_CHARS:"Size threshold (chars) above which the delegation guard blocks manager-written code.",
  PERMISSION:"Claude Code permission mode. 'dangerous' skips per-tool prompts — required for unattended autonomy.",
  WORKDIR:"Subfolder under the agent's home it treats as its working directory.",
  LOCAL_BRAIN_MODEL:"For BRAIN=local: the model name served by the local MLX/Ollama endpoint.",
  LOCAL_BRAIN_BASE:"For BRAIN=local: the base URL of the local OpenAI-compatible server.",
  LOCAL_REQ_TIMEOUT:"For BRAIN=local: request timeout in seconds.",
  GUARD_ALLOW_GIT:"1 = allow the agent to git push via its scoped deploy key (off by default).",
  GUARD_EGRESS_ENFORCE:"1 = enforce the egress allowlist (block outbound to non-allowlisted hosts)."
};
function theme(){return document.body.classList.contains("light")?"light":"dark";}
function cssv(n){return getComputedStyle(document.body).getPropertyValue(n).trim();}
function usd(n){if(n==null)return"—";return"$"+(n<10?n.toFixed(2):n<1000?n.toFixed(1):Math.round(n).toLocaleString());}
function num(n){if(n==null)return"—";return n>=1e6?(n/1e6).toFixed(1)+"M":n>=1e3?(n/1e3).toFixed(1)+"k":(""+n);}
/* ---------- view switching ---------- */
function view(v){curview=v;
  document.querySelectorAll(".navtab").forEach(e=>e.classList.toggle("sel",e.dataset.v===v));
  document.getElementById("view-overview").style.display=v==="overview"?"block":"none";
  document.getElementById("view-agents").style.display=v==="agents"?"flex":"none";
  document.getElementById("view-graph").style.display=v==="graph"?"block":"none";
  document.getElementById("view-activity").style.display=v==="activity"?"block":"none";
  document.getElementById("view-models").style.display=v==="models"?"block":"none";
  document.getElementById("winwrap").style.display=v==="overview"?"":"none";
  try{localStorage.setItem("console_view",v);}catch(e){}
  if(v==="overview"){loadOverview();}else if(v==="graph"){loadGraph();}else if(v==="activity"){loadActivity();}else if(v==="models"){loadModels();}else{render();}
}
/* ---------- canonical status model (ONE source of truth — rail, detail, table, graph) ---------- */
const STATUS={
  working:{label:"Working",col:"--ok"},      // up + mid-tick
  idle:{label:"Idle",col:"--idle"},          // up + reachable, between ticks
  unreachable:{label:"Unreachable",col:"--err"}, // up but chat port not answering — needs attention
  offline:{label:"Offline",col:"--off"},     // not running (stopped/paused/exited)
};
function statusKey(a){
  if(!a||!a.up)return"offline";
  if(a.tick==="working")return"working";
  if(a.reachable===false)return"unreachable";
  return"idle";
}
function statusCol(k){return cssv((STATUS[k]||STATUS.offline).col);}
function statusPill(a){const k=statusKey(a);return `<span class="dot ${k}"></span><span class="slabel ${k}">${STATUS[k].label}</span>`;}
function shortModel(m){return (m||"").replace("claude-","")||"?";}
/* ---------- Agents view ---------- */
function byId(x,y){return x.id<y.id?-1:1;}
function kidsOf(id){return Object.values(agents).filter(a=>a.manager===id);}   // direct sub-agents
function isManager(id){return kidsOf(id).length>0;}                            // runs a fleet of sub-agents
function railRow(a,depth){
  const k=statusKey(a),mgr=isManager(a.id),n=kidsOf(a.id).length;
  const pad=10+depth*17, master=mgr&&depth===0;          // depth-0 manager = the fleet master
  const tree=depth?`<span class="tree">└ </span>`:"";
  const crown=mgr?`<span class="crown" title="manager — runs a fleet of sub-agents">♛</span>`:"";
  const badge=mgr?`<span class="mgrbadge" title="manages ${n} sub-agent(s)">FLEET ·${n}</span>`:"";
  return `<div class="row${sel===a.id?' sel':''}${master?' master':''}" onclick="pick('${a.id}')" style="padding-left:${pad}px">
    ${tree}<span class="dot ${k}"></span><div style="min-width:0"><div class="rid">${crown}${esc(a.id)}${badge}</div>
    <div class="rmeta"><span class="slabel ${k}">${STATUS[k].label}</span> · ${esc(a.brain)}/${esc(shortModel(a.model))} · :${a.port}${a.work_open?" · work "+a.work_open:""}</div></div></div>`;
}
function render(){
  const s=document.getElementById("search");const f=(s.value||"").toLowerCase();
  const all=Object.values(agents);
  const list=all.filter(a=>!f||a.id.toLowerCase().includes(f)||(a.model||"").toLowerCase().includes(f));
  document.getElementById("count").textContent=list.length;
  let h="";
  if(f){ /* filtering: flat list (a tree with hidden parents misleads) — badges still mark managers */
    list.sort(byId).forEach(a=>h+=railRow(a,0));
  }else{
    /* FLEET as a real hierarchy: each master (depth-0 manager) with its sub-agents nested
       beneath it; STANDALONE (independent enclaves not wired into a fleet) in their own section. */
    const ids=new Set(all.map(a=>a.id));
    const fleet=all.filter(a=>a.kind!=="standalone");
    const roots=fleet.filter(a=>!a.manager||!ids.has(a.manager)).sort(byId);
    const seen=new Set();
    const walk=(a,depth)=>{if(seen.has(a.id))return;seen.add(a.id);h+=railRow(a,depth);
      kidsOf(a.id).sort(byId).forEach(c=>walk(c,depth+1));};
    if(roots.length){h+=`<div class="grp">▸ fleet</div>`;roots.forEach(r=>walk(r,0));}
    const standalone=all.filter(a=>a.kind==="standalone").sort(byId);
    if(standalone.length){h+=`<div class="grp">▸ standalone</div>`;standalone.forEach(a=>h+=railRow(a,0));}
  }
  document.getElementById("list").innerHTML=h||`<div class="grp" style="color:var(--mut)">no agents discovered</div>`;
}
function setBar(a){bm.innerHTML=a?`${statusPill(a)} · <span class="mono">${esc(a.status)}</span> · :${a.port}`:"";}
function pick(id){sel=id;if(curview!=="agents")view("agents");render();const a=agents[id];bt.textContent=id;setBar(a);tab(curtab);}
function openChat(){if(sel)window.open("http://127.0.0.1:"+agents[sel].port+"/","_blank");}
function tab(t){curtab=t;if(window._logTimer){clearInterval(window._logTimer);window._logTimer=null;}
  document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("sel",e.dataset.t===t));
  const p=document.getElementById("pane");if(!sel){p.innerHTML='<div class="empty">Select an agent.</div>';return;}
  const a=agents[sel];
  if(t==="chat"){p.innerHTML=`<iframe src="http://127.0.0.1:${a.port}/?theme=${theme()}" allow="microphone; clipboard-write"></iframe>`;}
  else if(t==="status"){
    const lr=(ov.last||{})[sel]||{};const c=(((ov.usage||{}).wtd||{}).agents||{})[sel]||{};
    p.innerHTML=`<div style="padding:16px;overflow:auto">
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px">
        <div class="card"><div class="k">status</div><div class="v" style="font-size:15px">${statusPill(a)}</div><div class="s"><span class="mono">${esc(a.status)}</span> · ${esc(a.brain)}/${esc(shortModel(a.model))} · :${a.port}<br>${a.kind==="standalone"?"standalone (independent enclave)":(isManager(a.id)?"♛ fleet master · manages "+kidsOf(a.id).length+" sub-agent(s)":"fleet · ↳ managed by "+esc(a.manager||"—"))}</div></div>
        <div class="card"><div class="k">wtd spend</div><div class="v">${usd(c.cost_usd)}</div><div class="s">${num(c.tokens)} tokens · ${c.ticks||0} ticks · ${c.cost_share_pct||0}% of fleet</div></div>
        <div class="card"><div class="k">last tick</div><div class="v">${lr.cost_usd!=null?usd(lr.cost_usd):"—"}</div><div class="s">${esc(lr.reason||"")}${lr.model?(" · "+lr.model.replace("claude-","")):""}${lr.rc!=null&&lr.rc!==0?" · rc "+lr.rc:""}</div></div>
      </div>
      ${a.headline?`<div class="card" style="margin-bottom:12px"><div class="k">headline</div><div class="s" style="font-size:12.5px;color:var(--tx);margin-top:3px">${esc(a.headline)}</div></div>`:""}
      <div class="chartcard" style="max-width:580px;margin-bottom:12px"><h3>This agent — cost over time (7d, $)</h3><canvas id="miniChart"></canvas></div>
      <div class="card"><div class="k">runtime</div><div class="s" style="margin-top:4px">
        chat ${a.reachable?"reachable":"<span style='color:var(--err)'>unreachable</span>"} · open work ${a.work_open||0} · home <span class="mono">${esc(a.home||"—")}</span></div>
        <div style="margin-top:9px"><button class="btn" onclick="runDoctor()">🩺 Health check</button><span class="s" id="docout" style="margin-left:10px"></span></div>
        <div id="docchecks" style="margin-top:8px"></div></div></div>`;
    ensureOv().then(()=>drawMini(sel));
  }
  else if(t==="diag"){renderDiag(a);}
  else if(t==="config"){renderConfig(a);}
  else if(t==="skills"){renderSkills(a);}
  else if(t==="logs"){
    p.innerHTML=`<div style="display:flex;align-items:center;gap:12px;padding:6px 12px">
      <span class="seg"><button class="segb sel" id="logActivity" onclick="setLogKind('activity')">Activity</button><button class="segb" id="logRaw" onclick="setLogKind('raw')">Raw</button></span>
      <label class="s"><input type="checkbox" id="logfollow" checked> live tail</label><span class="s" id="logstamp"></span>
      <span style="flex:1"></span><span class="s" id="logkindhint" style="color:var(--mut)">narrative — what the agent did</span></div><div id="logs">loading…</div>`;
    loadLogs(true);window._logTimer=setInterval(()=>{if(curtab==="logs"&&document.getElementById("logfollow")&&document.getElementById("logfollow").checked)loadLogs(false);},2000);
  }
}
let _logKind="activity";
function setLogKind(k){_logKind=k;
  const A=document.getElementById("logActivity"),R=document.getElementById("logRaw");
  if(A)A.classList.toggle("sel",k==="activity");if(R)R.classList.toggle("sel",k==="raw");
  const h=document.getElementById("logkindhint");if(h)h.textContent=k==="activity"?"narrative — what the agent did (state/rollup.md)":"raw tick trace (logs/runner.log)";
  loadLogs(true);}
async function loadLogs(force){if(!sel)return;const e=document.getElementById("logs");if(!e)return;
  const atBottom=Math.abs(e.scrollHeight-e.clientHeight-e.scrollTop)<40;
  try{const x=await(await fetch(qs(`/api/logs?id=${encodeURIComponent(sel)}&tail=300&kind=${_logKind}`))).text();
    if(e.textContent!==x){e.textContent=x;if(force||atBottom)e.scrollTop=e.scrollHeight;}
    const st=document.getElementById("logstamp");if(st)st.textContent="updated "+new Date().toLocaleTimeString();}catch(_){}}
async function runDoctor(){if(!sel)return;const o=document.getElementById("docout"),c=document.getElementById("docchecks");
  if(o){o.style.color="var(--mut)";o.textContent="checking…";}if(c)c.innerHTML="";
  const r=await(await fetch(qs(`/api/doctor?id=${encodeURIComponent(sel)}`))).json().catch(()=>({error:"failed"}));
  if(r.error){if(o){o.style.color="var(--err)";o.textContent=r.error;}return;}
  if(o){o.style.color=r.ok?"var(--ok)":"var(--idle)";o.textContent=r.ok?"all green":"needs attention";}
  if(c)c.innerHTML=(r.checks||[]).map(x=>`<div class="s" style="padding:2px 0"><span style="color:${x.ok?"var(--ok)":"var(--err)"}">${x.ok?"✓":"✗"}</span> ${esc(x.check)}${x.detail?` <span style="color:var(--mut)">— ${esc(x.detail)}</span>`:""}</div>`).join("");}
/* ---------- Diagnostics tab (Phase A) — the Agent Profiler: answer "why slow/expensive/stuck?" ---------- */
let _diag=null;
const HEALTHC={green:"var(--ok)",yellow:"#d4a72c",orange:"#e0883e",red:"var(--err)",unknown:"var(--mut)"};
const SEVC={high:"var(--err)",med:"#e0883e",low:"var(--mut)"};
const HEALTHDOT={green:"🟢",yellow:"🟡",orange:"🟠",red:"🔴",unknown:"⚪"};
/* a trend like ▲+340% / ▼-42%; for context/cost/duration UP is bad (red), for cache%/success UP is good */
function trendBadge(pct,goodWhenDown){if(pct==null)return '<span class="s" style="color:var(--mut)">no baseline yet</span>';
  const up=pct>=0,bad=goodWhenDown?up:!up,col=Math.abs(pct)<5?"var(--mut)":(bad?"var(--err)":"var(--ok)");
  return `<span style="color:${col};font-weight:600">${up?"▲":"▼"}${up?"+":""}${pct}%</span>`;}
function kpi(label,value,trendHtml,sub){return `<div class="card" style="min-width:128px"><div class="k">${label}</div>
  <div class="v">${value}</div><div class="s">${trendHtml||""}${sub?(trendHtml?" · ":"")+sub:""}</div></div>`;}
async function renderDiag(a){const p=document.getElementById("pane");p.innerHTML='<div style="padding:16px">loading diagnostics…</div>';
  let d;try{d=await(await fetch(qs(`/api/diagnostics?id=${encodeURIComponent(sel)}`))).json();}catch(e){p.innerHTML='<div style="padding:16px;color:var(--err)">diagnostics unavailable (agent has no home dir on this host)</div>';return;}
  if(d.error){p.innerHTML='<div style="padding:16px;color:var(--err)">'+esc(d.error)+'</div>';return;}
  _diag=d;
  if(!d.ticks_total){p.innerHTML='<div style="padding:16px;color:var(--mut)">No telemetry yet — this agent hasn\'t logged any ticks. The Diagnostics view fills in once it runs.</div>';return;}
  const h=d.health||{},m=d.metrics||{},ho=d.honesty||{};
  const ctx=m.context||{},cost=m.cost||{},dur=m.duration||{},cache=m.cache_pct||{},turns=m.turns||{};
  const winLbl=d.window==="week"?"vs last week":d.window==="split"?"vs earlier ticks":"building history";
  /* health banner */
  let html=`<div style="padding:14px 16px;overflow:auto">
    <div class="card" style="margin-bottom:12px;border-left:4px solid ${HEALTHC[h.level]||"var(--mut)"}">
      <div style="display:flex;align-items:center;gap:10px"><span style="font-size:20px">${HEALTHDOT[h.level]||"⚪"}</span>
        <div><div class="v" style="font-size:16px;color:${HEALTHC[h.level]||"var(--tx)"}">${esc(h.label||"")}</div>
        <div class="s">${esc(h.reason||"")}</div></div>
        <span style="flex:1"></span><div class="s" style="text-align:right">${d.ticks_total} ticks logged<br>trends ${winLbl}</div></div></div>`;
  /* anomalies engine — the centerpiece */
  if((d.anomalies||[]).length){html+=`<div class="k" style="margin:4px 2px 6px">⚠ Anomalies — what to debug</div>`;
    html+=d.anomalies.map(an=>`<div class="card" style="margin-bottom:8px;border-left:4px solid ${SEVC[an.severity]||"var(--mut)"}">
      <div style="display:flex;align-items:baseline;gap:8px"><div class="v" style="font-size:13.5px">${esc(an.title)}</div>
        <span style="flex:1"></span><span class="s" style="text-transform:uppercase;letter-spacing:.04em;color:${SEVC[an.severity]}">${esc(an.severity)}</span>
        <span class="s" title="how sure we are this reading is real">conf: ${esc(an.confidence||"—")}</span></div>
      <div class="s" style="margin-top:3px;color:var(--tx)">📊 ${esc(an.evidence||"")}</div>
      ${an.cause?`<div class="s" style="margin-top:2px">↳ likely cause: ${esc(an.cause)}</div>`:""}
      ${an.fix?`<div class="s" style="margin-top:2px;color:var(--accent)">→ try: ${esc(an.fix)}</div>`:""}</div>`).join("");}
  else{html+=`<div class="card" style="margin-bottom:10px"><div class="s">✓ No anomalies in recent telemetry. Charts below show the trends.</div></div>`;}
  /* KPI strip */
  html+=`<div style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0">
    ${kpi("context / tick",num(ctx.latest),trendBadge(ctx.trend_pct,false),"input+cache re-sent")}
    ${kpi("cost / tick (avg)",usd(cost.avg),trendBadge(cost.trend_pct,false),"")}
    ${kpi("duration (avg)",dur.avg!=null?(dur.avg>=60?(dur.avg/60).toFixed(1)+"m":Math.round(dur.avg)+"s"):"—",trendBadge(dur.trend_pct,false),"")}
    ${kpi("cache hit",cache.latest!=null?Math.round(cache.latest*100)+"%":"—",trendBadge(cache.trend_pct,false),"of context")}
    ${kpi("turns (avg)",turns.avg!=null?Math.round(turns.avg):"—","","per tick")}
    ${kpi("process success",ho.process_success_pct!=null?ho.process_success_pct+"%":"—","",`${ho.ticks_failed||0} failed`)}
  </div>`;
  /* runtime & resources (live docker stats — Phase B, host-side) */
  html+=`<div id="dgRes" class="card" style="margin-bottom:12px"><div class="k">runtime &amp; resources</div><div class="s" style="margin-top:3px;color:var(--mut)">loading…</div></div>`;
  /* charts — Context (the explosion diagnostic) is the hero */
  html+=`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-bottom:12px">
    <div class="chartcard full"><h3>CONTEXT SIZE per tick — input + cache (the explosion diagnostic)</h3><canvas id="dgCtx"></canvas></div>
    <div class="chartcard"><h3>Cost per tick ($)</h3><canvas id="dgCost"></canvas></div>
    <div class="chartcard"><h3>Tick duration (s)</h3><canvas id="dgDur"></canvas></div></div>`;
  /* behaviour & tools (Phase C — only when the runtime block is present) */
  html+=runtimeSection(d.runtime||{});
  /* honesty panel */
  html+=`<div class="card" style="margin-bottom:12px"><div class="k">honesty panel</div>
    <div class="s" style="margin-top:3px">Process success <b style="color:var(--tx)">${ho.process_success_pct!=null?ho.process_success_pct+"%":"—"}</b> (${(d.ticks_total-(ho.ticks_failed||0))}/${d.ticks_total} ticks, rc=0 &amp; subtype=success)
    · Verification: <b style="color:var(--idle)">${esc(ho.verification||"Unknown")}</b>
    <br><span style="color:var(--mut)">${esc(ho.verification_note||"")}</span></div></div>`;
  /* tick inspector */
  html+=`<div class="k" style="margin:4px 2px 6px">Tick inspector <span class="s" style="font-weight:400">— click a row for raw fields (newest first)</span></div>
    <table class="cost"><thead><tr><th>when</th><th>reason</th><th>model</th><th>context</th><th>cache%</th><th>cost</th><th>dur</th><th>turns</th><th>rc</th></tr></thead><tbody id="dgInspect"></tbody></table>`;
  /* pending telemetry — be honest about what's still not measured (shrinks once runtime data lands) */
  if((d.pending_telemetry||[]).length){
    html+=`<div class="card" style="margin-top:12px;border-style:dashed"><div class="k">pending telemetry${(d.runtime||{}).available?"":" (needs in-container runtime instrumentation → image rebuild)"}</div>
      <div class="s" style="margin-top:3px">${(d.pending_telemetry||[]).map(x=>esc(x)).join(" · ")}</div>
      <div class="s" style="margin-top:4px;color:var(--mut)">${(d.runtime||{}).available?"These genuinely aren't in the event stream (discrete call timing, queue wait, a work-done verdict) — not faked.":"These need the agent runtime to emit per-call events. Not derivable from per-tick totals, so we don't fake them."}</div></div>`;
  }
  html+=`</div>`;
  p.innerHTML=html;
  drawDiagCharts(d);
  renderInspect(d.inspect||[]);
  loadResources();
}
function runtimeSection(rt){
  if(!rt.available){return `<div class="card" style="margin-bottom:12px"><div class="k">behaviour &amp; tools</div>
    <div class="s" style="margin-top:3px;color:var(--mut)">No runtime telemetry on these ticks yet. Per-tool latency, failures, files-modified, delegations, compactions &amp; skill-usage appear once the agent runs on an image with Phase-C instrumentation.</div></div>`;}
  const b=[["tool calls / tick",rt.avg_tool_calls],["tool failures / tick",rt.avg_tool_failures],
    ["files modified / tick",rt.avg_files_modified],["delegations",rt.total_delegations],["compactions",rt.total_compactions]];
  const skills=Object.entries(rt.skills||{}).sort((a,b)=>b[1]-a[1]);
  const trows=(rt.tools||[]).map(t=>`<tr><td style="text-align:left">${esc(t.tool)}</td><td>${t.calls}</td>
    <td style="color:${t.fails?"var(--err)":"var(--mut)"}">${t.fails||0}</td>
    <td>${t.avg_ms!=null?t.avg_ms+"ms":"—"}</td><td>${t.max_ms!=null?t.max_ms+"ms":"—"}</td></tr>`).join("");
  return `<div class="card" style="margin-bottom:12px"><div class="k">behaviour &amp; tools <span class="s" style="font-weight:400">— from runtime instrumentation, last ${rt.ticks_with_data} ticks</span></div>
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin:6px 0 10px">${b.map(c=>`<div><div class="s" style="color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.03em">${c[0]}</div><div style="font-variant-numeric:tabular-nums;font-size:14px;color:var(--tx)">${c[1]!=null?c[1]:"—"}</div></div>`).join("")}</div>
    ${trows?`<table class="cost"><thead><tr><th style="text-align:left">tool</th><th>calls</th><th>fails</th><th>avg latency</th><th>max</th></tr></thead><tbody>${trows}</tbody></table>`:""}
    ${skills.length?`<div class="s" style="margin-top:8px">skills used: ${skills.map(s=>esc(s[0])+" ×"+s[1]).join(" · ")}</div>`:""}</div>`;
}
function fmtUptime(s){if(s==null)return"—";s=Math.floor(s);const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`;}
async function loadResources(){const e=document.getElementById("dgRes");if(!e||!sel)return;
  let r;try{r=await(await fetch(qs(`/api/resources?id=${encodeURIComponent(sel)}`))).json();}catch(_){e.innerHTML='<div class="k">runtime &amp; resources</div><div class="s" style="color:var(--mut)">unavailable</div>';return;}
  if(!r.running){e.innerHTML=`<div class="k">runtime &amp; resources</div><div class="s" style="margin-top:3px">container <b style="color:var(--idle)">${esc(r.status||"not running")}</b>${r.uptime_s!=null?` · was up ${fmtUptime(r.uptime_s)}`:""}${r.restart_count&&r.restart_count!=="0"?` · ${esc(r.restart_count)} restarts`:""}<br><span style="color:var(--mut)">live CPU/memory show when the agent is running</span></div>`;return;}
  const cells=[["CPU",esc(r.cpu_pct||"—")],["memory",esc(r.mem||"—")+(r.mem_pct?` (${esc(r.mem_pct)})`:"")],
    ["uptime",fmtUptime(r.uptime_s)],["restarts",esc(r.restart_count||"0")],["PIDs",esc(r.pids||"—")],
    ["net I/O",esc(r.net_io||"—")],["disk I/O",esc(r.block_io||"—")],["health",esc(r.health&&r.health!=="-"?r.health:"n/a")]];
  e.innerHTML=`<div class="k">runtime &amp; resources <span class="s" style="font-weight:400">— live (docker stats)</span></div>
    <div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:6px">${cells.map(c=>`<div><div class="s" style="color:var(--mut);font-size:10px;text-transform:uppercase;letter-spacing:.03em">${c[0]}</div><div style="font-variant-numeric:tabular-nums;font-size:13px;color:var(--tx)">${c[1]}</div></div>`).join("")}</div>`;}
function renderInspect(rows){const tb=document.getElementById("dgInspect");if(!tb)return;
  tb.innerHTML=rows.map((r,i)=>`<tr onclick="toggleInspect(${i})"><td style="text-align:left">${esc((r.ts||"").replace("T"," ").replace("Z",""))}</td>
    <td style="text-align:left">${esc(r.reason||"")}</td><td style="text-align:left">${esc((r.model||"").replace("claude-",""))}</td>
    <td>${num(r.context)}</td><td>${r.cache_pct}%</td><td>${usd(r.cost_usd)}</td><td>${r.duration_s}s</td><td>${r.turns}</td>
    <td style="color:${r.rc?"var(--err)":"var(--mut)"}">${r.rc}</td></tr>
    <tr id="dgexp${i}" style="display:none"><td colspan="9" style="text-align:left"><pre class="mono" style="white-space:pre-wrap;margin:0;font-size:11px;color:var(--mut)">${esc(JSON.stringify(r,null,1))}</pre></td></tr>`).join("");}
function toggleInspect(i){const e=document.getElementById("dgexp"+i);if(e)e.style.display=e.style.display==="none"?"table-row":"none";}
function drawDiagCharts(d){if(typeof Chart==="undefined")return;
  const s=d.series||{},L=s.labels||[];
  Chart.defaults.color=cssv("--mut");Chart.defaults.borderColor=cssv("--bd");Chart.defaults.font.family="-apple-system,system-ui,sans-serif";
  const fmtTok=v=>v>=1e6?(v/1e6).toFixed(1)+"M":v>=1e3?(v/1e3).toFixed(0)+"k":v;
  /* stacked composition: input (fresh) + cache_read (replayed) + cache_write — reveals WHAT is big */
  mkChart("dgCtx",{type:"bar",data:{labels:L,datasets:[
      {label:"input",data:s.input||[],backgroundColor:"#79c0ff"},
      {label:"cache_read",data:s.cache_read||[],backgroundColor:cssv("--accent")},
      {label:"cache_write",data:s.cache_write||[],backgroundColor:"#b58cf0"}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom",labels:{boxWidth:9,font:{size:9}}}},
      scales:{x:{stacked:true,grid:{display:false},ticks:{maxTicksLimit:8,font:{size:8}}},y:{stacked:true,ticks:{callback:fmtTok}}}}});
  mkChart("dgCost",{type:"bar",data:{labels:L,datasets:[{data:s.cost||[],backgroundColor:cssv("--accent")}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{display:false},ticks:{maxTicksLimit:6,font:{size:8}}},y:{ticks:{callback:v=>"$"+v}}}}});
  mkChart("dgDur",{type:"line",data:{labels:L,datasets:[{data:s.duration||[],borderColor:cssv("--ok"),backgroundColor:"transparent",tension:.25,pointRadius:0}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{display:false},ticks:{maxTicksLimit:6,font:{size:8}}},y:{ticks:{callback:v=>v+"s"}}}}});
}
/* ---------- Config tab (P0/P2) — EDIT LOCALLY, then ONE Save applies + restarts once ---------- */
const MODE_HELP={autonomous:"continuous — prep→do→continue (SUPERVISE=auto)",chat:"reply-only — wakes on messages (SUPERVISE=off)",scheduled:"heartbeat cadence (SUPERVISE=off + INTERVAL_SECONDS)"};
let _cfgEnv={},_cfgEditable=[],_cfgMeta={brains:["claude","api","local","optimize"],modes:["autonomous","chat","scheduled"],presets:[],defs:{}},_pending={},_cfgAdvanced=false;
function setCfgAdv(v){_cfgAdvanced=v;drawConfig();}
function _pendAdvCount(){const simple=["BRAIN","MODEL","SUPERVISE"];return Object.keys(_pending).filter(k=>!simple.includes(k)).length;}
function effV(k){return _pending[k]!==undefined?_pending[k]:(_cfgEnv[k]||"");}
function effMode(){if(effV("SUPERVISE")==="auto")return"autonomous";const iv=effV("INTERVAL_SECONDS");return (iv&&iv!=="10800")?"scheduled":"chat";}
function pend(k,v){if(String(_cfgEnv[k]||"")===String(v))delete _pending[k];else _pending[k]=String(v);updateDirty();}
function updateDirty(){const n=Object.keys(_pending).length;const b=document.getElementById("dirty");
  if(b){b.textContent=n?(n+" unsaved change"+(n>1?"s":"")):"no unsaved changes";b.style.color=n?"var(--idle)":"var(--mut)";}
  const sv=document.getElementById("saveBtn");if(sv)sv.disabled=!n;const dc=document.getElementById("discardBtn");if(dc)dc.disabled=!n;}
async function renderConfig(a){const p=document.getElementById("pane");p.innerHTML='<div style="padding:16px">loading config…</div>';
  let cfg={};
  try{cfg=await(await fetch(qs(`/api/config?id=${encodeURIComponent(sel)}`))).json();}catch(e){p.innerHTML='<div style="padding:16px;color:var(--err)">config unavailable (agent has no home dir on this host)</div>';return;}
  if(cfg.error){p.innerHTML='<div style="padding:16px;color:var(--err)">'+esc(cfg.error)+'</div>';return;}
  try{_cfgMeta=await(await fetch(qs("/api/presets"))).json();}catch(e){}
  _cfgEnv=cfg.env||cfg;_cfgEditable=cfg.editable||Object.keys(_cfgEnv).filter(k=>!k.startsWith("_")).sort();_pending={};
  let goal="";try{goal=((await(await fetch(qs(`/api/goal?id=${encodeURIComponent(sel)}`))).json()).goal)||"";}catch(e){}
  window._cfgGoal=goal;
  p.innerHTML='<div style="padding:16px;overflow:auto;height:100%"><div id="cfgmain"></div><div id="cfggoal"></div></div>';
  drawConfig();drawGoal();
}
function drawGoal(){const g=document.getElementById("cfggoal");if(!g)return;
  g.innerHTML=`<div class="card" style="margin-top:12px"><div class="k">phase goal — autonomous steering${ic("The work goal the off-Opus supervisor reads each cycle to fill the agent's task queue. Only BRAIN=local/optimize agents use it. Saving writes the goal; it does NOT restart the agent.")}</div>
    <div class="s" style="margin:3px 0 7px">The off-Opus supervisor (BRAIN=local/optimize agents) reads this each cycle to set the work queue. Saving does NOT restart the agent.</div>
    <textarea id="goalIn" rows="3" style="width:100%;box-sizing:border-box;background:var(--hover);color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:8px;font-family:inherit;font-size:13px">${esc(window._cfgGoal||"")}</textarea>
    <div style="margin-top:8px"><button class="btn" onclick="saveGoal()">Save goal</button><span class="s" id="goalmsg" style="margin-left:10px"></span></div></div>`;}
async function saveGoal(){if(!sel)return;const t=document.getElementById("goalIn").value;const m=document.getElementById("goalmsg");
  if(m){m.style.color="var(--mut)";m.textContent="saving…";}
  const r=await postR("/api/goal",{id:sel,text:t});
  if(m){if(r&&r.ok){m.style.color="var(--ok)";m.textContent="✓ goal saved (applies next supervisor cycle)";window._cfgGoal=t;}else{m.style.color="var(--err)";m.textContent="error: "+esc((r&&r.error)||"failed");}}}
function drawConfig(){const p=document.getElementById("cfgmain");if(!p)return;const mode=effMode();
  const brainOpts=_cfgMeta.brains.map(b=>`<option ${effV("BRAIN")===b?"selected":""}>${b}</option>`).join("");
  const known=(_cfgMeta.models&&_cfgMeta.models[effV("BRAIN")])||[];const curM=effV("MODEL");
  const modelOpts=[...new Set([...(curM?[curM]:[]),...known])].map(m=>`<option ${m===curM?"selected":""}>${esc(m)}</option>`).join("")+(curM?"":`<option value="" selected>(none)</option>`)+`<option value="__custom__">✏️ custom…</option>`;
  const presetBtns=(_cfgMeta.presets||[]).map(n=>`<button class="btn" onclick="presetLocal('${n}')">${esc(n)}</button>`).join(" ");
  const modeBtns=_cfgMeta.modes.map(m=>`<button class="btn ${m===mode?"danger":""}" title="${MODE_HELP[m]||""}" onclick="modeLocal('${m}')">${m}${m===mode?" ✓":""}</button>`).join(" ");
  /* BRAIN/MODEL/SUPERVISE are set by the cards above (dropdowns/buttons) — don't repeat them as free
     text here. Model-valued keys (MODEL_ROUTINE→claude tier, LOCAL_BRAIN_MODEL→local) render as dropdowns. */
  const HIDE=["BRAIN","MODEL","SUPERVISE"],MODELKEYS={MODEL_ROUTINE:"claude",LOCAL_BRAIN_MODEL:"local"};
  const rows=_cfgEditable.filter(k=>!HIDE.includes(k)).map(k=>{const ch=_pending[k]!==undefined;const cur=effV(k);let field;
    if(MODELKEYS[k]!==undefined){const lst=(_cfgMeta.models&&_cfgMeta.models[MODELKEYS[k]])||[];
      const opts=[...new Set([...(cur?[cur]:[]),...lst])].map(m=>`<option ${m===cur?"selected":""}>${esc(m)}</option>`).join("")+(cur?"":'<option value="" selected>(none)</option>')+'<option value="__custom__">✏️ custom…</option>';
      field=`<select class="cfgi" data-k="${esc(k)}" onchange="cfgModelPick(this)">${opts}</select>`;}
    else field=`<input class="cfgi" data-k="${esc(k)}" value="${esc(cur)}" oninput="pend(this.dataset.k,this.value)">`;
    return `<tr><td class="mono" style="color:${ch?"var(--idle)":"var(--mut)"}">${ch?"• ":""}${esc(k)}${KEY_HELP[k]?ic(KEY_HELP[k]):""}</td><td>${field}</td></tr>`;}).join("");
  p.innerHTML=`
    <div class="card" style="margin-bottom:12px"><div class="k">brain${ic(KEY_HELP.BRAIN)}</div>
      <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
        <select id="brainSel" onchange="pend('BRAIN',this.value);drawConfig()">${brainOpts}</select>
        <select id="modelSel" onchange="modelPick(this.value)" style="flex:1">${modelOpts}</select></div>
      <div class="s" style="margin-top:5px">brain sets the pool; model is the list for that brain (pick ✏️ custom… to type one)</div></div>
    <div class="card" style="margin-bottom:12px"><div class="k">run mode${ic("How the agent runs. Autonomous = continuous work loop (SUPERVISE=auto). Chat = only wakes when you message it. Scheduled = wakes on a fixed heartbeat interval.")}</div>
      <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">${modeBtns}</div>
      <div class="s" style="margin-top:5px">${esc(MODE_HELP[mode]||"")}</div></div>
    <div class="card" style="margin-bottom:12px"><div class="k">presets${ic("One-click config profiles. Clicking one FILLS the fields below (brain/mode/etc.) for you to review — nothing is applied until you Save.")}</div>
      <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">${presetBtns||"<span class='s'>none</span>"}</div></div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px"><span class="seg"><button class="segb ${_cfgAdvanced?"":"sel"}" onclick="setCfgAdv(false)">Simple</button><button class="segb ${_cfgAdvanced?"sel":""}" onclick="setCfgAdv(true)">Advanced</button></span>
      <span class="s" style="color:var(--mut)">${_cfgAdvanced?"all editable agent.env keys":"brain / mode / presets — the common settings"}${_pendAdvCount()?` · <span style="color:var(--idle)">${_pendAdvCount()} unsaved in advanced</span>`:""}</span></div>
    ${_cfgAdvanced?`<div class="card"><div class="k">agent.env (editable keys · • = changed)${ic("The agent's runtime settings file. Only safe-to-edit keys are shown; identity/wiring keys (AGENT_ID, ports, secrets) are hidden. Click the i next to a key for what it does.")}</div>
      <table class="cost" style="margin-top:8px"><tbody>${rows}</tbody></table></div>`:""}
    <div style="display:flex;gap:10px;align-items:center;padding:12px 2px">
      <button class="btn danger" id="saveBtn" onclick="saveCfg()" disabled>Save &amp; apply</button>
      <button class="btn" id="discardBtn" onclick="discardCfg()" disabled>Discard</button>
      <span class="s" id="dirty">no unsaved changes</span><span class="s" id="cfgmsg" style="margin-left:auto"></span></div>`;
  updateDirty();
}
function modelPick(v){if(v==="__custom__"){const c=prompt("Model id:",effV("MODEL")||"");if(c!==null)pend("MODEL",c.trim());drawConfig();}else{pend("MODEL",v);}}
function cfgModelPick(el){const k=el.dataset.k;if(el.value==="__custom__"){const c=prompt("Model id:",effV(k)||"");if(c!==null)pend(k,c.trim());drawConfig();}else{pend(k,el.value);}}
function modeLocal(m){if(m==="scheduled"){const iv=prompt("Heartbeat interval seconds:",effV("INTERVAL_SECONDS")||"10800");if(!iv)return;pend("SUPERVISE","off");pend("INTERVAL_SECONDS",iv);}
  else if(m==="autonomous"){pend("SUPERVISE","auto");}
  else{pend("SUPERVISE","off");pend("INTERVAL_SECONDS","10800");}
  drawConfig();}
function presetLocal(name){const d=(_cfgMeta.defs||{})[name];if(!d)return;Object.keys(d).forEach(k=>pend(k,d[k]));drawConfig();}
function discardCfg(){_pending={};drawConfig();}
async function saveCfg(){if(!sel)return;const upd=Object.assign({},_pending);if(!Object.keys(upd).length)return;
  const msg=document.getElementById("cfgmsg");if(msg){msg.style.color="var(--mut)";msg.textContent="saving…";}
  const sv=document.getElementById("saveBtn");if(sv)sv.disabled=true;
  const r=await postR("/api/config",{id:sel,updates:upd});
  if(r&&r.ok){const stopped=/stopped|will apply/i.test(r.out||"");if(msg){msg.style.color="var(--ok)";msg.textContent=stopped?"✓ saved — agent stopped; Start to apply":"✓ saved &amp; applied (agent recreated, live now)";}
    setTimeout(()=>{tab("config");load();},1400);}
  else{if(msg){msg.style.color="var(--err)";msg.textContent="error: "+esc((r&&(r.error||r.out))||"failed");}updateDirty();}}
async function act(action){if(!sel)return;if(action==="down"&&!confirm("Stop "+sel+"?"))return;
  await post("/api/action",{action,id:sel});setTimeout(load,800);}
async function sendD(){if(!sel)return;const t=dtext.value.trim();if(!t)return;dtext.value="";
  await post("/api/action",{action:"send",id:sel,text:t});}
async function post(path,body){try{await fetch(qs(path),{method:"POST",headers:{"Content-Type":"application/json","X-Requested-With":"fetch"},body:JSON.stringify(body)});}catch(e){}}
async function postR(path,body){try{const r=await fetch(qs(path),{method:"POST",headers:{"Content-Type":"application/json","X-Requested-With":"fetch"},body:JSON.stringify(body)});return await r.json();}catch(e){return{error:String(e)};}}
/* ---------- New-agent modal (P1 create) ---------- */
let _newModels={};
async function openNew(){document.getElementById("n_msg").textContent="";document.getElementById("newmodal").style.display="block";document.getElementById("n_newsec").innerHTML="";await fillNewModels();loadSecretsAvail();}
let _secAvail=[],_secSel=new Set(),_secLib=false;
async function loadSecretsAvail(){_secSel=new Set();
  try{const d=await(await fetch(qs("/api/secrets-available"))).json();_secAvail=d.available||[];_secLib=!!d.lib_configured;}catch(e){_secAvail=[];_secLib=false;}
  const s=document.getElementById("n_secsearch");if(s){s.value="";s.placeholder=_secLib?"search credentials to add…":"credential library not set — use ➕ new secret";s.disabled=!_secLib;}
  secDrop([]);renderSecChips();}
function secSearch(){const q=(document.getElementById("n_secsearch").value||"").toLowerCase();
  if(!_secLib)return secDrop([]);
  const m=_secAvail.filter(n=>!_secSel.has(n)&&n.toLowerCase().includes(q)).slice(0,12);secDrop(m);}
function secDrop(list){const d=document.getElementById("n_secdrop");if(!d)return;
  if(!list.length){d.className="secdrop";d.innerHTML="";return;}
  d.className="secdrop open";d.innerHTML=list.map(n=>`<div class="opt" onclick="secAdd('${esc(n)}')">${esc(n)}</div>`).join("");}
function secAdd(n){_secSel.add(n);document.getElementById("n_secsearch").value="";secDrop([]);renderSecChips();}
function secDel(n){_secSel.delete(n);renderSecChips();secSearch();}
function renderSecChips(){const c=document.getElementById("n_secchips");if(!c)return;
  c.innerHTML=[..._secSel].map(n=>`<span class="chip">${esc(n)}<span class="x" onclick="secDel('${esc(n)}')">×</span></span>`).join("");}
function addNewSecret(){const box=document.getElementById("n_newsec");const row=document.createElement("div");
  row.className="newsecrow";row.style.cssText="display:flex;gap:6px;margin-top:6px;align-items:center";
  row.innerHTML=`<input class="ns_name" placeholder="name.env" style="flex:0 0 130px"><input class="ns_val" placeholder="KEY=value" style="flex:1"><span class="info" onclick="this.parentElement.remove()" style="border-color:var(--err);color:var(--err)" title="remove">×</span>`;
  box.appendChild(row);}
function addNewSecret(){const box=document.getElementById("n_newsec");const row=document.createElement("div");
  row.className="newsecrow";row.style.cssText="display:flex;gap:6px;margin-top:6px;align-items:center";
  row.innerHTML=`<input class="ns_name" placeholder="name.env" style="flex:0 0 130px"><input class="ns_val" placeholder="KEY=value" style="flex:1"><span class="info" onclick="this.parentElement.remove()" style="border-color:var(--err);color:var(--err)" title="remove">×</span>`;
  box.appendChild(row);}
async function fillNewModels(){const sel=document.getElementById("n_model");if(!sel)return;
  if(!Object.keys(_newModels).length){try{_newModels=(await(await fetch(qs("/api/presets"))).json()).models||{};}catch(e){}}
  const brain=document.getElementById("n_brain").value;const list=_newModels[brain]||[];const cur=sel.value;
  sel.innerHTML='<option value="">(template default)</option>'+list.map(m=>`<option ${m===cur?"selected":""}>${esc(m)}</option>`).join("")+'<option value="__custom__">✏️ custom…</option>';}
function newModelPick(){const sel=document.getElementById("n_model");if(sel.value==="__custom__"){const c=prompt("Model id:","");if(c&&c.trim()){const o=document.createElement("option");o.textContent=c.trim();o.selected=true;sel.insertBefore(o,sel.lastElementChild);}else{sel.value="";}}}
function closeNew(){document.getElementById("newmodal").style.display="none";}
async function submitNew(){const g=id=>document.getElementById(id).value.trim();
  const name=g("n_name");const msg=document.getElementById("n_msg");
  if(!/^[a-z0-9][a-z0-9_-]*$/.test(name)){msg.style.color="var(--err)";msg.textContent="name must be kebab-case [a-z0-9][a-z0-9_-]*";return;}
  const body={name,template:g("n_template"),brain:g("n_brain")};
  const mdl=g("n_model");if(mdl&&mdl!=="__custom__")body.model=mdl;
  if(g("n_interval"))body.interval_seconds=g("n_interval");
  if(g("n_mission"))body.mission=g("n_mission");
  // (A) chosen existing credentials (chips)
  if(_secSel.size)body.secrets=[..._secSel];
  // (B) new name/value secret files
  const ns=[...document.querySelectorAll("#n_newsec .newsecrow")].map(r=>({name:r.querySelector(".ns_name").value.trim(),content:r.querySelector(".ns_val").value})).filter(x=>x.name&&x.content);
  if(ns.length)body.new_secrets=ns;
  msg.style.color="var(--mut)";msg.textContent="queuing…";
  const r=await postR("/api/create",body);
  if(r&&r.ok){msg.style.color="var(--ok)";msg.textContent=r.note||"queued";setTimeout(()=>{closeNew();load();},2500);}
  else{msg.style.color="var(--err)";msg.textContent="error: "+esc((r&&(r.error||r.out))||"failed");}}
async function load(){try{const j=await(await fetch(qs("/api/fleet"))).json();agents=j.agents||{};renderAlerts(j.alerts||[]);if(curview==="agents"){render();if(sel&&agents[sel]){setBar(agents[sel]);}}else{renderOverview();}}catch(e){}}
/* ---------- alerts ---------- */
function renderAlerts(al){const b=document.getElementById("alertbar");if(!al||!al.length){b.innerHTML="";return;}
  b.innerHTML=al.map(a=>`<div class="alert ${a.level==="crit"?"crit":"warn"}">${a.level==="crit"?"⛔":"⚠"} ${esc(a.msg)}</div>`).join("");}
/* ---------- Overview view ---------- */
async function loadOverview(){try{ov=await(await fetch(qs("/api/overview"))).json();}catch(e){}renderOverview();loadEscalations();}
let _escOpen=false;
async function loadEscalations(){const b=document.getElementById("escbox");if(!b)return;
  let items=[];try{items=(await(await fetch(qs("/api/escalations"))).json()).items||[];}catch(e){return;}
  if(!items.length){b.innerHTML="";return;}
  const show=_escOpen?items:items.slice(0,1);
  b.innerHTML=`<div class="sectit" style="color:var(--idle);cursor:pointer" onclick="_escOpen=!_escOpen;loadEscalations()">⚠ Needs your decision · ${items.length} <span class="s" style="font-weight:400">(${_escOpen?"collapse":"expand"})</span></div>
    <div style="max-height:${_escOpen?"50vh":"auto"};overflow:auto;margin-bottom:10px">`+
    show.map(it=>`<div class="s" style="padding:5px 9px;border-left:2px solid var(--idle);margin-bottom:4px;background:var(--card);border-radius:0 6px 6px 0">
      <b style="color:var(--tx)">${esc(it.agent)}</b> <span class="mono" style="color:var(--mut)">${esc((it.ts||"").slice(0,10))}</span> — ${esc(it.text.slice(0,150))}</div>`).join("")+
    (!_escOpen&&items.length>show.length?`<div class="s" style="color:var(--mut);padding:3px 9px;cursor:pointer" onclick="_escOpen=true;loadEscalations()">+${items.length-show.length} more…</div>`:"")+
    `</div>`;}
async function loadActivity(){const b=document.getElementById("auditbody");if(!b)return;b.innerHTML='<tr><td colspan=5 class="s">loading…</td></tr>';
  try{const j=await(await fetch(qs("/api/audit?n=150"))).json();const es=j.entries||[];
    b.innerHTML=es.length?es.map(e=>{const t=(e.ts||"").replace("T"," ").replace("Z","");
      return `<tr><td class="mono" style="text-align:left">${esc(t)}</td><td style="text-align:left">${esc(e.who||"")}</td><td style="text-align:left"><b>${esc(e.action||"")}</b></td><td style="text-align:left">${esc(e.agent||"")}</td><td class="s" style="text-align:left">${esc(e.detail||e.result||"")}</td></tr>`;}).join(""):'<tr><td colspan=5 class="s">no actions logged yet</td></tr>';
  }catch(e){b.innerHTML='<tr><td colspan=5 class="s" style="color:var(--err)">audit log unavailable</td></tr>';}}
async function loadModels(){const b=document.getElementById("modelsbox");if(!b)return;b.innerHTML='<div class="sectit">loading…</div>';
  let d={};try{d=await(await fetch(qs("/api/models"))).json();}catch(e){}
  const arch=d.archetypes||{};
  if(!Object.keys(arch).length){b.innerHTML=`<div class="sectit">Model recommendations</div><div class="card"><div class="s">${esc(d.note||d.error||"no recommendations available")}</div></div>`;return;}
  const ROLE_HELP={orchestrator:"The agent BRAIN / manager — needs routing, planning, decisions and multi-step instruction-following (what local pods failed at).",coder:"A worker that writes code — graded by actually running its output against tests.",fast:"Cheap high-throughput labor — classify / extract / format. Latency matters most."};
  let h=`<div class="sectit">Model recommendations <span class="s" style="font-weight:400">— ${esc(d.pool||"")} pool · ${d.candidates||0} evaluated${d.excluded&&d.excluded.length?" · "+d.excluded.length+" excluded (throttled-on-free)":""} · pick one in an agent's Config tab</span>${ic("Best model per agent archetype from the capability eval. This page is a decision aid only — set the model in an agent's Config tab.")}</div>`;
  for(const role of Object.keys(arch)){const info=arch[role];
    h+=`<div class="card" style="margin-bottom:12px"><div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div class="k" style="text-transform:capitalize;font-size:13px">${esc(role)}${ic(ROLE_HELP[role]||"")}</div>
      <div class="s">best: <b style="color:var(--ok)">${esc(info.recommend||"—")}</b></div></div>
      <table class="cost" style="margin-top:8px"><thead><tr><th style="text-align:left">model</th><th>score${ic("Weighted capability score for this archetype (0-100), blending the relevant test categories.")}</th><th>p50${ic("Median response latency in seconds — lower is faster.")}</th><th style="text-align:left">categories</th></tr></thead><tbody>`+
      (info.ranked||[]).map(s=>`<tr><td style="text-align:left" class="mono">${esc(s.model)}${s.model===info.recommend?' <span style="color:var(--ok)">★</span>':""}</td><td>${s.score}</td><td>${s.p50}s</td><td style="text-align:left" class="s">${Object.keys(s.cats||{}).map(c=>c+":"+Math.round(s.cats[c])).join("  ")}</td></tr>`).join("")+
      `</tbody></table></div>`;}
  b.innerHTML=h;}
/* ---------- Skills tab (P5: learned-memory vault) ---------- */
async function renderSkills(a){const p=document.getElementById("pane");p.innerHTML='<div style="padding:16px">loading…</div>';
  let d={};try{d=await(await fetch(qs(`/api/skills?id=${encodeURIComponent(sel)}`))).json();}catch(e){p.innerHTML='<div style="padding:16px;color:var(--err)">skills unavailable (agent has no home on this host)</div>';return;}
  if(d.error){p.innerHTML='<div style="padding:16px;color:var(--err)">'+esc(d.error)+'</div>';return;}
  const sk=d.skills||[];
  p.innerHTML=`<div style="padding:16px;overflow:auto;height:100%">
    <div class="sectit">Learned skills · ${sk.length}${ic("Reusable procedures the agent wrote from its own successful runs (its skills/ vault). Click one to read it.")}</div>
    ${sk.length?sk.map(s=>`<div class="card" style="margin-bottom:6px"><div style="cursor:pointer" onclick="viewSkill('${esc(s.name)}',this)"><b class="s" style="color:var(--tx)">${esc(s.name.replace(/\\.md$/,""))}</b><div class="s">${esc(s.desc)}</div></div><div class="skbody"></div></div>`).join(""):'<div class="card"><div class="s">no skills learned yet</div></div>'}
    ${d.memory_index?`<div class="sectit" style="margin-top:16px">Memory index${ic("The agent's learned facts and lessons (memory/INDEX.md) — recalled when relevant on future ticks.")}</div><div class="card"><pre class="s" style="white-space:pre-wrap;margin:0;font-family:inherit">${esc(d.memory_index)}</pre></div>`:""}
  </div>`;}
async function viewSkill(name,head){const b=head.parentElement.querySelector(".skbody");if(!b)return;
  if(b.dataset.open){b.innerHTML="";delete b.dataset.open;return;}
  b.innerHTML='<div class="s">loading…</div>';
  try{const t=await(await fetch(qs(`/api/skillfile?id=${encodeURIComponent(sel)}&name=${encodeURIComponent(name)}`))).text();
    b.innerHTML=`<pre class="s" style="white-space:pre-wrap;margin:8px 0 0;border-top:1px solid var(--bd);padding-top:8px;font-family:inherit">${esc(t)}</pre>`;b.dataset.open="1";}catch(e){b.innerHTML='<div class="s" style="color:var(--err)">failed to load</div>';}}
function gauge(w,label){const pct=w&&w.pct!=null?w.pct:null;const warn=label.indexOf("5h")>=0?70:85;
  /* resolve to real hex — Chrome does NOT substitute var() inside SVG presentation attributes
     (fill=/stroke=), so passing "var(--ok)" there renders black/invisible. */
  const col=cssv(pct==null?"--mut":pct>=90?"--err":pct>=warn?"--idle":"--ok"),track=cssv("--bd");
  const r=42,c=2*Math.PI*r,off=pct==null?c:c*(1-Math.min(pct,100)/100);
  let eta="resets —";if(w&&w.reset_epoch){const s=Math.max(0,w.reset_epoch-Date.now()/1000);eta="resets "+Math.floor(s/3600)+"h"+String(Math.floor(s%3600/60)).padStart(2,"0")+"m";}
  return `<div class="gaugecard" title="Claude subscription ${label} usage — defers at 90%"><svg class="gauge" width="66" height="66" viewBox="0 0 100 100">
    <circle cx=50 cy=50 r=${r} fill=none stroke="${track}" stroke-width=10/>
    <circle cx=50 cy=50 r=${r} fill=none stroke="${col}" stroke-width=10 stroke-linecap=round stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 50 50)"/>
    <text x=50 y=56 text-anchor=middle class=gv fill="${col}">${pct==null?"n/a":pct+"%"}</text></svg>
    <div class="glabel">${label}</div><div class="gsub">${eta}</div></div>`;
}
function renderFleetHealth(){
  /* fleet state at a glance — same status model + colors as the rail/graph. Counts the LIVE snapshot. */
  const list=Object.values(agents||{});
  const cnt={working:0,idle:0,unreachable:0,offline:0};
  let work=0;list.forEach(a=>{cnt[statusKey(a)]++;work+=a.work_open||0;});
  const order=["working","idle","unreachable","offline"];
  const chips=[`<div class="fchip tot"><span class="fn">${list.length}</span><span class="fl">agents</span></div>`];
  order.forEach(k=>{chips.push(`<div class="fchip${cnt[k]?"":" zero"}" onclick="view('agents')" title="${STATUS[k].label} agents">
    <span class="dot ${k}"></span><span class="fn slabel ${k}">${cnt[k]}</span><span class="fl">${STATUS[k].label}</span></div>`);});
  chips.push(`<div class="fchip tot"><span class="fn">${work}</span><span class="fl">open work</span></div>`);
  document.getElementById("fleethealth").innerHTML=chips.join("");
}
function renderOverview(){
  if(curview!=="overview")return;
  renderFleetHealth();
  const win=document.getElementById("win").value;
  const u=((ov.usage||{})[win])||{fleet:{},agents:{}};
  const F=u.fleet||{};const cap=ov.cap||{};
  const ex=((ov.external||{})[win])||{fleet:{usd:0,by_model:{}},agents:{}};
  const exF=ex.fleet||{usd:0,by_model:{}};
  document.getElementById("stale").textContent=ov.ts?("updated "+Math.max(0,Math.round(Date.now()/1000-ov.ts))+"s ago"):"";
  /* Claude subscription gauges (flat-rate; usage counts against the CAP, not the wallet) */
  let g=`<div class="gaugerow">${gauge(cap.five_hour,"5h session")}${gauge(cap.seven_day,"7d weekly")}</div>`;
  g+=`<div class="creditschip" title="Claude is a flat subscription: usage counts against the 5h/7d CAP, not your wallet. Pay-as-you-go credits are ${cap.credits_enabled?"ON":"OFF"}. Real money out the door is the External LLM spend →">Claude subscription · credits ${cap.credits_enabled?"ON":"OFF"}</div>`;
  document.getElementById("gauges").innerHTML=g;
  /* projection from 7d daily series (Claude notional) */
  const sa=(ov.series||{}).agent||{buckets:[],series:{}};
  let daily=sa.buckets.map((_,i)=>Object.values(sa.series).reduce((s,v)=>s+(v.cost[i]||0),0));
  const days=daily.length||1,avg=daily.reduce((a,b)=>a+b,0)/days;
  const exToday=(((ov.external||{}).today||{}).fleet||{}).usd||0;
  const provs=Object.keys(exF.by_model||{});
  const cards=[
    ["External LLM spend ("+win+")",usd(exF.usd||0),(provs.length?provs.length+" model(s) · ":"")+"real $ out-of-pocket (OpenRouter/NVIDIA/pools)"],
    ["Claude usage ("+win+")",usd(F.cost_usd),num(F.tokens)+" tok · subscription, cap-bound (not $ out)"],
    ["Today",usd(exToday)+" ext",usd((((ov.usage||{}).today||{}).fleet||{}).cost_usd)+" Claude"],
    ["Daily burn (Claude 7d avg)",usd(avg),"projected week "+usd(avg*7)],
  ];
  document.getElementById("cards").innerHTML=cards.map(c=>`<div class="card"><div class="k">${c[0]}</div><div class="v">${c[1]}</div><div class="s">${esc(c[2])}</div></div>`).join("");
  renderCostTable(u.agents||{},ex.agents||{});
  drawCharts(u,win);
}
function renderCostTable(ag,agext){
  const cols=[["id","Agent"],["status","Status"],["brain","Brain"],["claude","Claude $"],["external","Ext $"],["tokens","Tokens"],["last","Last tick"]];
  const ids=new Set([...Object.keys(agents||{}),...Object.keys(ag||{}),...Object.keys(agext||{})]);
  const rows=[...ids].map(id=>{
    const live=agents[id]||{},a=ag[id]||{},e=agext[id]||{},lr=(ov.last||{})[id]||{};
    return {id,brain:(live.brain||"?")+"/"+shortModel(live.model),status:statusKey(live),
      claude:a.cost_usd||0,external:e.usd||0,tokens:a.tokens||0,
      last:lr.cost_usd!=null?usd(lr.cost_usd)+" "+(lr.reason||""):(lr.reason||"—"),lastrc:lr.rc};
  });
  rows.sort((x,y)=>{const k=sortKey;const xv=k==="total"?x.claude+x.external:x[k],yv=k==="total"?y.claude+y.external:y[k];return (xv>yv?1:xv<yv?-1:0)*sortDir;});
  document.getElementById("costhead").innerHTML="<tr>"+cols.map(c=>`<th onclick="sortBy('${c[0]}')">${esc(c[1])}${sortKey===c[0]?(sortDir<0?" ▾":" ▴"):""}</th>`).join("")+"</tr>";
  document.getElementById("costbody").innerHTML=rows.map(r=>{
    return `<tr onclick="pick('${r.id}')"><td>${isManager(r.id)?'<span class="crown" title="manager — runs a fleet of sub-agents">♛</span>':""}<b>${esc(r.id)}</b></td>
      <td style="text-align:left"><span class="dot ${r.status}"></span> <span class="slabel ${r.status}">${STATUS[r.status].label}</span></td>
      <td class="mono">${esc(r.brain)}</td>
      <td><b>${r.claude?usd(r.claude):"—"}</b></td>
      <td style="${r.external>0?"color:var(--accent);font-weight:600":""}">${r.external>0?usd(r.external):"—"}</td>
      <td>${r.tokens?num(r.tokens):"—"}</td>
      <td style="${r.lastrc!=null&&r.lastrc!==0?"color:var(--err)":""}">${esc(r.last)}</td></tr>`;
  }).join("")||`<tr><td colspan="7" style="text-align:center;color:var(--mut);padding:18px">No agents discovered.</td></tr>`;
}
function sortBy(k){if(sortKey===k)sortDir=-sortDir;else{sortKey=k;sortDir=(k==="id"||k==="brain")?1:-1;}renderOverview();}
let charts={};
function mkChart(id,cfg){if(charts[id])charts[id].destroy();const el=document.getElementById(id);if(!el)return;charts[id]=new Chart(el,cfg);}
function exportCsv(){window.open(qs("/api/usage.csv?window="+document.getElementById("win").value),"_blank");}
/* vertical marker at the date the lean-tick/off-Opus fix landed — so the burn drop is visible */
const FIX_DATE="2026-06-25";
const fixMarker={id:"fixMarker",afterDraw(c){const i=(c.data.labels||[]).indexOf(FIX_DATE);if(i<0)return;
  const x=c.scales.x.getPixelForValue(c.data.labels[i]),a=c.chartArea,ctx=c.ctx;ctx.save();
  ctx.strokeStyle=cssv("--accent");ctx.setLineDash([4,3]);ctx.lineWidth=1.5;ctx.beginPath();ctx.moveTo(x,a.top);ctx.lineTo(x,a.bottom);ctx.stroke();
  ctx.setLineDash([]);ctx.fillStyle=cssv("--accent");ctx.font="10px system-ui";ctx.textAlign="center";ctx.fillText("tick fix",x,a.top-1);ctx.restore();}};
function drawCharts(u,win){
  if(typeof Chart==="undefined")return;
  const tx=cssv("--tx"),mut=cssv("--mut"),bd=cssv("--bd");Chart.defaults.color=mut;Chart.defaults.borderColor=bd;Chart.defaults.font.family="-apple-system,system-ui,sans-serif";
  /* cost over time, stacked by agent */
  const sa=(ov.series||{}).agent||{buckets:[],series:{}};
  const dsets=Object.entries(sa.series).map(([k,v],i)=>({label:k,data:v.cost,backgroundColor:PAL[i%PAL.length]}));
  mkChart("chTime",{type:"bar",data:{labels:sa.buckets,datasets:dsets},plugins:[fixMarker],
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom",labels:{boxWidth:10,font:{size:10}}}},
      scales:{x:{stacked:true,grid:{display:false}},y:{stacked:true,ticks:{callback:v=>"$"+v}}}}});
  /* cost by reason (sum over 7d series) */
  const sr=(ov.series||{}).reason||{buckets:[],series:{}};
  const rk=Object.keys(sr.series),rv=rk.map(k=>sr.series[k].cost.reduce((a,b)=>a+b,0));
  mkChart("chReason",{type:"bar",data:{labels:rk,datasets:[{data:rv,backgroundColor:rk.map((_,i)=>PAL[i%PAL.length])}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{ticks:{callback:v=>"$"+v}},x:{grid:{display:false}}}}});
  /* cost by model (window) */
  const bm=(u.fleet||{}).by_model||{};const mk=Object.keys(bm),mv=mk.map(k=>bm[k].cost_usd);
  mkChart("chModel",{type:"doughnut",data:{labels:mk.map(m=>m.replace("claude-","")),datasets:[{data:mv,backgroundColor:mk.map((_,i)=>PAL[i%PAL.length]),borderWidth:0}]},
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:"bottom",labels:{boxWidth:10,font:{size:10}}}}}});
}
/* ---------- per-agent mini time-series (Agents → Status) ---------- */
async function ensureOv(){if(ov&&ov.ts)return;try{ov=await(await fetch(qs("/api/overview"))).json();}catch(e){}}
function drawMini(id){if(typeof Chart==="undefined")return;
  const sa=(ov.series||{}).agent||{buckets:[],series:{}};const s=sa.series[id];
  const el=document.getElementById("miniChart");if(!el)return;
  if(!s){el.parentElement.style.display="none";return;}
  mkChart("miniChart",{type:"bar",data:{labels:sa.buckets,datasets:[{data:s.cost,backgroundColor:cssv("--accent")}]},plugins:[fixMarker],
    options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{y:{ticks:{callback:v=>"$"+v}},x:{grid:{display:false}}}}});
}
/* ---------- Graph view (force-directed topology) ---------- */
let G=null,GMGR=new Set();   // GMGR = ids that manage sub-agents (drawn with a crown) — refreshed each render
async function loadGraph(){let d;try{d=await(await fetch(qs("/api/graph"))).json();}catch(e){return;}renderGraph(d);}
function renderGraph(d){
  if(typeof ForceGraph==="undefined"){document.getElementById("graphbox").innerHTML='<div class="empty" style="padding:30px">graph library unavailable</div>';return;}
  const box=document.getElementById("graphbox");
  GMGR=new Set((d.links||[]).filter(l=>l.kind==="manager").map(l=>typeof l.source==="object"?l.source.id:l.source));
  const REL=4,rad=n=>REL*Math.sqrt(1+Math.sqrt(n.cost||0));   // compressed: $ → radius (sqrt), cap stays small
  if(!G){
    G=ForceGraph()(box).nodeId("id").backgroundColor("rgba(0,0,0,0)")
      .nodeLabel(n=>`${GMGR.has(n.id)?"♛ ":""}${n.id}${GMGR.has(n.id)?" (manager)":""}${n.model?" · "+n.model:""} · $${(n.cost||0).toFixed(2)} wtd · work ${n.work_open||0}`)
      .nodeColor(n=>statusCol(statusKey(n))).nodeRelSize(REL).nodeVal(n=>1+Math.sqrt(n.cost||0))
      .linkColor(l=>l.kind==="peer"?"#56b6c2":"#c9a23f").linkWidth(l=>Math.min(4,0.6+(l.count||1)*0.3))
      .linkDirectionalParticles(l=>l.kind==="peer"?2:0).linkDirectionalParticleWidth(2)
      .nodeCanvasObjectMode(()=>"after")
      .nodeCanvasObject((n,ctx,scale)=>{
        const r=rad(n),mgr=GMGR.has(n.id);
        if(n.work_open>0){ctx.beginPath();ctx.arc(n.x,n.y,r+2.5,0,2*Math.PI);ctx.strokeStyle=cssv("--accent");ctx.lineWidth=1.4/scale;ctx.stroke();}
        if(mgr){ctx.beginPath();ctx.arc(n.x,n.y,r+(n.work_open>0?4.5:2.5),0,2*Math.PI);ctx.strokeStyle=cssv("--accent");ctx.lineWidth=2/scale;ctx.setLineDash([3/scale,2/scale]);ctx.stroke();ctx.setLineDash([]);
          const cs=12/scale;ctx.font=`${cs}px system-ui`;ctx.fillStyle=cssv("--accent");ctx.textAlign="center";ctx.textBaseline="bottom";ctx.fillText("♛",n.x,n.y-r-3);}
        const fs=10/scale;ctx.font=`${mgr?"bold ":""}${fs}px -apple-system,system-ui,sans-serif`;ctx.fillStyle=mgr?cssv("--accent"):cssv("--tx");ctx.textAlign="center";ctx.textBaseline="top";
        ctx.fillText(n.id,n.x,n.y+r+2);
      })
      .onNodeClick(n=>pick(n.id));
    try{G.d3Force("charge").strength(-240);}catch(e){}     // spread nodes so they don't overlap
    try{G.d3Force("link").distance(70);}catch(e){}
  }
  G.width(box.clientWidth).height(box.clientHeight).graphData(d);
}
window.addEventListener("resize",()=>{if(G&&curview==="graph"){const b=document.getElementById("graphbox");G.width(b.clientWidth).height(b.clientHeight);}});
/* ---------- theme / chrome ---------- */
function applyThemeBtn(){const b=document.getElementById("themebtn");if(b)b.textContent=theme()==="light"?"🌙":"☀";}
function toggleTheme(){document.body.classList.toggle("light");try{localStorage.setItem("console_theme",theme());}catch(e){}applyThemeBtn();
  const f=document.querySelector("#pane iframe");if(f){try{const u=new URL(f.src);u.searchParams.set("theme",theme());f.src=u.toString();}catch(e){}}
  if(curview==="overview")renderOverview();}
try{if(localStorage.getItem("console_theme")==="light")document.body.classList.add("light");}catch(e){}
applyThemeBtn();
function toggleRail(){const c=document.body.classList.toggle("railcollapsed");try{localStorage.setItem("rail_collapsed",c?"1":"");}catch(e){}}
try{if(localStorage.getItem("rail_collapsed"))document.body.classList.add("railcollapsed");}catch(e){}
document.getElementById("search").addEventListener("input",render);
const _urlView=new URLSearchParams(location.search).get("view");
try{view((["overview","agents","graph"].includes(_urlView)?_urlView:null)||localStorage.getItem("console_view")||"overview");}catch(e){view("overview");}
load();
setInterval(()=>{if(curview==="overview")loadOverview();},15000);
let _lastA="";
try{const es=new EventSource(qs("/api/stream"));es.onmessage=e=>{try{const j=JSON.parse(e.data);const na=j.agents||agents;const k=JSON.stringify(na);
  if(k===_lastA)return;                       /* skip no-op pushes — the rail rebuilt every ~4s and flickered ("page reloads") even when nothing changed */
  _lastA=k;agents=na;if(curview==="agents"){render();if(sel&&agents[sel])setBar(agents[sel]);}else if(curview==="overview")renderOverview();}catch(_){}};}catch(e){setInterval(load,5000);}
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _ok(self):
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query).get("token", [""])[0]
        hdr = self.headers.get("X-Console-Token", "")
        return any(hmac.compare_digest(TOKEN, x) for x in (q, hdr) if x)

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The SPA shell (and API JSON) must never be cached, or a console restart with new code keeps
        # serving the operator's browser the OLD page on reload. Vendored /static/* assets still cache.
        if ctype.startswith("text/html") or ctype.startswith("application/json"):
            self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _static(self, p):
        """Serve a vendored asset (chart.umd.min.js / force-graph.min.js). No traversal; open like '/'."""
        name = p[len("/static/"):]
        if not name or "/" in name or ".." in name:
            return self._send(404, "text/plain", "not found")
        f = STATIC / name
        if not f.is_file():
            return self._send(404, "text/plain", "not found")
        ctype = "application/javascript" if name.endswith(".js") else "text/plain"
        try:
            return self._send(200, ctype, f.read_bytes())
        except Exception:
            return self._send(500, "text/plain", "error")

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE)
        if p.startswith("/static/"):
            return self._static(p)
        if not self._ok():
            return self._send(401, "application/json", '{"error":"unauthorized"}')
        if p == "/api/fleet":
            with _lock:
                agents, ts = _cache["agents"], _cache["ts"]
            with _cost_lock:
                alerts = list(_cost.get("alerts", []))
            return self._send(200, "application/json", json.dumps({"agents": agents, "ts": ts, "alerts": alerts}))
        if p == "/api/overview":
            with _cost_lock:
                return self._send(200, "application/json", json.dumps(_cost))
        if p == "/api/graph":
            with _cost_lock:
                return self._send(200, "application/json", json.dumps(_cost.get("graph", {"nodes": [], "links": []})))
        if p == "/api/usage.csv":
            win = parse_qs(urlparse(self.path).query).get("window", ["wtd"])[0]
            if win not in ("today", "wtd", "7d"):
                win = "wtd"
            with _cost_lock:
                u = _cost.get("usage", {}).get(win) or {}
            cols = ["agent", "cost_usd", "tokens", "input", "output", "cache_read", "cache_write",
                    "ticks", "share_pct", "cost_share_pct"]
            rows = [",".join(cols)]
            for aid, a in sorted((u.get("agents") or {}).items()):
                rows.append(",".join(str(a.get(c, 0)) if c != "agent" else aid for c in cols))
            f = u.get("fleet") or {}
            rows.append(",".join(["FLEET"] + [str(f.get(c, 0)) for c in cols[1:-2]] + ["100", "100"]))
            body = ("\n".join(rows) + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="fleet-usage-{win}.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass
            return
        if p == "/api/logs":
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "text/plain", "bad id")
            n = parse_qs(urlparse(self.path).query).get("tail", ["200"])[0]
            try:
                n = max(20, min(2000, int(n)))
            except ValueError:
                n = 200
            kind = parse_qs(urlparse(self.path).query).get("kind", ["raw"])[0]
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            # Activity = the agent's own narrative (state/rollup.md) — what it DID, human-readable.
            # Raw = the full tick trace (logs/runner.log). Two different audiences, one toggle.
            if kind == "activity" and home:
                rf = pathlib.Path(home) / "state" / "rollup.md"
                if rf.exists():
                    try:
                        return self._send(200, "text/plain; charset=utf-8",
                                          "\n".join(rf.read_text(errors="ignore").splitlines()[-n:]))
                    except Exception:
                        pass
                return self._send(200, "text/plain; charset=utf-8",
                                  "(no activity narrative yet — state/rollup.md is written as the agent works)")
            # Raw: read home/logs/runner.log directly from the cached home (fast, no docker); fall back
            # to `docker compose logs` only if the file is absent.
            logf = pathlib.Path(home) / "logs" / "runner.log" if home else None
            if logf and logf.exists():
                try:
                    tail = logf.read_text(errors="ignore").splitlines()[-n:]
                    return self._send(200, "text/plain; charset=utf-8", "\n".join(tail))
                except Exception:
                    pass
            r = _fleet_cmd("logs", aid, "--tail", str(n), timeout=30)
            return self._send(200, "text/plain; charset=utf-8", (r.stdout or "") + (r.stderr or ""))
        if p == "/api/diagnostics":   # Phase A: the Agent Profiler — context/cost/anomalies from usage.jsonl
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            if not home:
                return self._send(200, "application/json", json.dumps(
                    {"error": "no home dir on this host (telemetry lives in the agent's home/state)"}))
            try:
                import diagnostics
                return self._send(200, "application/json", json.dumps(diagnostics.from_home(home)))
            except Exception as e:
                return self._send(200, "application/json", json.dumps({"error": str(e)}))
        if p == "/api/config":
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            # Read straight from the cached snapshot + files IN-PROCESS — no `enclave fleet` subprocess
            # and no docker call, so opening the Config tab is instant (the slow CLI path re-snapshotted
            # the whole fleet every time). Fall back to the CLI only on a cold cache.
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            if home:
                try:
                    import fleet_config
                    cfg = fleet_config.read_config(home)
                    return self._send(200, "application/json", json.dumps(
                        {"env": cfg["env"], "editable": cfg["editable"], "path": cfg["path"]}))
                except Exception as e:
                    return self._send(400, "application/json", json.dumps({"error": str(e)}))
            r = _fleet_cmd("config", aid, "--json", timeout=15)
            if r.returncode != 0:
                return self._send(400, "application/json", json.dumps({"error": (r.stderr or r.stdout)[-300:]}))
            return self._send(200, "application/json", r.stdout or "{}")
        if p == "/api/presets":   # the named one-click profiles (+ their key/value defs) for the UI
            import fleet_config
            # known model ids per brain, so the Config model field can be a dropdown (no typos).
            # claude tier is the product's supported set; api/optimize models come from the eval
            # recs file (ENCLAVE_MODEL_RECS) when configured; local is unknown -> current+custom only.
            claude_tier = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5-20251001"]
            eval_models = []
            recs_path = os.environ.get("ENCLAVE_MODEL_RECS", "")
            if recs_path and os.path.isfile(recs_path):
                try:
                    rd = json.loads(pathlib.Path(recs_path).read_text())
                    s = set()
                    for info in rd.get("archetypes", {}).values():
                        for r in info.get("ranked", []):
                            s.add(r["model"])
                    eval_models = sorted(s)
                except Exception:
                    pass
            models_by_brain = {"claude": claude_tier, "optimize": claude_tier,
                               "api": eval_models, "local": []}
            return self._send(200, "application/json", json.dumps({
                "presets": sorted(fleet_config.PRESETS), "defs": fleet_config.PRESETS,
                "brains": sorted(fleet_config.BRAINS), "modes": sorted(fleet_config.MODES),
                "models": models_by_brain}))
        if p == "/api/models":   # P4: model-eval recommendations (from an external recs file, if configured)
            recs_path = os.environ.get("ENCLAVE_MODEL_RECS", "")
            if recs_path and os.path.isfile(recs_path):
                try:
                    return self._send(200, "application/json", pathlib.Path(recs_path).read_text())
                except Exception as e:
                    return self._send(200, "application/json", json.dumps({"error": str(e)}))
            return self._send(200, "application/json", json.dumps(
                {"archetypes": {}, "note": "No recommendations configured. Point ENCLAVE_MODEL_RECS "
                 "at a model-eval recommendations JSON (e.g. produced by recommend_setup.py --json)."}))
        if p == "/api/secrets-available":   # NAMES of the scoped-credential library (no values) for create
            lib = os.environ.get("ENCLAVE_SECRETS_LIB", "")
            names = []
            if lib and os.path.isdir(lib):
                names = sorted(f.name for f in pathlib.Path(lib).glob("*.env"))
            return self._send(200, "application/json", json.dumps(
                {"available": names, "lib_configured": bool(lib)}))
        if p == "/api/doctor":   # P3: per-agent wiring health check (in-process, no docker)
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            if not a:
                return self._send(404, "application/json", '{"error":"unknown agent"}')
            import fleet_config
            checks = []
            def chk(name, okv, detail=""):
                checks.append({"check": name, "ok": bool(okv), "detail": str(detail)})
            home, depdir = a.get("home"), a.get("dir")
            chk("home dir present", home and os.path.isdir(home), home or "missing")
            env = fleet_config.read_config(home)["env"] if home else {}
            chk("agent.env readable", bool(env), "")
            chk("brain configured", bool(env.get("BRAIN")), env.get("BRAIN", "—"))
            secs = [s.strip() for s in (env.get("SECRETS", "")).split(",") if s.strip()]
            if secs and depdir:
                missing = [s for s in secs if not os.path.exists(os.path.join(depdir, "secrets", s))]
                chk("scoped secrets mounted", not missing,
                    ("missing: " + ", ".join(missing)) if missing else f"{len(secs)} present")
            chk("container running", a.get("up"), a.get("status", ""))
            port = a.get("port")
            if port:
                sk = socket.socket(); sk.settimeout(0.4)
                try:
                    sk.connect(("127.0.0.1", int(port))); reach = True
                except Exception:
                    reach = False
                finally:
                    sk.close()
                chk("chat port reachable", reach, f":{port}")
            # Host-side runtime checks (universal — not studio-specific): disk headroom + docker daemon.
            try:
                import shutil
                du = shutil.disk_usage(home if home and os.path.isdir(home) else "/")
                free_gb = du.free / 1e9
                chk("disk space", free_gb > 2.0, f"{free_gb:.1f} GB free ({du.free * 100 // du.total}% used)")
            except Exception:
                pass
            chk("docker daemon", bool(fleet._docker("version", "--format", "{{.Server.Version}}").strip()),
                "reachable")
            # Optional host-bridge reachability — endpoints come from ENCLAVE_DOCTOR_BRIDGES (the studio
            # sets its qmd/voice/mlx/… here) so the PRODUCT stays generic. Format: "name:host:port,…".
            for spec in (os.environ.get("ENCLAVE_DOCTOR_BRIDGES", "") or "").split(","):
                spec = spec.strip()
                if not spec or spec.count(":") < 2:
                    continue
                bname, bhost, bport = spec.rsplit(":", 2)
                bsk = socket.socket(); bsk.settimeout(0.4)
                try:
                    bsk.connect((bhost, int(bport))); breach = True
                except Exception:
                    breach = False
                finally:
                    bsk.close()
                chk(f"bridge: {bname}", breach, f"{bhost}:{bport}")
            return self._send(200, "application/json", json.dumps(
                {"ok": all(c["ok"] for c in checks), "checks": checks}))
        if p == "/api/resources":   # Phase B: live container resources (docker stats/inspect; host-side)
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            if not a:
                return self._send(404, "application/json", '{"error":"unknown agent"}')
            # The agent container is named exactly <aid> (its web-chat sibling is <aid>-chat). aid is
            # _SAFE-validated, so it's safe to hand to docker. Empty output = not running / no docker.
            out = {"running": False}
            insp = fleet._docker("inspect", aid, "--format",
                                 "{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}|"
                                 "{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}")
            if insp.strip():
                parts = (insp.strip().split("|") + ["", "", "", ""])[:4]
                status, started, restarts, health = parts
                out["status"] = status
                out["restart_count"] = restarts
                out["health"] = health
                out["running"] = status == "running"
                out["started_at"] = started
                out["uptime_s"] = _uptime_s(started)
            if out["running"]:
                stats = fleet._docker("stats", "--no-stream", "--format", "{{json .}}", aid)
                try:
                    st = json.loads(stats.strip().splitlines()[0])
                    out["cpu_pct"] = st.get("CPUPerc")
                    out["mem"] = st.get("MemUsage")
                    out["mem_pct"] = st.get("MemPerc")
                    out["net_io"] = st.get("NetIO")
                    out["block_io"] = st.get("BlockIO")
                    out["pids"] = st.get("PIDs")
                except Exception:
                    pass
            return self._send(200, "application/json", json.dumps(out))
        if p == "/api/audit":   # P3: recent control-plane actions (who did what, when)
            n = parse_qs(urlparse(self.path).query).get("n", ["80"])[0]
            try:
                n = max(10, min(500, int(n)))
            except ValueError:
                n = 80
            af = pathlib.Path(os.environ.get("ENCLAVE_FLEET_AUDIT",
                              str(pathlib.Path.home() / ".config" / "enclave" / "fleet-audit.log")))
            out = []
            if af.exists():
                for ln in af.read_text(errors="ignore").splitlines()[-n:]:
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        pass
            return self._send(200, "application/json", json.dumps({"entries": out[::-1]}))
        if p == "/api/escalations":   # P3 HITL: the fleet's open "needs a human decision" asks
            with _lock:
                agents = dict(_cache.get("agents") or {})
            items = []
            for aid, a in agents.items():
                home = a.get("home")
                if not home:
                    continue
                st = pathlib.Path(home) / "state"
                # escalations.log: blocks beginning "<ts> ESCALATE :: <text>" (+ indented continuations)
                ef = st / "escalations.log"
                if ef.exists():
                    cur = None
                    for ln in ef.read_text(errors="ignore").splitlines():
                        m = re.match(r"^(\d{4}-\d\d-\d\dT[\d:]+Z)\s+(\w+)\s*::\s*(.*)", ln)
                        if m:
                            if cur and cur["kind"] == "ESCALATE":
                                items.append({"agent": aid, "ts": cur["ts"], "kind": "escalation", "text": cur["text"][:400]})
                            cur = {"ts": m.group(1), "kind": m.group(2), "text": m.group(3)}
                        elif cur and ln.strip():
                            cur["text"] += " " + ln.strip()
                    if cur and cur["kind"] == "ESCALATE":
                        items.append({"agent": aid, "ts": cur["ts"], "kind": "escalation", "text": cur["text"][:400]})
                # approvals.json: a non-empty array = pending approval requests
                aj = st / "approvals.json"
                if aj.exists():
                    try:
                        arr = json.loads(aj.read_text(errors="ignore") or "[]")
                        for it in (arr if isinstance(arr, list) else []):
                            txt = it.get("text") or it.get("msg") or json.dumps(it) if isinstance(it, dict) else str(it)
                            items.append({"agent": aid, "ts": (it.get("ts", "") if isinstance(it, dict) else ""),
                                          "kind": "approval", "text": str(txt)[:400]})
                    except Exception:
                        pass
            items.sort(key=lambda x: x["ts"], reverse=True)
            return self._send(200, "application/json", json.dumps({"items": items[:100]}))
        if p == "/api/goal":   # P3 steering: the autonomous-supervisor goal (state/phase-goal.txt)
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            gf = pathlib.Path(home) / "state" / "phase-goal.txt" if home else None
            txt = gf.read_text(errors="ignore") if (gf and gf.exists()) else ""
            return self._send(200, "application/json", json.dumps({"goal": txt}))
        if p == "/api/skills":   # P5: the agent's learned-memory vault (skills/ + memory index)
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            if not home:
                return self._send(400, "application/json", '{"error":"no home"}')
            skills = []
            sk = pathlib.Path(home) / "skills"
            if sk.is_dir():
                for f in sorted(sk.glob("*.md")):
                    if f.name == "INDEX.md":
                        continue
                    desc = ""
                    try:
                        lines = f.read_text(errors="ignore").splitlines()
                        start = 0
                        if lines and lines[0].strip() == "---":   # skip a YAML frontmatter block
                            for j in range(1, len(lines)):
                                if lines[j].strip() == "---":
                                    start = j + 1; break
                        for ln in lines[start:]:
                            s = ln.strip()
                            if s.startswith("# "):
                                desc = s[2:][:140]; break
                            if s and not s.startswith("#"):
                                desc = s[:140]; break
                    except Exception:
                        pass
                    skills.append({"name": f.name, "desc": desc})
            mi = pathlib.Path(home) / "memory" / "INDEX.md"
            memidx = mi.read_text(errors="ignore")[:8000] if mi.exists() else ""
            return self._send(200, "application/json", json.dumps({"skills": skills, "memory_index": memidx}))
        if p == "/api/skillfile":   # P5: read one skill file (path-traversal-guarded)
            aid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            name = parse_qs(urlparse(self.path).query).get("name", [""])[0]
            if not fleet._SAFE.match(aid or "") or not re.match(r"^[A-Za-z0-9._-]+\.md$", name or ""):
                return self._send(400, "text/plain", "bad request")
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            if not home:
                return self._send(404, "text/plain", "no home")
            skdir = (pathlib.Path(home) / "skills").resolve()
            f = (skdir / name).resolve()
            if not str(f).startswith(str(skdir) + os.sep) or not f.is_file():
                return self._send(404, "text/plain", "not found")
            return self._send(200, "text/plain; charset=utf-8", f.read_text(errors="ignore")[:20000])
        if p == "/api/stream":
            return self._stream()
        return self._send(404, "application/json", '{"error":"not found"}')

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        last = 0
        try:
            for _ in range(600):   # bounded; client (EventSource) auto-reconnects
                with _lock:
                    ts, agents = _cache["ts"], _cache["agents"]
                if ts != last:
                    last = ts
                    self.wfile.write(f"data: {json.dumps({'agents': agents})}\n\n".encode())
                else:
                    self.wfile.write(b": ping\n\n")   # heartbeat
                self.wfile.flush()
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self):
        p = urlparse(self.path).path
        if not self._ok():
            return self._send(401, "application/json", '{"error":"unauthorized"}')
        # CSRF: state-changing POSTs require our custom header + a same-origin/no Origin check
        if self.headers.get("X-Requested-With") != "fetch":
            return self._send(403, "application/json", '{"error":"forbidden"}')
        origin = self.headers.get("Origin", "")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            return self._send(403, "application/json", '{"error":"bad origin"}')
        if p == "/api/action":
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            except Exception:
                return self._send(400, "application/json", '{"error":"bad json"}')
            action, aid, text = d.get("action", ""), d.get("id", ""), d.get("text", "")
            if action not in ("up", "down", "restart", "send") or not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad action"}')
            args = [action, aid] + ([text] if action == "send" and text else [])
            try:
                r = _fleet_cmd(*args, timeout=190)
                return self._send(200, "application/json", json.dumps({"ok": r.returncode == 0, "out": (r.stdout or r.stderr)[-400:]}))
            except Exception as e:
                return self._send(500, "application/json", json.dumps({"error": str(e)}))
        if p == "/api/config":   # apply a config change, then restart (P0 writable-config plane)
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            except Exception:
                return self._send(400, "application/json", '{"error":"bad json"}')
            aid = d.get("id", "")
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            # one of: preset | brain(+model) | mode(+interval) | updates{K:V}
            if d.get("preset"):
                args = ["preset", aid, str(d["preset"])]
            elif d.get("brain"):
                args = ["set-brain", aid, str(d["brain"])] + ([str(d["model"])] if d.get("model") else [])
            elif d.get("mode"):
                args = ["set-mode", aid, str(d["mode"])] + ([str(d["interval"])] if d.get("interval") else [])
            elif isinstance(d.get("updates"), dict) and d["updates"]:
                args = ["set-config", aid] + [f"{k}={v}" for k, v in d["updates"].items()]
            else:
                return self._send(400, "application/json", '{"error":"need preset|brain|mode|updates"}')
            try:
                r = _fleet_cmd(*args, timeout=190)
                return self._send(200, "application/json", json.dumps({"ok": r.returncode == 0, "out": (r.stdout or r.stderr)[-500:]}))
            except Exception as e:
                return self._send(500, "application/json", json.dumps({"error": str(e)}))
        if p == "/api/goal":   # P3 steering: write the autonomous-supervisor goal (no restart needed)
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            except Exception:
                return self._send(400, "application/json", '{"error":"bad json"}')
            aid = d.get("id", "")
            if not fleet._SAFE.match(aid or ""):
                return self._send(400, "application/json", '{"error":"bad id"}')
            with _lock:
                a = (_cache.get("agents") or {}).get(aid)
            home = a.get("home") if a else None
            if not home:
                return self._send(400, "application/json", '{"error":"agent has no home on this host"}')
            try:
                gf = pathlib.Path(home) / "state" / "phase-goal.txt"
                gf.parent.mkdir(parents=True, exist_ok=True)
                gf.write_text((d.get("text") or "").strip() + "\n")
                fleet._audit("set-goal", aid, (d.get("text") or "")[:80])
                return self._send(200, "application/json", json.dumps({"ok": True}))
            except Exception as e:
                return self._send(500, "application/json", json.dumps({"error": str(e)}))
        if p == "/api/create":   # enqueue a new-agent spec for the spawn watcher (P1 create-agent)
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0) or 0)) or b"{}")
            except Exception:
                return self._send(400, "application/json", '{"error":"bad json"}')
            name = (d.get("name") or "").strip()
            if not fleet._SAFE.match(name):
                return self._send(400, "application/json", '{"error":"name must be kebab-case [a-z0-9][a-z0-9_-]*"}')
            # whitelist spec fields the spawn pipeline understands (enclave new --spec)
            spec = {"name": name}
            for k in ("template", "brain", "model", "mission"):
                if d.get(k):
                    spec[k] = d[k]
            if d.get("interval_seconds"):
                try:
                    spec["interval_seconds"] = int(d["interval_seconds"])
                except (TypeError, ValueError):
                    return self._send(400, "application/json", '{"error":"interval_seconds must be an integer"}')
            # Secrets: (A) existing files chosen from the library + (B) new name/value files. Both are
            # STAGED as real env files next to the spec; the spawn watcher copies them into the new
            # agent's secrets/ and removes the staging (so values don't linger). Spec carries only names.
            import shutil
            SECNAME = re.compile(r"^[A-Za-z0-9._-]+\.env$")
            lib = os.environ.get("ENCLAVE_SECRETS_LIB", "")
            existing = [str(s).strip() for s in (d.get("secrets") or []) if str(s).strip()]
            for s in existing:
                if not SECNAME.match(s):
                    return self._send(400, "application/json", json.dumps({"error": f"bad secret name: {s}"}))
                if not (lib and os.path.isfile(os.path.join(lib, s))):
                    return self._send(400, "application/json", json.dumps({"error": f"secret not in library: {s}"}))
            new_files = {}
            for ns in (d.get("new_secrets") or []):
                nm = str((ns or {}).get("name", "")).strip()
                if nm and not nm.endswith(".env"):
                    nm += ".env"
                if not SECNAME.match(nm):
                    return self._send(400, "application/json", json.dumps({"error": f"bad new-secret filename: {nm}"}))
                new_files[nm] = str((ns or {}).get("content", ""))
            all_names = sorted(set(existing) | set(new_files))
            if all_names:
                spec["secrets"] = all_names
            qroot = pathlib.Path(os.environ.get("ENCLAVE_SPAWN_QUEUE",
                                 str(fleet.STACKS_ROOTS[0] / "_queue") if fleet.STACKS_ROOTS else "/tmp/enclave-queue"))
            incoming = qroot / "incoming"
            try:
                incoming.mkdir(parents=True, exist_ok=True)
                dest = incoming / f"{name}.json"
                if dest.exists():
                    return self._send(409, "application/json", json.dumps({"error": f"spec {name}.json already queued"}))
                if all_names:   # stage the real secret files (chmod 600) for the watcher to apply
                    staging = qroot / "secrets-staging" / name
                    staging.mkdir(parents=True, exist_ok=True)
                    try: os.chmod(staging, 0o700)
                    except OSError: pass
                    for s in existing:
                        shutil.copy2(os.path.join(lib, s), staging / s)
                        try: os.chmod(staging / s, 0o600)
                        except OSError: pass
                    for nm, content in new_files.items():
                        (staging / nm).write_text(content if content.endswith("\n") else content + "\n")
                        try: os.chmod(staging / nm, 0o600)
                        except OSError: pass
                dest.write_text(json.dumps(spec, indent=2))
                fleet._audit("create-queued", name, str(dest))
                watching = (qroot / "processed").exists() or (qroot / "failed").exists()
                note = "queued — spawn watcher will build + start it" if watching else \
                       f"queued at {dest} — NOTE: no spawn watcher detected on this queue (run `enclave fleet watch {qroot}`)"
                return self._send(200, "application/json", json.dumps({"ok": True, "queued": str(dest), "note": note}))
            except Exception as e:
                return self._send(500, "application/json", json.dumps({"error": str(e)}))
        return self._send(404, "application/json", '{"error":"not found"}')


def main():
    a = sys.argv[1:]
    host = a[a.index("--host") + 1] if "--host" in a else "127.0.0.1"
    port = int(a[a.index("--port") + 1] if "--port" in a else os.environ.get("CONSOLE_PORT", "8700"))
    if host not in ("127.0.0.1", "localhost"):
        sys.exit("console binds loopback only (127.0.0.1) — reach it remotely via an SSH tunnel.")
    threading.Thread(target=_snapshot_loop, daemon=True).start()
    threading.Thread(target=_cost_loop, daemon=True).start()   # second, slower loop for cost/monitoring
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    auth = "token-gated" if TOKEN else "OPEN (loopback; set CONSOLE_TOKEN to gate)"
    print(f"[console] Enclave fleet console on http://{host}:{port}/  ({auth})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
