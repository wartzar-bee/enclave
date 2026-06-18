#!/usr/bin/env python3
"""
web_chat.py — a claude.ai-style web chat interface for an Enclave agent.

Serves a single-page chat UI at http://<host>:<port>/ and bridges to the agent
via the same file convention the Telegram relay uses:

  Inbound:  browser POST /api/send  → append to agent inbox.md (triggers tick)
  Outbound: agent writes state/chat-reply.md → browser polls GET /api/poll

Features (all backend = pure stdlib, no deps; voice runs in the browser):
  • Multi-conversation — claude.ai-style left sidebar (New chat · Search chats · chat list); each chat
    has a "…" menu (Star / Delete), starred pinned to top. New chat = a fresh session; the agent's
    durable memory persists across all of them. Threads live in state/chat/<id>.jsonl + index.json.
  • Image attachments — POST /api/upload saves to uploads/, the agent reads them by path.
  • Voice input  — browser Web Speech API (SpeechRecognition) dictates into the box.
  • Speak replies — browser speechSynthesis reads the agent's answer aloud.
  • Model switch — POST /api/model writes state/model.override; runtime.sh honors it next tick.

The agent bridge is unchanged: a message goes to the chat plane (chat-inbox.jsonl) and the reply comes
back via chat-reply.md; a FIFO routes each reply to the conversation it was sent from. A legacy
state/chat-history.jsonl (single thread) is migrated into one conversation on first start.

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
CHAT_DIR = AGENT_DIR / "state" / "chat"                 # per-conversation threads: <id>.jsonl + index.json
INDEX = CHAT_DIR / "index.json"
OLD_HISTORY = AGENT_DIR / "state" / "chat-history.jsonl"  # legacy single thread → migrated into one conversation
UPLOADS = AGENT_DIR / "uploads"
OUTPUTS = AGENT_DIR / "outputs"          # agent-generated deliverables the operator can download (CSV, reports, …)
DOWNLOAD_EXT = {".csv", ".tsv", ".txt", ".md", ".json", ".xlsx", ".xls", ".pdf", ".zip",
                ".xml", ".log", ".html", ".docx", ".yaml", ".yml"}
WORK_DIR = pathlib.Path(os.environ.get("WORK_DIR", "/work"))   # ro in the chat container — for skill discovery
STOP_FILE = AGENT_DIR / "state" / "chat-stop"   # web_chat touches it → chat_responder kills the in-flight turn
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

_lock = threading.RLock()        # reentrant: conversation helpers are called inside locked sections
_last_reply_mtime = [None]
_upload_n = [0]
_conv_seq = [0]
_pending = []                    # FIFO of conversation ids awaiting an agent reply (routes chat-reply.md back)


# ── conversations (multi-thread, claude.ai-style) + agent bridge ──────────────
# Each conversation is state/chat/<id>.jsonl (one message per line); state/chat/index.json holds
# the sidebar list {id,title,created,updated,count}. Agent stays unchanged: messages go to the chat
# plane (chat-inbox.jsonl) and replies come back via chat-reply.md; a FIFO routes each reply to the
# conversation it was sent from.
_SAFE_ID = re.compile(r"^c[0-9]+$")

def _now():
    return time.time()

def _gen_id():
    _conv_seq[0] += 1
    return f"c{int(time.time() * 1000)}{_conv_seq[0]}"

def _conv_path(cid):
    if not _SAFE_ID.match(cid or ""):
        return None
    return CHAT_DIR / (cid + ".jsonl")

def _title_from_text(text):
    t = " ".join((text or "").split())
    return (t[:48] + "…") if len(t) > 48 else (t or "New chat")

def _load_index():
    try:
        return json.loads(INDEX.read_text())
    except Exception:
        return []

def _save_index(idx):
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(idx))
    tmp.replace(INDEX)

def _migrate_legacy():
    """Fold a pre-existing single chat-history.jsonl into one conversation, once."""
    with _lock:
        if INDEX.exists():
            return
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        idx = []
        if OLD_HISTORY.exists():
            lines = [l for l in OLD_HISTORY.read_text().splitlines() if l.strip()]
            if lines:
                cid = _gen_id()
                _conv_path(cid).write_text("\n".join(lines) + "\n")
                first = next((json.loads(l).get("text") for l in lines if '"user"' in l), "")
                idx = [{"id": cid, "title": _title_from_text(first), "created": _now(),
                        "updated": _now(), "count": len(lines)}]
        _save_index(idx)

def _append_msg(cid, role, text, images=None):
    with _lock:
        p = _conv_path(cid)
        if not p:
            return
        CHAT_DIR.mkdir(parents=True, exist_ok=True)
        rec = {"ts": _now(), "role": role, "text": text}
        if images:
            rec["images"] = images
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
        idx = _load_index()
        e = next((c for c in idx if c["id"] == cid), None)
        if e is None:
            e = {"id": cid, "title": "New chat", "created": _now(), "updated": _now(), "count": 0}
            idx.append(e)
        if role == "user" and e.get("title", "New chat") == "New chat":
            e["title"] = _title_from_text(text)
        e["updated"] = _now()
        e["count"] = e.get("count", 0) + 1
        _save_index(idx)

def list_conversations():
    # starred pinned to top, then most-recently-updated (ChatGPT/Claude order)
    return sorted(_load_index(), key=lambda c: (bool(c.get("starred")), c.get("updated", 0)), reverse=True)

def conversation_messages(cid):
    p = _conv_path(cid)
    if not p or not p.exists():
        return []
    try:
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    except Exception:
        return []

def conversation_markdown(cid):
    """Render a conversation as portable markdown (title + each turn). Returns (filename, text) or None."""
    msgs = conversation_messages(cid)
    if not msgs:
        return None
    title = next((c.get("title") for c in _load_index() if c.get("id") == cid), None) or "conversation"
    lines = [f"# {title}", ""]
    for m in msgs:
        who = "You" if m.get("role") == "user" else AGENT_NAME
        lines.append(f"## {who}")
        if m.get("text"):
            lines.append(m["text"])
        for im in (m.get("images") or []):
            lines.append(f"![image](/{im})")
        lines.append("")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:48] or "conversation"
    return (f"{safe}.md", "\n".join(lines) + "\n")

def delete_conversation(cid):
    with _lock:
        p = _conv_path(cid)
        if p and p.exists():
            try:
                p.unlink()
            except OSError:
                pass
        _save_index([c for c in _load_index() if c["id"] != cid])
        return True

def star_conversation(cid, starred):
    with _lock:
        idx = _load_index()
        e = next((c for c in idx if c["id"] == cid), None)
        if not e:
            return False
        e["starred"] = bool(starred)
        _save_index(idx)
        return True

def search_conversations(q):
    q = (q or "").strip().lower()
    if not q:
        return []
    out = []
    for c in list_conversations():
        snippet = None
        for m in conversation_messages(c["id"]):
            if q in (m.get("text") or "").lower():
                t = " ".join((m["text"]).split())
                i = t.lower().find(q)
                snippet = ("…" if i > 24 else "") + t[max(0, i - 24):i + 80] + "…"
                break
        if snippet or q in c.get("title", "").lower():
            out.append({"id": c["id"], "title": c["title"], "snippet": snippet or ""})
    return out

def deliver_to_agent(cid, text, images=None):
    """Persist the user msg to its conversation, queue the conversation for the reply, hand to the agent."""
    _append_msg(cid, "user", text, images)
    with _lock:
        _pending.append(cid)
    rec = {"ts": _now(), "text": text, "images": images or [], "conversation": cid}
    CHAT_INBOX.parent.mkdir(parents=True, exist_ok=True)
    with CHAT_INBOX.open("a") as f:
        f.write(json.dumps(rec) + "\n")

def check_new_reply():
    """Return (reply_text, conversation_id) if chat-reply.md changed, else (None, None)."""
    try:
        mtime = REPLY_FILE.stat().st_mtime
    except OSError:
        return None, None
    if mtime == _last_reply_mtime[0]:
        return None, None
    _last_reply_mtime[0] = mtime
    try:
        content = REPLY_FILE.read_text().strip()
    except OSError:
        return None, None
    if not content:
        return None, None
    with _lock:
        if _pending:
            cid = _pending.pop(0)
        else:                                  # proactive reply (no pending send) → newest conversation
            idx = list_conversations()
            cid = idx[0]["id"] if idx else None
    if cid:
        _append_msg(cid, "agent", content)
    try:
        REPLY_FILE.write_text("")
    except OSError:
        pass
    return content, cid


# ── slash commands (UI controls + discoverable skills) ───────────────────────
def _skill_desc(md):
    try:
        lines = md.read_text(errors="ignore").splitlines()
    except OSError:
        return ""
    for i, ln in enumerate(lines):
        m = re.match(r"^description:\s*(.*)$", ln)
        if m:
            v = m.group(1).strip().strip('"').strip("'")
            if v and v not in ("|", ">", "|-", ">-"):
                return v
            for nxt in lines[i + 1:i + 4]:        # block scalar → first indented line
                if nxt.strip():
                    return nxt.strip()
            return ""
    return ""

def list_commands():
    """UI commands + skills discovered in /agent and /work .claude/skills — powers the slash menu."""
    cmds = [
        {"cmd": "/clear", "desc": "Start a new chat", "kind": "ui"},
        {"cmd": "/retry", "desc": "Resend your last message (re-ask)", "kind": "ui"},
        {"cmd": "/export", "desc": "Download this chat as markdown", "kind": "ui"},
        {"cmd": "/help", "desc": "Show available commands", "kind": "ui"},
        # Claude Code built-ins that work in headless `claude -p` (run in the agent's session, output shown
        # in chat). Interactive-only ones (/remote-control,/model,/config,/agents,/memory,/resume) excluded.
        {"cmd": "/usage", "desc": "Token & cost / cap usage breakdown", "kind": "builtin"},
        {"cmd": "/context", "desc": "Context-window usage", "kind": "builtin"},
        {"cmd": "/mcp", "desc": "MCP server status", "kind": "builtin"},
        {"cmd": "/recap", "desc": "One-line recap of this session", "kind": "builtin"},
    ]
    seen = set()
    for root in (AGENT_DIR / ".claude" / "skills", WORK_DIR / ".claude" / "skills"):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for p in entries:
            name = desc = None
            if p.is_dir() and (p / "SKILL.md").exists():
                name, desc = p.name, _skill_desc(p / "SKILL.md")
            elif p.suffix == ".md" and p.name not in ("INDEX.md", "README.md", "ROADMAP.md"):
                name, desc = p.stem, _skill_desc(p)
            if name and name not in seen:
                seen.add(name)
                cmds.append({"cmd": "/" + name, "desc": (desc or "")[:120], "kind": "skill"})
    return cmds


# ── model config ─────────────────────────────────────────────────────────────
# Short aliases the CLI accepts → canonical ids (so the picker shows a real, allowlisted model).
_MODEL_ALIAS = {"opus": "claude-opus-4-8", "sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5"}

def agent_config():
    brain, tick_model, chat_model = "claude", "", ""
    envf = AGENT_DIR / "agent.env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line.startswith("BRAIN="):
                brain = line.split("=", 1)[1].strip() or brain
            elif line.startswith("BRAIN_MODEL="):
                tick_model = line.split("=", 1)[1].strip()
            elif line.startswith("MODEL=") and not tick_model:
                tick_model = line.split("=", 1)[1].strip()
            elif line.startswith("CHAT_MODEL="):
                chat_model = line.split("=", 1)[1].strip()
    chat_model = os.environ.get("CHAT_MODEL", "").strip() or chat_model
    override = ""
    if OVERRIDE.exists():
        try:
            o = OVERRIDE.read_text().strip().splitlines()
            if o and o[0].strip():
                override = o[0].strip()
        except OSError:
            pass
    # Reflect the model the CHAT actually uses — same precedence as chat_responder._chat_model:
    # UI override > CHAT_MODEL > the agent's tick MODEL. Normalize CLI aliases to canonical ids.
    model = override or chat_model or tick_model
    model = _MODEL_ALIAS.get(model, model)
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
  /* ?theme=light|dark forces the theme (overrides the OS preference) — used when embedded in the fleet console */
  html[data-theme="light"] { --bg:#faf9f5; --card:#ffffff; --text:#28261f; --muted:#73726c; --accent:#d97757; --accent-hover:#c2603f; --user:#f0eee6; --border:#e7e3d8; --code:#f3f1ea; --menu:#ffffff; --hover:#f3f1ea; }
  html[data-theme="dark"]  { --bg:#262624; --card:#30302e; --text:#ececec; --muted:#9a988f; --user:#3a3a37; --border:#3f3f3b; --code:#1f1f1d; --menu:#30302e; --hover:#3a3a37; }
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
  .bubble table { border-collapse:collapse; margin:10px 0; font-size:14px; display:block; overflow-x:auto; max-width:100%; }
  .bubble th, .bubble td { border:1px solid var(--border); padding:6px 12px; text-align:left; vertical-align:top; }
  .bubble th { background:var(--code); font-weight:650; white-space:nowrap; }
  .bubble ul, .bubble ol { margin:8px 0; padding-left:24px; }
  .bubble li { margin:3px 0; }
  .bubble h1, .bubble h2, .bubble h3 { font-size:1.05em; font-weight:650; margin:14px 0 6px; }
  .bubble blockquote { border-left:3px solid var(--border); margin:8px 0; padding:2px 0 2px 12px; color:var(--muted); }
  .bubble a { color:var(--accent); text-decoration:underline; }
  .bubble a.dl { display:inline-flex; align-items:center; gap:6px; text-decoration:none; background:var(--user);
                 border:1px solid var(--border); border-radius:9px; padding:6px 12px; margin:4px 0; font-size:13.5px;
                 color:var(--text); font-weight:550; }
  .bubble a.dl:hover { border-color:var(--accent); color:var(--accent); }
  .bubble hr { border:none; border-top:1px solid var(--border); margin:13px 0; }
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

  /* slash-command menu */
  #composer { position:relative; }
  .slashmenu { display:none; position:absolute; bottom:74px; left:0; right:0; max-height:280px; overflow-y:auto;
               background:var(--menu); border:1px solid var(--border); border-radius:12px; padding:6px;
               box-shadow:0 8px 28px rgba(40,30,15,.16); z-index:25; }
  .slashmenu.open { display:block; }
  .slashmenu .si { display:flex; align-items:baseline; gap:10px; padding:8px 11px; border-radius:8px; cursor:pointer; }
  .slashmenu .si.sel, .slashmenu .si:hover { background:var(--hover); }
  .slashmenu .si .c { font-family:ui-monospace,Menlo,monospace; font-size:13px; color:var(--accent); font-weight:600; white-space:nowrap; }
  .slashmenu .si .d { font-size:12.5px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .sendbtn.stopmode { background:var(--text); }
  .sendbtn.stopmode:hover { background:var(--accent-hover); }

  /* ── sidebar (collapsible, ChatGPT/Claude-style) ── */
  #shell { flex:1; display:flex; min-height:0; }
  #sidebar { width:268px; flex:0 0 268px; background:var(--card); border-right:1px solid var(--border);
             display:flex; flex-direction:column; gap:6px; padding:10px; overflow:hidden;
             transition:flex-basis .18s ease, width .18s ease, padding .18s ease; }
  body.collapsed #sidebar { flex-basis:0; width:0; padding:10px 0; border-right:none; }
  body.collapsed #sidebar > * { opacity:0; pointer-events:none; }
  /* mobile: sidebar slides in as an overlay within #shell (no header-offset math); chat is full-width */
  @media (max-width:720px){
    #shell { position:relative; }
    #sidebar { position:absolute; top:0; left:0; bottom:0; z-index:40; width:84%; max-width:300px;
               flex-basis:auto; transform:translateX(0); transition:transform .2s ease;
               box-shadow:2px 0 20px rgba(0,0,0,.3); }
    body.collapsed #sidebar { transform:translateX(-104%); width:84%; max-width:300px; padding:10px;
               border-right:1px solid var(--border); }
    body.collapsed #sidebar > * { opacity:1; pointer-events:auto; }
    #app { max-width:100%; width:100%; }
  }
  .sb-btn { display:flex; align-items:center; gap:9px; width:100%; text-align:left; border:1px solid var(--border);
            background:transparent; color:var(--text); border-radius:10px; padding:9px 11px; cursor:pointer;
            font:inherit; font-size:13.5px; white-space:nowrap; }
  .sb-btn:hover { background:var(--hover); }
  .sb-btn svg { width:16px; height:16px; flex:0 0 16px; color:var(--muted); }
  .sb-search { display:flex; align-items:center; gap:8px; background:var(--bg); border:1px solid var(--border);
               border-radius:10px; padding:7px 10px; }
  .sb-search svg { width:15px; height:15px; color:var(--muted); flex:0 0 15px; }
  .sb-search input { border:none; outline:none; background:transparent; color:var(--text); font:inherit;
                     font-size:13px; width:100%; }
  #convlist { flex:1; overflow-y:auto; margin-top:4px; display:flex; flex-direction:column; gap:1px; }
  .sb-sec { color:var(--muted); font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.4px;
            padding:10px 11px 4px; white-space:nowrap; }
  .conv { position:relative; display:flex; align-items:center; gap:6px; border-radius:9px; padding:8px 6px 8px 11px;
          cursor:pointer; white-space:nowrap; }
  .conv:hover { background:var(--hover); }
  .conv.active { background:var(--hover); }
  .conv .ti { flex:1; overflow:hidden; text-overflow:ellipsis; font-size:13.5px; }
  .conv .star { color:var(--accent); width:13px; height:13px; flex:0 0 13px; }
  .conv .kebab { width:26px; height:26px; border:none; background:transparent; color:var(--muted); border-radius:7px;
                 cursor:pointer; display:none; align-items:center; justify-content:center; flex:0 0 26px; }
  .conv:hover .kebab, .conv .kebab.open { display:flex; }
  .conv .kebab:hover { background:var(--border); color:var(--text); }
  .convmenu { position:absolute; right:6px; top:34px; z-index:30; min-width:150px; background:var(--menu);
              border:1px solid var(--border); border-radius:10px; padding:5px; display:none;
              box-shadow:0 8px 28px rgba(40,30,15,.18); }
  .convmenu.open { display:block; }
  .convmenu .mi { display:flex; align-items:center; gap:9px; padding:8px 10px; border-radius:7px; font-size:13px; cursor:pointer; }
  .convmenu .mi:hover { background:var(--hover); }
  .convmenu .mi svg { width:15px; height:15px; color:var(--muted); }
  .convmenu .mi.del { color:#c2603f; } .convmenu .mi.del svg { color:#c2603f; }
</style>
<script>(function(){var t=new URLSearchParams(location.search).get("theme");if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);})();</script>
</head>
<body class="empty">
<header>
  <div style="display:flex;align-items:center;gap:4px">
    <button id="toggle" class="iconbtn" title="Toggle sidebar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M3 12h18M3 18h18"/></svg>
    </button>
    <div class="brand">__SPARK__ <span>__NAME__</span></div>
  </div>
  <button id="speak" class="iconbtn" title="Read replies aloud">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M19 5a9 9 0 0 1 0 14"/></svg>
  </button>
</header>
<div id="shell">
  <aside id="sidebar">
    <button id="newchat" class="sb-btn" title="New chat">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>
      New chat
    </button>
    <div class="sb-search">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>
      <input id="search" placeholder="Search chats" autocomplete="off">
    </div>
    <div id="convlist"></div>
  </aside>
  <div id="app">
  <div id="main">
    <div id="hero">__SPARK__<h1 id="greeting">__NAME__</h1></div>
    <div id="log"></div>
  </div>
  <div id="composer">
    <div id="slash" class="slashmenu"></div>
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
</div>
<script>
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const $ = id => document.getElementById(id);
const log=$("log"), inp=$("inp"), btn=$("send"), greeting=$("greeting");
const thumbs=$("thumbs"), fileIn=$("file"), micBtn=$("mic"), speakBtn=$("speak");
const wrap=$("inputwrap"), menu=$("menu"), modelpill=$("modelpill"), modelname=$("modelname");
let polling=false, pending=[], cfg={models:[],model:""}, autoSpeak=false;
let activeConv=null, commands=[], slashSel=0, stopReq=false, lastUserText="";
const convlist=$("convlist"), searchIn=$("search"), slash=$("slash");

const hr=new Date().getHours();
greeting.textContent = hr<5?"Good evening":hr<12?"Good morning":hr<18?"Good afternoon":"Good evening";

function esc(s){ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function mdRow(l){ return l.trim().replace(/^\\|/,"").replace(/\\|$/,"").split("|").map(c=>c.trim()); }
function mdTable(head,body){
  let h="<table><thead><tr>"+head.map(c=>"<th>"+esc(c)+"</th>").join("")+"</tr></thead>";
  if(body.length) h+="<tbody>"+body.map(r=>"<tr>"+r.map(c=>"<td>"+esc(c)+"</td>").join("")+"</tr>").join("")+"</tbody>";
  return h+"</table>";
}
function md(s){
  // 1) pull fenced code blocks OUT first — their content stays literal (never parsed as markdown/html)
  const blocks=[];
  s=s.replace(/```[a-zA-Z0-9_-]*\\r?\\n?([\\s\\S]*?)```/g,(m,c)=>{ blocks.push(c.replace(/\\n$/,"")); return "\\u0000"+(blocks.length-1)+"\\u0000"; });
  // 2) block pass: tables + lists need line grouping
  const lines=s.split(/\\r?\\n/), out=[]; let i=0;
  const isSep=l=>/-/.test(l)&&/^\\s*\\|?[\\s:|-]+\\|?\\s*$/.test(l);
  while(i<lines.length){
    const ln=lines[i];
    if(i+1<lines.length && ln.includes("|") && ln.trim() && isSep(lines[i+1])){
      const head=mdRow(ln); i+=2; const body=[];
      while(i<lines.length && lines[i].includes("|") && lines[i].trim()){ body.push(mdRow(lines[i])); i++; }
      out.push(mdTable(head,body)); continue;
    }
    if(/^\\s*[-*+]\\s+/.test(ln)){
      const it=[]; while(i<lines.length && /^\\s*[-*+]\\s+/.test(lines[i])){ it.push(lines[i].replace(/^\\s*[-*+]\\s+/,"")); i++; }
      out.push("<ul>"+it.map(x=>"<li>"+esc(x)+"</li>").join("")+"</ul>"); continue;
    }
    if(/^\\s*\\d+\\.\\s+/.test(ln)){
      const it=[]; while(i<lines.length && /^\\s*\\d+\\.\\s+/.test(lines[i])){ it.push(lines[i].replace(/^\\s*\\d+\\.\\s+/,"")); i++; }
      out.push("<ol>"+it.map(x=>"<li>"+esc(x)+"</li>").join("")+"</ol>"); continue;
    }
    out.push(esc(ln)); i++;
  }
  let t=out.join("\\n");
  // 3) headings, blockquote, hr (line-anchored; content already escaped)
  t=t.replace(/^\\s*#{1,6}\\s+(.+)$/gm,"<h3>$1</h3>");
  t=t.replace(/^\\s*&gt;\\s?(.+)$/gm,"<blockquote>$1</blockquote>");
  t=t.replace(/^\\s*([-*_])\\1{2,}\\s*$/gm,"<hr>");
  // 4) inline: code, bold, italic, links
  t=t.replace(/`([^`\\n]+)`/g,"<code>$1</code>");
  t=t.replace(/\\*\\*([^*\\n]+)\\*\\*/g,"<strong>$1</strong>");
  t=t.replace(/(^|[^*\\w])\\*([^*\\n]+)\\*(?!\\w)/g,"$1<em>$2</em>");
  t=t.replace(/\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  // relative download/uploads links the agent emits → clickable, token appended, forced download
  t=t.replace(/\\[([^\\]\\n]+)\\]\\((\\/(?:download|uploads)[^)\\s]*)\\)/g,(m,label,href)=>{
    const u=href+(TOKEN?(href.includes("?")?"&":"?")+"token="+encodeURIComponent(TOKEN):"");
    return '<a class="dl" href="'+u+'" download>⬇ '+label+'</a>';
  });
  // 5) restore fenced code blocks (escaped, literal)
  t=t.replace(/\\u0000(\\d+)\\u0000/g,(m,n)=>"<pre><code>"+esc(blocks[n])+"</code></pre>");
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

/* ---- conversations (sidebar) ---- */
const STAR_FILL='<svg class="star" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3 7h7l-5.5 4 2 7L12 16l-6.5 4 2-7L2 9h7z"/></svg>';
async function loadConversations(filter){
  let items=[];
  try{
    const url=filter?("/api/search?q="+encodeURIComponent(filter)):"/api/conversations";
    const j=await (await api(url)).json(); items=filter?j.results:j.conversations;
  }catch(e){}
  renderConvList(items||[], !!filter);
}
function addSec(t){ const d=document.createElement("div"); d.className="sb-sec"; d.textContent=t; convlist.appendChild(d); }
function addConv(c){
  const row=document.createElement("div"); row.className="conv"+(c.id===activeConv?" active":""); row.dataset.id=c.id;
  if(c.starred) row.insertAdjacentHTML("beforeend",STAR_FILL);
  const ti=document.createElement("div"); ti.className="ti"; ti.textContent=c.title||"New chat"; row.appendChild(ti);
  const kb=document.createElement("button"); kb.className="kebab"; kb.title="Options";
  kb.innerHTML='<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="19" cy="12" r="2"/></svg>';
  kb.onclick=e=>{ e.stopPropagation(); openConvMenu(row,c,kb); };
  row.appendChild(kb);
  row.onclick=()=>selectConv(c.id);
  convlist.appendChild(row);
}
function renderConvList(items, isSearch){
  convlist.innerHTML="";
  if(!items.length){ addSec(isSearch?"No matches":"No chats yet"); return; }
  if(isSearch){ items.forEach(addConv); return; }
  const star=items.filter(c=>c.starred), rest=items.filter(c=>!c.starred);
  if(star.length){ addSec("Starred"); star.forEach(addConv); if(rest.length) addSec("Chats"); }
  rest.forEach(addConv);
}
function closeConvMenus(){ document.querySelectorAll(".convmenu").forEach(m=>m.remove()); document.querySelectorAll(".kebab.open").forEach(k=>k.classList.remove("open")); }
function openConvMenu(row,c,kb){
  closeConvMenus(); kb.classList.add("open");
  const menu=document.createElement("div"); menu.className="convmenu open";
  const star=document.createElement("div"); star.className="mi";
  star.innerHTML=(c.starred?STAR_FILL:'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M12 2l3 7h7l-5.5 4 2 7L12 16l-6.5 4 2-7L2 9h7z"/></svg>')+"<span>"+(c.starred?"Unstar":"Star")+"</span>";
  star.onclick=async e=>{ e.stopPropagation(); closeConvMenus();
    try{ await api("/api/conversation/star",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:c.id,starred:!c.starred})}); }catch(e){}
    loadConversations(searchIn.value.trim()); };
  const exp=document.createElement("div"); exp.className="mi";
  exp.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg><span>Export markdown</span>';
  exp.onclick=e=>{ e.stopPropagation(); closeConvMenus(); exportConv(c.id); };
  const del=document.createElement("div"); del.className="mi del";
  del.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg><span>Delete chat</span>';
  del.onclick=async e=>{ e.stopPropagation(); closeConvMenus(); if(!confirm("Delete this chat? This cannot be undone.")) return;
    try{ await api("/api/conversation/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:c.id})}); }catch(e){}
    if(c.id===activeConv) newChat();
    loadConversations(searchIn.value.trim()); };
  menu.append(star,exp,del); row.appendChild(menu);
}
async function selectConv(id){
  if(polling) return;
  activeConv=id; closeConvMenus(); log.innerHTML="";
  try{ const j=await (await api("/api/conversation?id="+encodeURIComponent(id))).json();
    for(const m of j.messages) m.role==="user"? userMsg(m.text,m.images) : agentMsg().finalize(m.text);
  }catch(e){}
  setEmpty();
  document.querySelectorAll(".conv").forEach(r=>r.classList.toggle("active", r.dataset.id===id));
  if(window.matchMedia("(max-width:720px)").matches) document.body.classList.add("collapsed");  // close the overlay
}
function newChat(){ activeConv=null; closeConvMenus(); log.innerHTML=""; setEmpty(); inp.focus();
  document.querySelectorAll(".conv.active").forEach(r=>r.classList.remove("active")); }

/* ---- slash commands (UI controls + skills) ---- */
async function loadCommands(){ try{ commands=(await (await api("/api/commands")).json()).commands||[]; }catch(e){} }
function slashMatches(){
  const v=inp.value;
  if(!v.startsWith("/")||v.includes(" ")||v.includes("\\n")) return null;
  const q=v.slice(1).toLowerCase();
  if(!q) return commands.slice(0,8);
  const scored=[];                              // match anywhere in name (then description), prefix ranked first
  for(const c of commands){
    const name=c.cmd.slice(1).toLowerCase(), desc=(c.desc||"").toLowerCase();
    const s = name.startsWith(q)?0 : name.includes(q)?1 : desc.includes(q)?2 : -1;
    if(s>=0) scored.push([s,c]);
  }
  scored.sort((a,b)=>a[0]-b[0]||a[1].cmd.length-b[1].cmd.length);
  return scored.slice(0,8).map(x=>x[1]);
}
function renderSlash(){
  const ms=slashMatches();
  if(!ms||!ms.length){ slash.classList.remove("open"); return; }
  slashSel=Math.min(slashSel,ms.length-1); if(slashSel<0) slashSel=0;
  slash.innerHTML=ms.map((c,i)=>`<div class="si${i===slashSel?' sel':''}" data-cmd="${esc(c.cmd)}"><span class="c">${esc(c.cmd)}</span><span class="d">${esc(c.desc||'')}</span></div>`).join("");
  [...slash.children].forEach((el,i)=>{ el.onmousedown=e=>{e.preventDefault();pickSlash(el.dataset.cmd);}; });
  slash.classList.add("open");
}
function pickSlash(cmd){
  slash.classList.remove("open");
  const c=commands.find(x=>x.cmd===cmd);
  if(c&&c.kind==="ui"){ inp.value=""; syncSend(); runUI(cmd); return; }
  if(c&&c.kind==="builtin"){ inp.value=cmd; send(); return; }   // Claude Code built-in → run in-session now
  inp.value=cmd+" "; inp.focus(); inp.dispatchEvent(new Event("input"));   // skill → insert, user adds args
}
function runUI(cmd){
  if(cmd==="/clear"||cmd==="/new") newChat();
  else if(cmd==="/help") showHelp();
  else if(cmd==="/retry") retryLast();
  else if(cmd==="/export") exportConv(activeConv);
}
function exportConv(id){
  if(!id){ agentMsg().finalize("_Nothing to export — this chat hasn't started yet._"); return; }
  const u="/api/conversation/export?id="+encodeURIComponent(id)+(TOKEN?("&token="+encodeURIComponent(TOKEN)):"");
  const a=document.createElement("a"); a.href=u; a.download=""; document.body.appendChild(a); a.click(); a.remove();
}
function retryLast(){
  if(polling||!lastUserText){ return; }
  inp.value=lastUserText; inp.dispatchEvent(new Event("input")); send();
}
function showHelp(){
  const ui=commands.filter(c=>c.kind==="ui").map(c=>`${c.cmd} — ${c.desc}`).join("\\n");
  const sk=commands.filter(c=>c.kind==="skill").map(c=>`${c.cmd} — ${c.desc||""}`).join("\\n");
  agentMsg().finalize("**Commands**\\n\\nType `/` to search. UI commands run here; skills run the agent.\\n\\n"+
    ui+(sk?("\\n\\n**Skills**\\n"+sk):""));
}
/* ---- stop button (send button toggles to Stop while a turn runs) ---- */
function setStopMode(on){
  if(on){ btn.classList.add("stopmode"); btn.disabled=false; btn.title="Stop";
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="2"/></svg>'; }
  else { btn.classList.remove("stopmode"); btn.title="Send";
    btn.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
    syncSend(); }
}
async function stopTurn(){ stopReq=true; try{ await api("/api/stop",{method:"POST"}); }catch(e){} }

async function pollReply(convAtSend){
  if(polling) return; polling=true; stopReq=false; setStopMode(true);
  const {b,finalize}=agentMsg();
  b.innerHTML='<span class="dots"><span></span><span></span><span></span></span>';
  const deadline=Date.now()+600000; let done=false;
  while(Date.now()<deadline && !done){
    await new Promise(r=>setTimeout(r,2000));
    if(stopReq){
      if(activeConv===convAtSend) finalize("_(stopped)_");
      else { const mm=b.closest(".msg"); if(mm) mm.remove(); }
      done=true; break;
    }
    try{ const j=await (await api("/api/poll")).json();
      if(j.reply){
        if(activeConv===convAtSend) finalize(j.reply);       // still viewing that thread → show it
        else { const mm=b.closest(".msg"); if(mm) mm.remove(); }  // navigated away; saved in its thread
        done=true;
      } }catch(e){}
  }
  if(done) loadConversations(searchIn.value.trim());          // bump title/order in the sidebar
  else if(!stopReq) b.textContent="(no reply yet — the agent may still be working; it will appear here when ready)";
  polling=false; setStopMode(false);
}

/* ---- send ---- */
async function send(){
  const text=inp.value.trim();
  slash.classList.remove("open");
  if(text==="/clear"||text==="/new"){ inp.value=""; inp.style.height="auto"; syncSend(); newChat(); return; }
  if(text==="/help"){ inp.value=""; inp.style.height="auto"; syncSend(); showHelp(); return; }
  if((!text&&!pending.length)||polling) return;
  const images=pending.map(p=>p.path);
  if(text) lastUserText=text;                 // remembered for /retry
  inp.value=""; inp.style.height="auto"; btn.disabled=true;
  pending=[]; renderThumbs();
  userMsg(text, images);
  const wasNew = !activeConv;
  try{ const j=await (await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({text,images,conversation:activeConv})})).json();
    if(j.conversation) activeConv=j.conversation;
  }catch(e){}
  if(wasNew){ if(searchIn.value){ searchIn.value=""; } loadConversations(); }
  pollReply(activeConv);
}
btn.onclick=()=>{ btn.classList.contains("stopmode") ? stopTurn() : send(); };
inp.addEventListener("keydown",e=>{
  if(slash.classList.contains("open")){
    const items=[...slash.children];
    if(e.key==="ArrowDown"){ e.preventDefault(); slashSel=(slashSel+1)%items.length; renderSlash(); return; }
    if(e.key==="ArrowUp"){ e.preventDefault(); slashSel=(slashSel-1+items.length)%items.length; renderSlash(); return; }
    if(e.key==="Enter"||e.key==="Tab"){ e.preventDefault(); if(items[slashSel]) pickSlash(items[slashSel].dataset.cmd); return; }
    if(e.key==="Escape"){ slash.classList.remove("open"); return; }
  }
  if(e.key==="Enter"&&!e.shiftKey){ e.preventDefault(); send(); }
});
/* global shortcuts: Cmd/Ctrl+K = new chat, Esc = stop a running turn (else close menus) */
document.addEventListener("keydown",e=>{
  if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==="k"){ e.preventDefault(); newChat(); return; }
  if(e.key==="Escape"){ if(polling){ stopTurn(); return; } closeConvMenus(); slash.classList.remove("open"); }
});
inp.addEventListener("input",()=>{ inp.style.height="auto"; inp.style.height=Math.min(inp.scrollHeight,200)+"px"; syncSend(); slashSel=0; renderSlash(); });
inp.addEventListener("blur",()=>setTimeout(()=>slash.classList.remove("open"),150));
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

/* ---- sidebar: toggle, new chat, search ---- */
$("toggle").onclick=()=>{ const c=document.body.classList.toggle("collapsed"); try{ localStorage.setItem("sb_collapsed", c?"1":""); }catch(e){} };
try{ if(localStorage.getItem("sb_collapsed")) document.body.classList.add("collapsed"); }catch(e){}
if(window.matchMedia("(max-width:720px)").matches) document.body.classList.add("collapsed");  // mobile: start closed
$("newchat").onclick=newChat;
let searchT=null;
searchIn.addEventListener("input",()=>{ clearTimeout(searchT); searchT=setTimeout(()=>loadConversations(searchIn.value.trim()),200); });
document.addEventListener("click",closeConvMenus);

loadConfig().then(syncVoiceUI);
loadCommands();
loadConversations().then(()=>{ const first=convlist.querySelector(".conv"); first? selectConv(first.dataset.id) : newChat(); });
inp.focus();
/* background poller: surfaces a reply whenever it lands, even outside an active send's pollReply
   (e.g. a slow turn that finished after the loop, or a second queued message) — so a reply never
   waits for the operator to send another message to appear. */
setInterval(async()=>{
  if(polling) return;                                  // pollReply owns delivery during a turn
  try{ const j=await (await api("/api/poll")).json();
    if(j&&j.reply){
      if(!activeConv || j.conversation===activeConv) agentMsg().finalize(j.reply);
      loadConversations(searchIn.value.trim());
    }
  }catch(e){}
}, 3000);
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
        if path == "/api/conversations":
            self._json({"conversations": list_conversations()}); return
        if path == "/api/conversation":
            cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            self._json({"messages": conversation_messages(cid)}); return
        if path == "/api/search":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            self._json({"results": search_conversations(q)}); return
        if path == "/api/conversation/export":
            cid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            md = conversation_markdown(cid) if _SAFE_ID.match(cid or "") else None
            if not md:
                self._json({"error": "not found"}, 404); return
            fname, text = md
            data = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data); return
        if path == "/api/poll":
            with _lock:
                reply, cid = check_new_reply()
            self._json({"reply": reply, "conversation": cid}); return
        if path == "/api/config":
            self._json(agent_config()); return
        if path == "/api/commands":
            self._json({"commands": list_commands()}); return
        if path.startswith("/uploads/"):
            return self._serve_upload(unquote(path[len("/uploads/"):]))
        if path == "/download":
            return self._serve_download(parse_qs(urlparse(self.path).query).get("path", [""])[0])
        if path == "/api/outputs":          # list downloadable deliverables (for a future panel; cheap)
            try:
                items = sorted((p.name for p in OUTPUTS.iterdir() if p.is_file() and p.suffix.lower() in DOWNLOAD_EXT))
            except OSError:
                items = []
            self._json({"files": items}); return
        self._json({"error": "not found"}, 404)

    def _serve_download(self, rel):
        """Serve an agent-generated deliverable from OUTPUTS as a download. Token-gated (checked in
        do_GET), path-traversal-safe (basename + containment), extension-allowlisted."""
        name = os.path.basename(unquote(rel or ""))
        if not name or os.path.splitext(name)[1].lower() not in DOWNLOAD_EXT:
            self._json({"error": "not found"}, 404); return
        target = (OUTPUTS / name).resolve()
        try:
            if OUTPUTS.resolve() not in target.parents or not target.is_file():
                self._json({"error": "not found"}, 404); return
        except OSError:
            self._json({"error": "not found"}, 404); return
        data = target.read_bytes()
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')   # force download
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
            cid = (data.get("conversation") or "").strip()
            with _lock:
                if not _SAFE_ID.match(cid):     # null/invalid → start a new conversation (claude-style)
                    cid = _gen_id()
                deliver_to_agent(cid, text, images)
            self._json({"ok": True, "conversation": cid}); return
        if path == "/api/conversation/delete":
            try:
                data = json.loads(self._read_body(100_000) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            delete_conversation((data.get("id") or "").strip())
            self._json({"ok": True}); return
        if path == "/api/conversation/star":
            try:
                data = json.loads(self._read_body(100_000) or b"{}")
            except Exception:
                self._json({"error": "bad json"}, 400); return
            ok = star_conversation((data.get("id") or "").strip(), bool(data.get("starred")))
            self._json({"ok": ok}); return
        if path == "/api/stop":
            try:
                STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
                STOP_FILE.write_text(str(time.time()))   # chat_responder sees it → kills the in-flight turn
            except OSError:
                pass
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
    _migrate_legacy()
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
