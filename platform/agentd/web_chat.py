#!/usr/bin/env python3
"""
web_chat.py — a claude.ai-style web chat interface for an Enclave agent.

Serves a single-page chat UI at http://<host>:<port>/ and bridges to the agent
via the same file convention the Telegram relay uses:

  Inbound:  browser POST /api/send  → append to agent inbox.md (triggers tick)
  Outbound: agent writes state/chat-reply.md → browser polls GET /api/poll

Features (all backend = pure stdlib, no deps; voice runs in the browser):
  • Image attachments — POST /api/upload saves to uploads/, the agent reads them by path.
  • Voice input  — browser Web Speech API (SpeechRecognition) dictates into the box.
  • Speak replies — browser speechSynthesis reads the agent's answer aloud.
  • Model switch — POST /api/model writes state/model.override; runtime.sh honors it next tick.

Conversation history persists to state/chat-history.jsonl so a reload keeps the thread.

Usage:  python3 web_chat.py
Env:
  AGENT_DIR        path to mounted agent data (default /agent)
  WEB_CHAT_PORT    listen port (default 8888)
  WEB_CHAT_TOKEN   if set, require ?token=... (or X-Chat-Token header)
  AGENT_NAME       display name in the UI (default "Agent")
"""
import os, sys, json, time, re, base64, pathlib, threading, mimetypes, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

AGENT_DIR = pathlib.Path(os.environ.get("AGENT_DIR", "/agent"))
PORT = int(os.environ.get("WEB_CHAT_PORT", "8888"))
TOKEN = os.environ.get("WEB_CHAT_TOKEN", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "Agent")

# Optional server-side voice (privacy opt-in): point these at a compatible service and the UI uses it
# instead of the browser's Web Speech API. Contracts (see docs):
#   TRANSCRIBE_URL: POST {"audio_base64","mime"} -> {"text"}
#   TTS_URL:        POST {"text","voice"}        -> audio bytes (Content-Type audio/*)
TRANSCRIBE_URL = os.environ.get("TRANSCRIBE_URL", "").strip()
TTS_URL = os.environ.get("TTS_URL", "").strip()
TTS_VOICE = os.environ.get("TTS_VOICE", "").strip()

CHAT_INBOX = AGENT_DIR / "state" / "chat-inbox.jsonl"   # the real-time chat plane (chat_responder reads it)
REPLY_FILE = AGENT_DIR / "state" / "chat-reply.md"
HISTORY = AGENT_DIR / "state" / "chat-history.jsonl"
UPLOADS = AGENT_DIR / "uploads"
OVERRIDE = AGENT_DIR / "state" / "model.override"

MAX_UPLOAD = 16 * 1024 * 1024          # 16 MB per image (pre-base64)
ALLOWED_IMG = {"image/png", "image/jpeg", "image/gif", "image/webp"}

