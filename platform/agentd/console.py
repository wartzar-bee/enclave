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
import os, sys, json, time, threading, socket, subprocess, pathlib, hmac, secrets as _secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fleet   # the control-plane helper (snapshot + lifecycle); read-only here, mutations via subprocess

TOKEN = os.environ.get("CONSOLE_TOKEN", "")
PROBE_SECS = 4.0
_cache = {"agents": {}, "ts": 0}
_lock = threading.Lock()
_sessions = {}   # token -> expiry (process-local; re-auth is one POST)


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
<title>Enclave Fleet</title><style>
/* palette matches web_chat exactly so the console frame + the embedded chat are ONE UI */
:root{--bg:#262624;--card:#30302e;--bd:#3f3f3b;--tx:#ececec;--mut:#9a988f;--accent:#d97757;--hover:#3a3a37;--sel:#403f3b;--ok:#3fbf6f;--idle:#c9a23f;--down:#c2603f}
body.light{--bg:#faf9f5;--card:#ffffff;--bd:#e7e3d8;--tx:#28261f;--mut:#73726c;--accent:#d97757;--hover:#f3f1ea;--sel:#ece7dc}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--tx);height:100vh;display:flex}
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
</style></head><body>
<aside id="rail"><h1><button class="railx" onclick="toggleRail()" title="Collapse panel">−</button><span>ENCLAVE FLEET</span><span id="count" style="margin-left:auto"></span></h1>
<input id="search" placeholder="filter agents…" autocomplete="off"><div id="list"></div></aside>
<main id="main">
  <div id="bar"><button id="railtoggle" title="Show agents" onclick="toggleRail()">☰</button>
    <span class="t" id="bt">—</span><span class="m" id="bm"></span><span style="flex:1"></span>
    <button class="btn" onclick="act('restart')">Restart</button>
    <button class="btn danger" onclick="act('down')">Stop</button>
    <button class="btn" onclick="act('up')">Start</button>
    <button class="btn" onclick="openChat()">↗ Chat tab</button>
    <button class="btn" id="themebtn" title="Toggle light/dark" onclick="toggleTheme()">🌙</button></div>
  <div class="tabs"><span class="tab sel" data-t="chat" onclick="tab('chat')">Chat</span>
    <span class="tab" data-t="status" onclick="tab('status')">Status</span>
    <span class="tab" data-t="logs" onclick="tab('logs')">Logs</span></div>
  <div id="pane"><div class="empty">Select an agent from the rail.</div></div>
  <div id="dbox"><input id="dtext" placeholder="Send a directive to this agent (wakes its tick)…"><button class="btn" onclick="sendD()">Send</button></div>
</main>
<script>
const TOK=new URLSearchParams(location.search).get("token")||"";
const qs=p=>TOK?(p+(p.includes("?")?"&":"?")+"token="+encodeURIComponent(TOK)):p;
let agents={},sel=null,curtab="chat";
function dotcls(a){return a.tick==="working"?"working":a.tick==="down"?"down":"idle";}
function render(){
  const f=(search.value||"").toLowerCase();
  const list=Object.values(agents).filter(a=>!f||a.id.toLowerCase().includes(f)||(a.model||"").toLowerCase().includes(f));
  count.textContent=list.length;
  const bym={};list.forEach(a=>{(bym[a.manager||""]=bym[a.manager||""]||[]).push(a);});
  let h="";const grp=(title,arr)=>{if(title)h+=`<div class="grp">▸ ${esc(title)}</div>`;
    arr.sort((x,y)=>x.id<y.id?-1:1).forEach(a=>{h+=`<div class="row${sel===a.id?' sel':''}" onclick="pick('${a.id}')">
      <span class="dot ${dotcls(a)}"></span><div><div class="rid">${esc(a.id)}</div>
      <div class="rmeta">${esc(a.brain)} · ${esc(a.model)} · :${a.port} · work ${a.work_open}</div></div></div>`;});};
  Object.keys(bym).filter(m=>m).forEach(m=>grp(m+" (manager)",bym[m]));
  if(bym[""])grp(Object.keys(bym).length>1?"standalone":"",bym[""]);
  list_el().innerHTML=h;
}
function list_el(){return document.getElementById("list");}
function esc(s){return (s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
function pick(id){sel=id;render();const a=agents[id];bt.textContent=id;bm.textContent=a?`${a.status} · :${a.port}`:"";tab(curtab);}
function openChat(){if(sel)window.open("http://127.0.0.1:"+agents[sel].port+"/","_blank");}
function tab(t){curtab=t;document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("sel",e.dataset.t===t));
  const p=document.getElementById("pane");if(!sel){p.innerHTML='<div class="empty">Select an agent.</div>';return;}
  const a=agents[sel];
  if(t==="chat"){p.innerHTML=`<iframe src="http://127.0.0.1:${a.port}/?theme=${theme()}" allow="microphone; clipboard-write"></iframe>`;}
  else if(t==="status"){p.innerHTML=`<div id="status">${esc(JSON.stringify({id:a.id,up:a.up,status:a.status,brain:a.brain,model:a.model,port:a.port,manager:a.manager,tick:a.tick,reachable:a.reachable,work_open:a.work_open,headline:a.headline,home:a.home},null,2))}</div>`;}
  else if(t==="logs"){p.innerHTML='<div id="logs">loading…</div>';fetch(qs(`/api/logs?id=${encodeURIComponent(sel)}`)).then(r=>r.text()).then(x=>{const e=document.getElementById("logs");if(e)e.textContent=x;});}
}
async function act(action){if(!sel)return;if(action==="down"&&!confirm("Stop "+sel+"?"))return;
  await post("/api/action",{action,id:sel});setTimeout(load,800);}
async function sendD(){if(!sel)return;const t=dtext.value.trim();if(!t)return;dtext.value="";
  await post("/api/action",{action:"send",id:sel,text:t});}
async function post(path,body){try{await fetch(qs(path),{method:"POST",headers:{"Content-Type":"application/json","X-Requested-With":"fetch"},body:JSON.stringify(body)});}catch(e){}}
async function load(){try{const j=await(await fetch(qs("/api/fleet"))).json();agents=j.agents||{};render();if(sel&&agents[sel]){bm.textContent=agents[sel].status+" · :"+agents[sel].port;}}catch(e){}}
function theme(){return document.body.classList.contains("light")?"light":"dark";}
function applyThemeBtn(){const b=document.getElementById("themebtn");if(b)b.textContent=theme()==="light"?"🌙":"☀";}
function toggleTheme(){document.body.classList.toggle("light");try{localStorage.setItem("console_theme",theme());}catch(e){}applyThemeBtn();
  const f=document.querySelector("#pane iframe");if(f){try{const u=new URL(f.src);u.searchParams.set("theme",theme());f.src=u.toString();}catch(e){}}}
try{if(localStorage.getItem("console_theme")==="light")document.body.classList.add("light");}catch(e){}
applyThemeBtn();
function toggleRail(){const c=document.body.classList.toggle("railcollapsed");try{localStorage.setItem("rail_collapsed",c?"1":"");}catch(e){}}
try{if(localStorage.getItem("rail_collapsed"))document.body.classList.add("railcollapsed");}catch(e){}
search.addEventListener("input",render);
load();
try{const es=new EventSource(qs("/api/stream"));es.onmessage=e=>{try{agents=JSON.parse(e.data).agents||agents;render();}catch(_){}};}catch(e){setInterval(load,5000);}
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

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/":
            return self._send(200, "text/html; charset=utf-8", PAGE)
        if not self._ok():
            return self._send(401, "application/json", '{"error":"unauthorized"}')
        if p == "/api/fleet":
            with _lock:
                return self._send(200, "application/json", json.dumps({"agents": _cache["agents"], "ts": _cache["ts"]}))
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
    srv = ThreadingHTTPServer((host, port), H)
    srv.daemon_threads = True
    auth = "token-gated" if TOKEN else "OPEN (loopback; set CONSOLE_TOKEN to gate)"
    print(f"[console] Enclave fleet console on http://{host}:{port}/  ({auth})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
