#!/usr/bin/env python3
"""
console.py — Enclave fleet console (P2): one web panel to see + steer 20-100 agents.

Two panes (NOT a table): a left RAIL of agents grouped by manager (the studio-agent -> sub-agents
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
        if a.get("up") and a.get("tick") == "down":
            al.append({"level": "warn", "msg": f"{aid}: container up but no recent tick"})
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
            "status": a.get("tick") or ("idle" if a else "down"),
            "model": (a.get("model") or "").replace("claude-", ""),
            "cost": round((wtd_agents.get(aid, {}) or {}).get("cost_usd", 0), 2),
            "work_open": a.get("work_open", 0),
        })
        have.add(aid)
    for n in list(nodes):                      # surface a manager that isn't itself a discovered agent
        if n["manager"] and n["manager"] not in have:
            nodes.append({"id": n["manager"], "manager": "", "status": "idle", "model": "", "cost": 0, "work_open": 0})
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
:root{--bg:#262624;--card:#30302e;--bd:#3f3f3b;--tx:#ececec;--mut:#9a988f;--accent:#d97757;--hover:#3a3a37;--sel:#403f3b;--ok:#3fbf6f;--idle:#c9a23f;--down:#c2603f}
body.light{--bg:#faf9f5;--card:#ffffff;--bd:#e7e3d8;--tx:#28261f;--mut:#73726c;--accent:#d97757;--hover:#f3f1ea;--sel:#ece7dc}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--tx);height:100vh;display:flex;flex-direction:column}
#nav{display:flex;align-items:center;gap:6px;padding:9px 14px;background:var(--card);border-bottom:1px solid var(--bd);flex:0 0 auto}
#nav .brand{font-size:12.5px;font-weight:700;letter-spacing:.05em;color:var(--mut);margin-right:8px}
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
.dot{width:9px;height:9px;border-radius:50%;flex:0 0 9px}.working{background:var(--ok)}.idle{background:var(--idle)}.down{background:var(--down)}
.rid{font-weight:600}.rmeta{font-size:11.5px;color:var(--mut)}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#bar{padding:11px 16px;border-bottom:1px solid var(--bd);display:flex;align-items:center;gap:10px}
#bar .t{font-weight:700;font-size:15px}#bar .m{color:var(--mut);font-size:12.5px}
.btn{background:var(--hover);border:1px solid var(--bd);color:var(--tx);border-radius:8px;padding:6px 11px;cursor:pointer;font:inherit;font-size:12.5px}
.btn:hover{background:var(--sel)}.btn.danger:hover{background:#3a2420;border-color:#c2603f}
.tabs{display:flex;gap:4px;padding:8px 14px 0}.tab{padding:6px 12px;border-radius:8px 8px 0 0;cursor:pointer;color:var(--mut)}.tab.sel{background:var(--card);color:var(--tx)}
#pane{flex:1;background:var(--card);margin:0 0 0 0;overflow:auto;min-height:0;display:flex;flex-direction:column}
iframe{flex:1;border:0;width:100%;background:var(--bg)}
#status,#logs{padding:16px;white-space:pre-wrap;font:12.5px ui-monospace,Menlo,monospace;color:var(--tx);overflow:auto}
#dbox{display:flex;gap:8px;padding:10px 14px;border-top:1px solid var(--bd)}
#dtext{flex:1;background:var(--bg);border:1px solid var(--bd);border-radius:9px;color:var(--tx);padding:8px 11px;font:inherit}
.empty{margin:auto;color:var(--mut)}
/* ---- Overview view (compact, width-capped) ---- */
#view-overview{display:none;overflow:auto;padding:14px}
.ovwrap{max-width:880px;margin:0 auto}
.toprow{display:flex;gap:10px;align-items:stretch;flex-wrap:wrap;margin-bottom:12px}
.gaugewrap{display:flex;gap:8px;align-items:center}
.creditschip{align-self:center;font-size:9.5px;color:var(--mut);border:1px solid var(--bd);border-radius:7px;padding:3px 7px;white-space:nowrap}
.gaugecard{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:8px 10px;display:flex;flex-direction:column;align-items:center;min-width:96px}
.gauge{width:72px;height:72px}.gv{font-size:21px;font-weight:800}
.glabel{font-size:10px;color:var(--tx);font-weight:600;margin-top:3px;text-align:center}
.gsub{font-size:9.5px;color:var(--mut);margin-top:1px;text-align:center}
.ovgrid{flex:1;min-width:240px;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
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
  <span id="winwrap"><select id="win" onchange="renderOverview()"><option value="today">Today</option><option value="wtd" selected>Week-to-date</option><option value="7d">Last 7 days</option></select>
    <button class="btn" onclick="exportCsv()" title="Download usage as CSV">⬇ CSV</button></span>
  <span class="stale" id="stale"></span>
  <button class="btn" id="themebtn" title="Toggle light/dark" onclick="toggleTheme()">🌙</button>
</nav>
<div id="alertbar"></div>
<div id="body">
<section id="view-overview" class="view"><div class="ovwrap">
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
    <div class="li"><span class="sw" style="background:var(--down)"></span>down</div>
    <div class="li"><span class="sw" style="background:#c9a23f;border-radius:2px"></span>manager link · <span class="sw" style="background:#56b6c2;border-radius:2px"></span>peer comms</div>
  </div>
</section>
</div>
<script>
const TOK=new URLSearchParams(location.search).get("token")||"";
const qs=p=>TOK?(p+(p.includes("?")?"&":"?")+"token="+encodeURIComponent(TOK)):p;
const PAL=["#d97757","#79c0ff","#3fbf6f","#c9a23f","#b58cf0","#e06c9f","#56b6c2","#d0a35c","#8fbf6f","#f08a8a"];
let agents={},sel=null,curtab="chat",curview="overview",ov={},sortKey="claude",sortDir=-1;
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
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
  document.getElementById("winwrap").style.display=v==="overview"?"":"none";
  try{localStorage.setItem("console_view",v);}catch(e){}
  if(v==="overview"){loadOverview();}else if(v==="graph"){loadGraph();}else{render();}
}
/* ---------- Agents view ---------- */
function dotcls(a){return a.tick==="working"?"working":a.tick==="down"?"down":"idle";}
function render(){
  const s=document.getElementById("search");const f=(s.value||"").toLowerCase();
  const list=Object.values(agents).filter(a=>!f||a.id.toLowerCase().includes(f)||(a.model||"").toLowerCase().includes(f));
  document.getElementById("count").textContent=list.length;
  const bym={};list.forEach(a=>{(bym[a.manager||""]=bym[a.manager||""]||[]).push(a);});
  let h="";const grp=(title,arr)=>{if(title)h+=`<div class="grp">▸ ${esc(title)}</div>`;
    arr.sort((x,y)=>x.id<y.id?-1:1).forEach(a=>{h+=`<div class="row${sel===a.id?' sel':''}" onclick="pick('${a.id}')">
      <span class="dot ${dotcls(a)}"></span><div><div class="rid">${esc(a.id)}</div>
      <div class="rmeta">${esc(a.brain)} · ${esc(a.model)} · :${a.port} · work ${a.work_open}</div></div></div>`;});};
  Object.keys(bym).filter(m=>m).forEach(m=>grp(m+" (manager)",bym[m]));
  if(bym[""])grp(Object.keys(bym).length>1?"standalone":"",bym[""]);
  document.getElementById("list").innerHTML=h;
}
function pick(id){sel=id;if(curview!=="agents")view("agents");render();const a=agents[id];bt.textContent=id;bm.textContent=a?`${a.status} · :${a.port}`:"";tab(curtab);}
function openChat(){if(sel)window.open("http://127.0.0.1:"+agents[sel].port+"/","_blank");}
function tab(t){curtab=t;document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("sel",e.dataset.t===t));
  const p=document.getElementById("pane");if(!sel){p.innerHTML='<div class="empty">Select an agent.</div>';return;}
  const a=agents[sel];
  if(t==="chat"){p.innerHTML=`<iframe src="http://127.0.0.1:${a.port}/?theme=${theme()}" allow="microphone; clipboard-write"></iframe>`;}
  else if(t==="status"){
    const lr=(ov.last||{})[sel]||{};const c=(((ov.usage||{}).wtd||{}).agents||{})[sel]||{};
    p.innerHTML=`<div style="padding:16px;overflow:auto">
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px">
        <div class="card"><div class="k">wtd spend</div><div class="v">${usd(c.cost_usd)}</div><div class="s">${num(c.tokens)} tokens · ${c.ticks||0} ticks · ${c.cost_share_pct||0}% of fleet</div></div>
        <div class="card"><div class="k">last tick</div><div class="v">${lr.cost_usd!=null?usd(lr.cost_usd):"—"}</div><div class="s">${esc(lr.reason||"")}${lr.model?(" · "+lr.model.replace("claude-","")):""}${lr.rc!=null&&lr.rc!==0?" · rc "+lr.rc:""}</div></div>
      </div>
      <div class="chartcard" style="max-width:580px;margin-bottom:12px"><h3>This agent — cost over time (7d, $)</h3><canvas id="miniChart"></canvas></div>
      <div id="status">${esc(JSON.stringify({id:a.id,up:a.up,status:a.status,brain:a.brain,model:a.model,port:a.port,manager:a.manager,tick:a.tick,reachable:a.reachable,work_open:a.work_open,headline:a.headline,home:a.home},null,2))}</div></div>`;
    ensureOv().then(()=>drawMini(sel));
  }
  else if(t==="logs"){p.innerHTML='<div id="logs">loading…</div>';fetch(qs(`/api/logs?id=${encodeURIComponent(sel)}`)).then(r=>r.text()).then(x=>{const e=document.getElementById("logs");if(e)e.textContent=x;});}
}
async function act(action){if(!sel)return;if(action==="down"&&!confirm("Stop "+sel+"?"))return;
  await post("/api/action",{action,id:sel});setTimeout(load,800);}
async function sendD(){if(!sel)return;const t=dtext.value.trim();if(!t)return;dtext.value="";
  await post("/api/action",{action:"send",id:sel,text:t});}
async function post(path,body){try{await fetch(qs(path),{method:"POST",headers:{"Content-Type":"application/json","X-Requested-With":"fetch"},body:JSON.stringify(body)});}catch(e){}}
async function load(){try{const j=await(await fetch(qs("/api/fleet"))).json();agents=j.agents||{};renderAlerts(j.alerts||[]);if(curview==="agents"){render();if(sel&&agents[sel]){bm.textContent=agents[sel].status+" · :"+agents[sel].port;}}else{renderOverview();}}catch(e){}}
/* ---------- alerts ---------- */
function renderAlerts(al){const b=document.getElementById("alertbar");if(!al||!al.length){b.innerHTML="";return;}
  b.innerHTML=al.map(a=>`<div class="alert ${a.level==="crit"?"crit":"warn"}">${a.level==="crit"?"⛔":"⚠"} ${esc(a.msg)}</div>`).join("");}
/* ---------- Overview view ---------- */
async function loadOverview(){try{ov=await(await fetch(qs("/api/overview"))).json();}catch(e){}renderOverview();}
function gauge(w,label){const pct=w&&w.pct!=null?w.pct:null;const warn=label.indexOf("5h")>=0?70:85;
  const col=pct==null?"var(--mut)":pct>=90?"var(--down)":pct>=warn?"var(--idle)":"var(--ok)";
  const r=42,c=2*Math.PI*r,off=pct==null?c:c*(1-Math.min(pct,100)/100);
  let eta="resets —";if(w&&w.reset_epoch){const s=Math.max(0,w.reset_epoch-Date.now()/1000);eta="resets "+Math.floor(s/3600)+"h"+String(Math.floor(s%3600/60)).padStart(2,"0")+"m";}
  return `<div class="gaugecard" title="Claude subscription ${label} usage — defers at 90%"><svg class="gauge" viewBox="0 0 100 100">
    <circle cx=50 cy=50 r=${r} fill=none stroke="var(--bd)" stroke-width=10/>
    <circle cx=50 cy=50 r=${r} fill=none stroke="${col}" stroke-width=10 stroke-linecap=round stroke-dasharray="${c}" stroke-dashoffset="${off}" transform="rotate(-90 50 50)"/>
    <text x=50 y=56 text-anchor=middle class=gv fill="${col}">${pct==null?"n/a":pct+"%"}</text></svg>
    <div class="glabel">${label}</div><div class="gsub">${eta}</div></div>`;
}
function renderOverview(){
  if(curview!=="overview")return;
  const win=document.getElementById("win").value;
  const u=((ov.usage||{})[win])||{fleet:{},agents:{}};
  const F=u.fleet||{};const cap=ov.cap||{};
  const ex=((ov.external||{})[win])||{fleet:{usd:0,by_model:{}},agents:{}};
  const exF=ex.fleet||{usd:0,by_model:{}};
  document.getElementById("stale").textContent=ov.ts?("updated "+Math.max(0,Math.round(Date.now()/1000-ov.ts))+"s ago"):"";
  /* Claude subscription gauges (flat-rate; usage counts against the CAP, not the wallet) */
  let g=gauge(cap.five_hour,"5h session")+gauge(cap.seven_day,"7d weekly");
  g+=`<span class="creditschip" title="Claude is a flat subscription: usage counts against the 5h/7d CAP, not your wallet. Pay-as-you-go credits are ${cap.credits_enabled?"ON":"OFF"}. Real money out the door is the External LLM spend →">Claude subscription${cap.credits_enabled===false?" · credits OFF":""}</span>`;
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
  const cols=[["id","Agent"],["brain","Brain"],["status","·"],["claude","Claude $"],["external","Ext $"],["tokens","Tokens"],["last","Last tick"]];
  const ids=new Set([...Object.keys(agents||{}),...Object.keys(ag||{}),...Object.keys(agext||{})]);
  const rows=[...ids].map(id=>{
    const live=agents[id]||{},a=ag[id]||{},e=agext[id]||{},lr=(ov.last||{})[id]||{};
    return {id,brain:live.brain||"?",tick:live.tick||(live.up?"idle":"down"),
      claude:a.cost_usd||0,external:e.usd||0,tokens:a.tokens||0,
      last:lr.cost_usd!=null?usd(lr.cost_usd)+" "+(lr.reason||""):(lr.reason||"—"),lastrc:lr.rc};
  });
  rows.sort((x,y)=>{const k=sortKey;const xv=k==="total"?x.claude+x.external:x[k],yv=k==="total"?y.claude+y.external:y[k];return (xv>yv?1:xv<yv?-1:0)*sortDir;});
  document.getElementById("costhead").innerHTML="<tr>"+cols.map(c=>`<th onclick="sortBy('${c[0]}')">${esc(c[1])}${sortKey===c[0]?(sortDir<0?" ▾":" ▴"):""}</th>`).join("")+"</tr>";
  document.getElementById("costbody").innerHTML=rows.map(r=>{
    const dot=r.tick==="working"?"working":r.tick==="down"?"down":"idle";
    return `<tr onclick="pick('${r.id}')"><td><b>${esc(r.id)}</b></td>
      <td class="mono">${esc(r.brain)}</td>
      <td><span class="dot ${dot}" style="display:inline-block"></span></td>
      <td><b>${r.claude?usd(r.claude):"—"}</b></td>
      <td style="${r.external>0?"color:var(--accent);font-weight:600":""}">${r.external>0?usd(r.external):"—"}</td>
      <td>${r.tokens?num(r.tokens):"—"}</td>
      <td style="${r.lastrc!=null&&r.lastrc!==0?"color:var(--down)":""}">${esc(r.last)}</td></tr>`;
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
let G=null;
async function loadGraph(){let d;try{d=await(await fetch(qs("/api/graph"))).json();}catch(e){return;}renderGraph(d);}
function statusCol(s){return s==="working"?cssv("--ok"):s==="down"?cssv("--down"):cssv("--idle");}
function renderGraph(d){
  if(typeof ForceGraph==="undefined"){document.getElementById("graphbox").innerHTML='<div class="empty" style="padding:30px">graph library unavailable</div>';return;}
  const box=document.getElementById("graphbox");
  const REL=4,rad=n=>REL*Math.sqrt(1+Math.sqrt(n.cost||0));   // compressed: $ → radius (sqrt), cap stays small
  if(!G){
    G=ForceGraph()(box).nodeId("id").backgroundColor("rgba(0,0,0,0)")
      .nodeLabel(n=>`${n.id}${n.model?" · "+n.model:""} · $${(n.cost||0).toFixed(2)} wtd · work ${n.work_open||0}`)
      .nodeColor(n=>statusCol(n.status)).nodeRelSize(REL).nodeVal(n=>1+Math.sqrt(n.cost||0))
      .linkColor(l=>l.kind==="peer"?"#56b6c2":"#c9a23f").linkWidth(l=>Math.min(4,0.6+(l.count||1)*0.3))
      .linkDirectionalParticles(l=>l.kind==="peer"?2:0).linkDirectionalParticleWidth(2)
      .nodeCanvasObjectMode(()=>"after")
      .nodeCanvasObject((n,ctx,scale)=>{
        const r=rad(n);
        if(n.work_open>0){ctx.beginPath();ctx.arc(n.x,n.y,r+2.5,0,2*Math.PI);ctx.strokeStyle=cssv("--accent");ctx.lineWidth=1.4/scale;ctx.stroke();}
        const fs=10/scale;ctx.font=`${fs}px -apple-system,system-ui,sans-serif`;ctx.fillStyle=cssv("--tx");ctx.textAlign="center";ctx.textBaseline="top";
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
try{view(localStorage.getItem("console_view")||"overview");}catch(e){view("overview");}
load();
setInterval(()=>{if(curview==="overview")loadOverview();},15000);
try{const es=new EventSource(qs("/api/stream"));es.onmessage=e=>{try{const j=JSON.parse(e.data);agents=j.agents||agents;if(curview==="agents")render();}catch(_){}};}catch(e){setInterval(load,5000);}
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
            r = _fleet_cmd("logs", aid, "--tail", "150", timeout=30)
            return self._send(200, "text/plain; charset=utf-8", (r.stdout or "") + (r.stderr or ""))
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