# Brain → selectable models (the allowlist; only these ids are ever written to the override).
MODELS = {
    "claude": [
        {"id": "claude-opus-4-8", "label": "Claude Opus 4.8"},
        {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5"},
    ],
    "api": [
        {"id": "deepseek/deepseek-chat", "label": "DeepSeek V3"},
        {"id": "anthropic/claude-sonnet-4", "label": "Claude Sonnet 4"},
        {"id": "google/gemini-2.0-flash-001", "label": "Gemini 2.0 Flash"},
        {"id": "openai/gpt-4o", "label": "GPT-4o"},
        {"id": "meta-llama/llama-3.3-70b-instruct", "label": "Llama 3.3 70B"},
    ],
    "local": [
        {"id": "qwen2.5:7b", "label": "Qwen2.5 7B"},
        {"id": "llama3.1:8b", "label": "Llama 3.1 8B"},
    ],
}

_lock = threading.Lock()
_last_reply_mtime = [None]
_upload_n = [0]


# ── history / agent bridge ───────────────────────────────────────────────────
def _append_history(role, text, images=None):
    try:
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "role": role, "text": text}
        if images:
            rec["images"] = images
        with HISTORY.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _read_history():
    try:
        return [json.loads(l) for l in HISTORY.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def deliver_to_agent(text, images=None):
    # Write to the real-time chat plane (chat_responder answers it concurrently with the work tick).
    rec = {"ts": time.time(), "text": text, "images": images or []}
    CHAT_INBOX.parent.mkdir(parents=True, exist_ok=True)
    with CHAT_INBOX.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    _append_history("user", text, images)


def check_new_reply():
    """Return new agent reply text if chat-reply.md changed, else None."""
    try:
        mtime = REPLY_FILE.stat().st_mtime
    except OSError:
        return None
    if mtime == _last_reply_mtime[0]:
        return None
    _last_reply_mtime[0] = mtime
    try:
        content = REPLY_FILE.read_text().strip()
    except OSError:
        return None
    if not content:
        return None
    _append_history("agent", content)
    try:
        REPLY_FILE.write_text("")
    except OSError:
        pass
    return content


# ── model config ─────────────────────────────────────────────────────────────
def agent_config():
    brain, model = "claude", ""
    envf = AGENT_DIR / "agent.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith("BRAIN="):
                brain = line.split("=", 1)[1].strip() or brain
            elif line.startswith("BRAIN_MODEL="):
                model = line.split("=", 1)[1].strip()
            elif line.startswith("MODEL=") and not model:
                model = line.split("=", 1)[1].strip()
    if OVERRIDE.exists():
        try:
            o = OVERRIDE.read_text().strip().splitlines()
            if o and o[0].strip():
                model = o[0].strip()
        except OSError:
            pass
    models = list(MODELS.get(brain, []))
    if model and not any(m["id"] == model for m in models):
        models.insert(0, {"id": model, "label": model})
    if not model and models:
        model = models[0]["id"]
    return {"agent": AGENT_NAME, "brain": brain, "model": model, "models": models,
            "stt_backend": bool(TRANSCRIBE_URL), "tts_backend": bool(TTS_URL)}


def set_model(model):
    cfg = agent_config()
    if not any(m["id"] == model for m in cfg["models"]):
        return False
    OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE.write_text(model + "\n")
    return True


# ── image upload ─────────────────────────────────────────────────────────────
def save_upload(name, data_url):
    m = re.match(r"^data:([^;,]+);base64,(.*)$", data_url, re.DOTALL)
    if not m:
        return None, "bad data url"
    mime, b64 = m.group(1).lower(), m.group(2)
    if mime not in ALLOWED_IMG:
        return None, "unsupported type"
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return None, "bad base64"
    if not raw or len(raw) > MAX_UPLOAD:
        return None, "too large"
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}[mime]
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or "image"))[-48:]
    if not base or base.startswith("."):
        base = "image" + ext
    _upload_n[0] += 1
    fname = f"{int(time.time())}-{_upload_n[0]}-{base}"
    if not fname.lower().endswith(ext):
        fname += ext
    UPLOADS.mkdir(parents=True, exist_ok=True)
    (UPLOADS / fname).write_bytes(raw)
    return f"uploads/{fname}", None


# ── optional server-side voice (proxy to a configured service) ────────────────
def proxy_transcribe(audio_b64, mime):
    body = json.dumps({"audio_base64": audio_b64, "mime": mime}).encode()
    req = urllib.request.Request(TRANSCRIBE_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

def proxy_tts(text, voice):
    body = json.dumps({"text": text, "voice": voice or TTS_VOICE}).encode()
    req = urllib.request.Request(TTS_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), (r.headers.get("Content-Type") or "audio/mpeg")


SPARK = ('<svg class="spark" viewBox="0 0 100 100" aria-hidden="true">'
         '<g stroke="#d97757" stroke-width="9" stroke-linecap="round">'
         '<line x1="50" y1="13" x2="50" y2="87"/><line x1="13" y1="50" x2="87" y2="50"/>'
         '<line x1="23.8" y1="23.8" x2="76.2" y2="76.2"/><line x1="23.8" y1="76.2" x2="76.2" y2="23.8"/>'
         '<line x1="18.3" y1="36.5" x2="81.7" y2="63.5"/><line x1="36.5" y1="18.3" x2="63.5" y2="81.7"/>'
         '<line x1="18.3" y1="63.5" x2="81.7" y2="36.5"/><line x1="36.5" y1="81.7" x2="63.5" y2="18.3"/>'
         '</g></svg>')

PAGE = ("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__NAME__ — Enclave</title>
<style>
  :root {
    --bg:#faf9f5; --card:#ffffff; --text:#28261f; --muted:#73726c;
    --accent:#d97757; --accent-hover:#c2603f; --user:#f0eee6; --border:#e7e3d8;
    --code:#f3f1ea; --menu:#ffffff; --hover:#f3f1ea;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#262624; --card:#30302e; --text:#ececec; --muted:#9a988f;
            --user:#3a3a37; --border:#3f3f3b; --code:#1f1f1d; --menu:#30302e; --hover:#3a3a37; }
  }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--text);
         font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         display:flex; flex-direction:column; -webkit-font-smoothing:antialiased; }

  /* top bar */
  header { display:flex; align-items:center; justify-content:space-between;
           padding:11px 18px; border-bottom:1px solid var(--border); }
  header .brand { display:flex; align-items:center; gap:8px; font-weight:600; font-size:14px; }
  header .brand .spark { width:18px; height:18px; }
  .iconbtn { width:34px; height:34px; border:none; background:transparent; border-radius:9px;
             color:var(--muted); cursor:pointer; display:flex; align-items:center; justify-content:center; }
  .iconbtn:hover { background:var(--hover); color:var(--text); }
  .iconbtn.on { color:var(--accent); }
  .iconbtn svg { width:19px; height:19px; }

  #app { flex:1; display:flex; flex-direction:column; width:100%; max-width:760px;
         margin:0 auto; padding:0 20px; min-height:0; }
  body.empty #app { justify-content:center; }

  #main { flex:1; overflow-y:auto; min-height:0; display:flex; flex-direction:column; }
  body.empty #main { flex:0 0 auto; overflow:visible; }
  #hero { display:none; flex-direction:column; align-items:center; text-align:center; padding-bottom:26px; }
  body.empty #hero { display:flex; }
  body.empty #log { display:none; }
  #hero .spark { width:42px; height:42px; margin-bottom:18px; }
  #hero h1 { font-family:ui-serif,Georgia,"Times New Roman",serif; font-weight:400;
             font-size:34px; letter-spacing:-0.4px; margin:0; }

  #log { display:flex; flex-direction:column; gap:22px; padding:26px 0 12px; }
  .msg { display:flex; }
  .msg.user { justify-content:flex-end; }
  .user .col { display:flex; flex-direction:column; align-items:flex-end; gap:6px; max-width:80%; }
  .user .bubble { background:var(--user); border-radius:16px 16px 4px 16px; padding:11px 16px; }
  .agent { gap:13px; align-items:flex-start; }
  .agent .spark { width:24px; height:24px; flex:0 0 24px; margin-top:2px; }
  .agent .col { max-width:calc(100% - 40px); display:flex; flex-direction:column; }
  .bubble { line-height:1.62; font-size:15.5px; white-space:pre-wrap; overflow-wrap:anywhere; }
  .bubble strong { font-weight:650; }
  .bubble code { background:var(--code); padding:1.5px 5px; border-radius:5px;
                 font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.88em; }
  .bubble pre { background:var(--code); border:1px solid var(--border); border-radius:10px;
                padding:13px 15px; overflow-x:auto; margin:10px 0; }
  .bubble pre code { background:none; padding:0; font-size:0.86em; line-height:1.5; }
  .imgs { display:flex; flex-wrap:wrap; gap:8px; }
  .imgs img { max-width:220px; max-height:220px; border-radius:12px; border:1px solid var(--border); }
  .acts { display:flex; gap:2px; margin-top:6px; opacity:0; transition:opacity .15s; }
  .agent:hover .acts { opacity:1; }
  .acts .iconbtn { width:28px; height:28px; }
  .acts .iconbtn svg { width:15px; height:15px; }
  .dots span { display:inline-block; width:6px; height:6px; margin-right:4px; border-radius:50%;
               background:var(--muted); animation:bounce 1.3s infinite ease-in-out both; }
  .dots span:nth-child(2){ animation-delay:.18s; } .dots span:nth-child(3){ animation-delay:.36s; }
  @keyframes bounce { 0%,80%,100%{ transform:scale(.5); opacity:.4; } 40%{ transform:scale(1); opacity:1; } }

  /* composer */
  #composer { padding:10px 0 22px; }
  .inputwrap { background:var(--card); border:1px solid var(--border); border-radius:22px;
               padding:10px 12px 8px; box-shadow:0 2px 10px rgba(60,50,30,.05);
               transition:border-color .15s, box-shadow .15s; }
  .inputwrap.drag { border-color:var(--accent); }
  .inputwrap:focus-within { border-color:#d8c9b6; box-shadow:0 3px 14px rgba(60,50,30,.09); }
  #thumbs { display:flex; flex-wrap:wrap; gap:8px; padding:4px 4px 8px; }
  #thumbs:empty { display:none; }
  .thumb { position:relative; width:56px; height:56px; border-radius:10px; overflow:hidden; border:1px solid var(--border); }
  .thumb img { width:100%; height:100%; object-fit:cover; }
  .thumb .x { position:absolute; top:2px; right:2px; width:18px; height:18px; border-radius:50%;
              background:rgba(0,0,0,.6); color:#fff; border:none; cursor:pointer; font-size:12px;
              line-height:1; display:flex; align-items:center; justify-content:center; }
  textarea { width:100%; resize:none; border:none; outline:none; background:transparent; color:var(--text);
             font:inherit; font-size:15.5px; line-height:1.5; padding:4px 6px; max-height:200px; min-height:26px; }
  textarea::placeholder { color:var(--muted); }
  .toolbar { display:flex; align-items:center; gap:6px; margin-top:4px; }
  .toolbar .grow { flex:1; }
  .sendbtn { width:34px; height:34px; border:none; border-radius:50%; background:var(--accent);
             color:#fff; cursor:pointer; display:flex; align-items:center; justify-content:center;
             transition:background .15s, opacity .15s; }
  .sendbtn:hover { background:var(--accent-hover); }
  .sendbtn:disabled { opacity:.35; cursor:default; }
  .sendbtn svg { width:18px; height:18px; }
  .micbtn.rec { color:#fff; background:var(--accent); }
  .micbtn.rec:hover { background:var(--accent-hover); }

  /* model selector */
  .model { position:relative; }
  .modelpill { display:flex; align-items:center; gap:6px; background:transparent; border:none;
               color:var(--muted); font-size:12.5px; cursor:pointer; padding:6px 8px; border-radius:8px; }
  .modelpill:hover { background:var(--hover); color:var(--text); }
  .modelpill .spark { width:13px; height:13px; }
  .modelpill .chev { width:12px; height:12px; }
  .menu { position:absolute; bottom:38px; left:0; min-width:210px; background:var(--menu);
          border:1px solid var(--border); border-radius:12px; padding:6px; display:none; z-index:20;
          box-shadow:0 8px 28px rgba(40,30,15,.16); }
  .menu.open { display:block; }
  .menu .item { display:flex; align-items:center; justify-content:space-between; gap:8px;
                padding:8px 10px; border-radius:8px; font-size:13.5px; cursor:pointer; }
  .menu .item:hover { background:var(--hover); }
  .menu .item .check { color:var(--accent); opacity:0; }
  .menu .item.sel .check { opacity:1; }
  .hint { text-align:center; color:var(--muted); font-size:11.5px; margin-top:9px; }
</style>
</head>
<body class="empty">
<header>
  <div class="brand">__SPARK__ <span>__NAME__</span></div>
  <button id="speak" class="iconbtn" title="Read replies aloud">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/></svg>
  </button>
</header>
<div id="app">
  <div id="main">
    <div id="hero">__SPARK__<h1 id="greeting">__NAME__</h1></div>
    <div id="log"></div>
  </div>
  <div id="composer">
    <div class="inputwrap" id="inputwrap">
      <div id="thumbs"></div>
      <textarea id="inp" placeholder="Message __NAME__…" rows="1" autofocus></textarea>
      <div class="toolbar">
        <button id="attach" class="iconbtn" title="Attach image">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21.4 11.05 12.25 20.2a5.5 5.5 0 0 1-7.78-7.78l9.2-9.2a3.67 3.67 0 1 1 5.18 5.2l-9.2 9.2a1.83 1.83 0 0 1-2.6-2.6l8.5-8.48"/></svg>
        </button>
        <div class="model">
          <button id="modelpill" class="modelpill">__SPARK__ <span id="modelname">model</span>
            <svg class="chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>
          </button>
          <div id="menu" class="menu"></div>
        </div>
        <span class="grow"></span>
        <button id="mic" class="iconbtn micbtn" title="Dictate">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10a7 7 0 0 0 14 0"/><path d="M12 19v3"/></svg>
        </button>
        <button id="send" class="sendbtn" title="Send" disabled>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>
        </button>
      </div>
    </div>
    <input id="file" type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden>
    <div class="hint">__NAME__ runs in a hardened Enclave container.</div>
  </div>
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = id => document.getElementById(id);
const log=$("log"), inp=$("inp"), btn=$("send"), greeting=$("greeting");
const thumbs=$("thumbs"), fileIn=$("file"), micBtn=$("mic"), speakBtn=$("speak");
const wrap=$("inputwrap"), menu=$("menu"), modelpill=$("modelpill"), modelname=$("modelname");
let polling=false, pending=[], cfg={models:[],model:""}, autoSpeak=false;

const hr=new Date().getHours();
greeting.textContent = hr<5?"Good evening":hr<12?"Good morning":hr<18?"Good afternoon":"Good evening";

function esc(s){ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function md(s){
  let t=esc(s);
  t=t.replace(/```([\\s\\S]*?)```/g,(m,c)=>"<pre><code>"+c.replace(/^\\n/,"")+"</code></pre>");
  t=t.replace(/`([^`\\n]+)`/g,"<code>$1</code>");
  t=t.replace(/\\*\\*([^*]+)\\*\\*/g,"<strong>$1</strong>");
  return t;
}
function imgURL(p){ return "/"+p+(TOKEN?("?token="+encodeURIComponent(TOKEN)):""); }
function setEmpty(){ document.body.classList.toggle("empty", log.children.length===0); }

async function api(path,opts={}){
  opts.headers=Object.assign({"X-Chat-Token":TOKEN},opts.headers||{});
  return fetch(path+(path.includes("?")?"&":"?")+"token="+encodeURIComponent(TOKEN),opts);
}

/* ---- rendering ---- */
function userMsg(text, images){
  const m=document.createElement("div"); m.className="msg user";
  const col=document.createElement("div"); col.className="col";
  if(images&&images.length){
    const ig=document.createElement("div"); ig.className="imgs";
    images.forEach(p=>{ const im=document.createElement("img"); im.src=imgURL(p); ig.appendChild(im); });
    col.appendChild(ig);
  }
  if(text){ const b=document.createElement("div"); b.className="bubble"; b.textContent=text; col.appendChild(b); }
  m.appendChild(col); log.appendChild(m); setEmpty(); m.scrollIntoView({block:"end"});
}
function agentMsg(){
  const m=document.createElement("div"); m.className="msg agent";
  m.insertAdjacentHTML("afterbegin", `__SPARK__`);
  const col=document.createElement("div"); col.className="col";
  const b=document.createElement("div"); b.className="bubble"; col.appendChild(b);
  m.appendChild(col); log.appendChild(m); setEmpty(); m.scrollIntoView({block:"end"});
  return {b, finalize:(text)=>{
    b.innerHTML=md(text);
    const acts=document.createElement("div"); acts.className="acts";
    const sb=document.createElement("button"); sb.className="iconbtn"; sb.title="Read aloud";
    sb.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>';
    sb.onclick=()=>speak(text);
    const cp=document.createElement("button"); cp.className="iconbtn"; cp.title="Copy";
    cp.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>';
    cp.onclick=()=>navigator.clipboard&&navigator.clipboard.writeText(text);
    acts.append(sb,cp); col.appendChild(acts);
    m.scrollIntoView({block:"end"});
    if(autoSpeak) speak(text);
  }};
}

async function loadHistory(){
  try{ const j=await (await api("/api/history")).json();
    for(const m of j.messages) m.role==="user"? userMsg(m.text,m.images) : agentMsg().finalize(m.text);
  }catch(e){}
  setEmpty();
}

async function pollReply(){
  if(polling) return; polling=true;
  const {b,finalize}=agentMsg();
  b.innerHTML='<span class="dots"><span></span><span></span><span></span></span>';
  const deadline=Date.now()+600000;
  while(Date.now()<deadline){
    await new Promise(r=>setTimeout(r,2000));
    try{ const j=await (await api("/api/poll")).json();
      if(j.reply){ finalize(j.reply); polling=false; return; } }catch(e){}
  }
  b.textContent="(no reply yet — the agent may still be working; it will appear here when ready)";
  polling=false;
}

/* ---- send ---- */
async function send(){
  const text=inp.value.trim();
  if((!text&&!pending.length)||polling) return;
  const images=pending.map(p=>p.path);
  inp.value=""; inp.style.height="auto"; btn.disabled=true;
  pending=[]; renderThumbs();
  userMsg(text, images);
  try{ await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({text,images})}); }catch(e){}
  pollReply();
}
btn.onclick=send;
inp.addEventListener("keydown",e=>{ if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); send(); }});
inp.addEventListener("input",()=>{ inp.style.height="auto"; inp.style.height=Math.min(inp.scrollHeight,200)+"px"; syncSend(); });
function syncSend(){ btn.disabled=(inp.value.trim().length===0 && pending.length===0); }

/* ---- attachments ---- */
$("attach").onclick=()=>fileIn.click();
fileIn.onchange=()=>{ [...fileIn.files].forEach(addFile); fileIn.value=""; };
function addFile(file){
  if(!file.type.startsWith("image/")) return;
  const rd=new FileReader();
  rd.onload=async ()=>{
    const ph={name:file.name,dataURL:rd.result,path:null}; pending.push(ph); renderThumbs(); syncSend();
    try{ const j=await (await api("/api/upload",{method:"POST",headers:{"Content-Type":"application/json"},
          body:JSON.stringify({name:file.name,data:rd.result})})).json();
      if(j.path) ph.path=j.path; else removePending(ph);
    }catch(e){ removePending(ph); }
    renderThumbs(); syncSend();
  };
  rd.readAsDataURL(file);
}
function removePending(ph){ pending=pending.filter(p=>p!==ph); }
function renderThumbs(){
  thumbs.innerHTML="";
  pending.forEach(p=>{ const d=document.createElement("div"); d.className="thumb";
    const im=document.createElement("img"); im.src=p.dataURL; d.appendChild(im);
    const x=document.createElement("button"); x.className="x"; x.textContent="×";
    x.onclick=()=>{ removePending(p); renderThumbs(); syncSend(); }; d.appendChild(x);
    thumbs.appendChild(d); });
}
inp.addEventListener("paste",e=>{ for(const it of (e.clipboardData||{}).items||[]) if(it.type.startsWith("image/")){ const f=it.getAsFile(); if(f) addFile(f); } });
["dragover","dragenter"].forEach(ev=>wrap.addEventListener(ev,e=>{e.preventDefault();wrap.classList.add("drag");}));
["dragleave","drop"].forEach(ev=>wrap.addEventListener(ev,e=>{e.preventDefault();wrap.classList.remove("drag");}));
wrap.addEventListener("drop",e=>{ [...(e.dataTransfer.files||[])].forEach(addFile); });

/* ---- model selector ---- */
async function loadConfig(){
  try{ cfg=await (await api("/api/config")).json(); }catch(e){ return; }
  modelname.textContent = labelFor(cfg.model);
  menu.innerHTML="";
  cfg.models.forEach(m=>{
    const it=document.createElement("div"); it.className="item"+(m.id===cfg.model?" sel":"");
    it.innerHTML=`<span>${esc(m.label)}</span><span class="check">✓</span>`;
    it.onclick=()=>chooseModel(m.id); menu.appendChild(it);
  });
}
function labelFor(id){ const m=cfg.models.find(x=>x.id===id); return m?m.label:id; }
async function chooseModel(id){
  menu.classList.remove("open");
  try{ const j=await (await api("/api/model",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({model:id})})).json();
    if(j.ok){ cfg.model=id; modelname.textContent=labelFor(id); loadConfig(); } }catch(e){}
}
modelpill.onclick=e=>{ e.stopPropagation(); menu.classList.toggle("open"); };
document.addEventListener("click",()=>menu.classList.remove("open"));

/* ---- voice: speak (TTS) — server backend if configured, else browser ---- */
let ttsAudio=null;
async function speak(text){
  if(cfg.tts_backend){
    try{
      if(ttsAudio){ ttsAudio.pause(); ttsAudio=null; }
      const r=await api("/api/tts",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
      if(!r.ok) throw 0;
      ttsAudio=new Audio(URL.createObjectURL(await r.blob())); ttsAudio.play(); return;
    }catch(e){}
  }
  if(!("speechSynthesis" in window)) return;
  speechSynthesis.cancel();
  const u=new SpeechSynthesisUtterance(text.replace(/```[\\s\\S]*?```/g," code block ").replace(/[*`#>]/g,""));
  u.rate=1.02; speechSynthesis.speak(u);
}
function stopSpeak(){ if(ttsAudio){ ttsAudio.pause(); ttsAudio=null; } if("speechSynthesis" in window) speechSynthesis.cancel(); }
speakBtn.onclick=()=>{ autoSpeak=!autoSpeak; speakBtn.classList.toggle("on",autoSpeak); if(!autoSpeak) stopSpeak(); };

/* ---- voice: dictate (STT) — server MediaRecorder backend if configured, else browser SpeechRecognition ---- */
const SR=window.SpeechRecognition||window.webkitSpeechRecognition;
let rec=null, recOn=false, mediaRec=null, chunks=[], srBase="";
if(SR){
  rec=new SR(); rec.continuous=true; rec.interimResults=true; rec.lang=navigator.language||"en-US";
  rec.onresult=e=>{ let s=""; for(let i=e.resultIndex;i<e.results.length;i++) s+=e.results[i][0].transcript;
    inp.value=(srBase+s).trimStart(); inp.dispatchEvent(new Event("input")); };
  rec.onend=()=>{ recOn=false; micBtn.classList.remove("rec"); srBase=inp.value?inp.value+" ":""; };
}
async function startBackendRec(){
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
  mediaRec=new MediaRecorder(stream); chunks=[];
  mediaRec.ondataavailable=e=>{ if(e.data.size) chunks.push(e.data); };
  mediaRec.onstop=async ()=>{
    stream.getTracks().forEach(t=>t.stop()); recOn=false; micBtn.classList.remove("rec");
    const blob=new Blob(chunks,{type:(mediaRec.mimeType||"audio/webm")});
    const dataURL=await new Promise(res=>{ const fr=new FileReader(); fr.onload=()=>res(fr.result); fr.readAsDataURL(blob); });
    try{
      const r=await api("/api/transcribe",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({audio_base64:dataURL.split(",")[1], mime:blob.type})});
      const j=await r.json();
      if(j.text){ inp.value=(inp.value?inp.value+" ":"")+j.text; inp.dispatchEvent(new Event("input")); }
    }catch(e){}
  };
  mediaRec.start(); recOn=true; micBtn.classList.add("rec");
}
micBtn.onclick=async ()=>{
  if(cfg.stt_backend){
    if(recOn && mediaRec){ mediaRec.stop(); } else { try{ await startBackendRec(); }catch(e){} }
  } else if(SR){
    if(recOn){ rec.stop(); } else { srBase=inp.value?inp.value+" ":""; try{ rec.start(); recOn=true; micBtn.classList.add("rec"); }catch(e){} }
  }
};
function syncVoiceUI(){
  micBtn.style.display=(cfg.stt_backend||SR)?"":"none";
  speakBtn.style.display=(cfg.tts_backend||("speechSynthesis" in window))?"":"none";
}

loadConfig().then(syncVoiceUI); loadHistory(); inp.focus();
</script>
</body>
</html>""").replace("__SPARK__", SPARK)


class Handler(BaseHTTPRequestHandler):
    def _auth_ok(self):
        if not TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        return q.get("token", [""])[0] == TOKEN or self.headers.get("X-Chat-Token") == TOKEN

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit=MAX_UPLOAD + 1_000_000):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = PAGE.replace("__NAME__", AGENT_NAME).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401); return
        if path == "/api/history":
            self._json({"messages": _read_history()[-100:]}); return
        if path == "/api/poll":
            with _lock:
                reply = check_new_reply()
            self._json({"reply": reply}); return
        if path == "/api/config":
            self._json(agent_config()); return
        if path.startswith("/uploads/"):
            return self._serve_upload(unquote(path[len("/uploads/"):]))
        self._json({"error": "not found"}, 404)

    def _serve_upload(self, name):
        name = os.path.basename(name)
        target = (UPLOADS / name).resolve()
        try:
            if UPLOADS.resolve() not in target.parents or not target.is_file():
                self._json({"error": "not found"}, 404); return
        except OSError:
            self._json({"error": "not found"}, 404); return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401); return
        path = urlparse(self.path).path
        if path == "/api/send":
            body = self._read_body()
            if body is None:
                self._json({"error": "too large"}, 413); return
            try:
                data = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            text = (data.get("text") or "").strip()
            images = data.get("images") or []
            images = [i for i in images if isinstance(i, str) and i.startswith("uploads/")][:8]
            if not text and not images:
                self._json({"error": "empty"}, 400); return
            with _lock:
                deliver_to_agent(text, images)
            self._json({"ok": True}); return
        if path == "/api/model":
            try:
                data = json.loads(self._read_body(100_000) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            model = (data.get("model") or "").strip()
            if set_model(model):
                self._json({"ok": True, "model": model})
            else:
                self._json({"ok": False, "error": "unknown model"}, 400)
            return
        if path == "/api/upload":
            body = self._read_body()
            if body is None:
                self._json({"error": "too large"}, 413); return
            try:
                data = json.loads(body or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            path_rel, err = save_upload(data.get("name"), data.get("data") or "")
            if err:
                self._json({"error": err}, 400)
            else:
                self._json({"ok": True, "path": path_rel})
            return
        if path == "/api/transcribe":
            if not TRANSCRIBE_URL:
                self._json({"error": "no stt backend"}, 503); return
            try:
                data = json.loads(self._read_body() or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            audio = data.get("audio_base64") or ""
            if not audio:
                self._json({"error": "empty"}, 400); return
            try:
                self._json(proxy_transcribe(audio, data.get("mime") or "audio/webm"))
            except Exception as e:
                self._json({"error": f"stt failed: {e}"}, 502)
            return
        if path == "/api/tts":
            if not TTS_URL:
                self._json({"error": "no tts backend"}, 503); return
            try:
                data = json.loads(self._read_body(2_000_000) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            text = (data.get("text") or "").strip()
            if not text:
                self._json({"error": "empty"}, 400); return
            try:
                audio, ctype = proxy_tts(text[:4000], data.get("voice"))
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                self.wfile.write(audio)
            except Exception as e:
                self._json({"error": f"tts failed: {e}"}, 502)
            return
        self._json({"error": "not found"}, 404)


def main():
    try:
        _last_reply_mtime[0] = REPLY_FILE.stat().st_mtime
    except OSError:
        pass
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    auth = "token-protected" if TOKEN else "OPEN (no token — set WEB_CHAT_TOKEN)"
    print(f"[web_chat] {AGENT_NAME} chat UI on http://0.0.0.0:{PORT}/ ({auth})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
