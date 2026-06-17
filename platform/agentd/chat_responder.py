#!/usr/bin/env python3
"""
chat_responder.py — the REAL-TIME chat plane for an Enclave agent.

agentloop.py spawns `chat_loop(agent_dir, log=...)` in a daemon thread. It watches a SEPARATE
chat inbox (`state/chat-inbox.jsonl`, written by web_chat) and answers each message CONCURRENTLY
with the work tick — so a long (≤40-min) autonomous task never blocks a chat reply. The reply is
written to `state/chat-reply.md`, which the web chat polls.

Two planes, on purpose:
  • work plane  — inbox.md + tick.txt (scheduled/▸directive autonomous work; serialized; `enclave send`)
  • chat plane  — state/chat-inbox.jsonl (interactive Q&A; this module; concurrent; the web chat)

For BRAIN=claude each conversation is a CONTINUOUS, RESUMABLE Claude Code session (one per web-chat
thread): the first message starts a session, later messages `--resume` it — so the full thread (text
AND tool calls) is real native context, exactly like the CLI conversation, just a different UI. It runs
at the agent's own model (not a downgraded side-model) and is fully tool-capable (qmd, file read/write
in /work, read-only backoffice queries), guard-protected by .claude/settings.json. For BRAIN=api/local
it falls back to a single-shot completion with the recent thread replayed as text (no native session).

Env:
  CHAT_RESPONDER=off     disable (agentloop checks this before importing)
  CHAT_MODEL             override the chat model (default: the UI picker / the agent's MODEL — same as the agent)
  CHAT_TURN_TIMEOUT      seconds per chat turn (default 150)
"""
import os, sys, json, time, re, pathlib, subprocess, threading, urllib.request

POLL_SECS = 1.5
HISTORY_CTX = 12  # recent turns of THIS conversation to include for context
_SAFE_ID = re.compile(r"^c[0-9]+$")


def _read_jsonl(p):
    try:
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def _chat_model(agent_dir, brain):
    """Run the chat at the SAME capability as the agent — honor an explicit CHAT_MODEL, else the UI model
    picker (state/model.override), else the agent's own MODEL. No downgraded side-model."""
    m = os.environ.get("CHAT_MODEL", "").strip()
    if m:
        return m
    try:
        ov = (agent_dir / "state" / "model.override").read_text().strip().splitlines()
        if ov and ov[0].strip():
            return ov[0].strip()
    except Exception:
        pass
    if brain == "claude":
        return os.environ.get("MODEL", "").strip() or "claude-sonnet-4-6"
    return os.environ.get("BRAIN_MODEL", "").strip() or "deepseek/deepseek-chat"


# Established once on the first turn; session resume carries it across the whole thread.
CHAT_PREAMBLE = (
    "You are in a LIVE, CONTINUOUS chat with the operator through a web UI — treat it EXACTLY like an "
    "interactive Claude Code conversation. Do NOT read inbox.md or follow the per-tick 'no new message' "
    "protocol (that's for autonomous work ticks, not this). Converse naturally, REMEMBER everything said "
    "earlier in this thread (e.g. 'try again' refers to the previous request), and use your full "
    "tools/skills/knowledge — qmd search, reading/writing files in /work, and read-only backoffice "
    "queries — to actually do what's asked. Be concise; output only your reply (shown directly in chat).")


def _conv_history(agent_dir, conv_id):
    """Recent turns of THIS conversation — only for the api/local fallback (no native session). The
    claude brain uses real session resume instead, so it gets the full thread + tool context."""
    if conv_id and _SAFE_ID.match(conv_id):
        hist = _read_jsonl(agent_dir / "state" / "chat" / (conv_id + ".jsonl"))
        if hist:
            hist = hist[:-1]            # drop the just-arrived user msg (added separately below)
    else:
        hist = _read_jsonl(agent_dir / "state" / "chat-history.jsonl")
    return hist[-HISTORY_CTX:]


def _api_prompt(agent_dir, conv_id, msg, images):
    parts = [CHAT_PREAMBLE]
    hist = _conv_history(agent_dir, conv_id)
    if hist:
        parts.append("## conversation so far\n" +
                     "\n".join(f"{h.get('role','?')}: {h.get('text','')[:800]}" for h in hist))
    if images:
        parts.append("User attached image(s):\n" + "\n".join(f"- {p}" for p in images))
    parts.append("User: " + msg)
    return "\n\n".join(parts)


def _seed_prompt(agent_dir, conv_id, turn):
    """First-turn prompt: the chat-mode preamble + any prior thread text (so context SURVIVES a fresh
    session — e.g. after an image rebuild wiped the container's claude sessions)."""
    seed = CHAT_PREAMBLE
    hist = _conv_history(agent_dir, conv_id)
    if hist:
        seed += "\n\n## conversation so far\n" + "\n".join(
            f"{h.get('role','?')}: {h.get('text','')[:800]}" for h in hist)
    return seed + "\n\nUser: " + turn

def _answer_claude(agent_dir, conv_id, msg, images, model, timeout, log):
    """One turn of a CONTINUOUS Claude Code session per conversation. First message starts a session (id
    saved to state/chat/<id>.session); later messages `--resume` it, so the FULL thread — text AND tool
    calls — is native context, like the CLI, just a different UI. cwd=agent_dir auto-loads CLAUDE.md +
    .claude/settings.json (guard) + .mcp.json (qmd). If a saved session is gone (container rebuild wipes
    ~/.claude), we RETRY fresh in the same turn — seeded with the thread — so the user never sees a failure."""
    sf = (agent_dir / "state" / "chat" / (conv_id + ".session")) if (conv_id and _SAFE_ID.match(conv_id)) else None
    turn = msg
    if images:
        turn = "User attached image(s); read them with the Read tool:\n" + "\n".join(f"- {p}" for p in images) + "\n\n" + msg
    base = ["claude", "--model", model, "--dangerously-skip-permissions", "--output-format", "json"]

    def run(sid):
        cmd = base + (["--resume", sid, "-p", turn] if sid else ["-p", _seed_prompt(agent_dir, conv_id, turn)])
        try:
            return subprocess.run(cmd, cwd=str(agent_dir), capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            log("chat turn timed out"); return None

    sid = (sf.read_text().strip() or None) if (sf and sf.exists()) else None
    r = run(sid)
    if r is not None and r.returncode != 0 and sid:
        err = (r.stderr or r.stdout or "")
        log(f"chat resume failed (rc={r.returncode}): {err[:160]} — starting a fresh session")
        try:
            if sf: sf.unlink()                                  # stale/lost session → drop the dangling pointer
        except OSError:
            pass
        r = run(None)                                           # retry fresh in the SAME turn (seeded w/ history)
    if r is None:
        return None
    if r.returncode != 0:
        log(f"chat turn failed (rc={r.returncode}): {((r.stderr or r.stdout) or '')[:200]}")
        return None
    out = (r.stdout or "").strip()
    try:
        d = json.loads(out)
        if sf and d.get("session_id"):
            sf.write_text(d["session_id"])
        return (d.get("result") or "").strip() or None
    except Exception:
        return out or None                                      # non-json fallback


def _answer_api(prompt, model, timeout, log):
    """Single-shot completion for BRAIN=api/local (no tools)."""
    base = os.environ.get("BRAIN_API_BASE") or "https://openrouter.ai/api/v1"
    key = os.environ.get("OPENROUTER_API_KEY") or os.environ.get("LOCAL_BRAIN_KEY") or ""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}],
                       "max_tokens": int(os.environ.get("CHAT_MAX_TOKENS", "1024"))}).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.load(resp)
        return (d["choices"][0]["message"]["content"] or "").strip() or None
    except Exception as e:
        log(f"chat turn (api) failed: {e}")
        return None


def _is_first_turn(agent_dir, conv_id):
    """True if this conversation has only the just-arrived user message (no prior turns) → time to title it."""
    if not (conv_id and _SAFE_ID.match(conv_id)):
        return False
    return len(_read_jsonl(agent_dir / "state" / "chat" / (conv_id + ".jsonl"))) <= 1

def _clean_title(s):
    s = " ".join((s or "").split()).strip().strip('"').strip("'").rstrip(".")
    return s[:60] or None

def _gen_title(agent_dir, brain, user_msg, timeout, log):
    """A short topic title for the conversation (ChatGPT/Claude-style) — NOT the verbatim first message.
    Cheap one-shot; runs from a neutral cwd so it does NOT inherit the agent's work-tick CLAUDE.md."""
    p = ("Reply with ONLY a short topic title (3-6 words, no quotes, no trailing punctuation) that "
         "summarizes this request — like a chat tab title:\n\n" + (user_msg or "")[:500])
    try:
        if brain == "claude":
            r = subprocess.run(["claude", "-p", p, "--model", "claude-haiku-4-5", "--dangerously-skip-permissions"],
                               cwd="/tmp", capture_output=True, text=True, timeout=min(timeout, 60))
            return _clean_title(r.stdout) if r.returncode == 0 else None
        return _clean_title(_answer_api(p, _chat_model(agent_dir, brain), min(timeout, 60), log))
    except Exception as e:
        log(f"title gen failed: {e}"); return None

def _set_title(agent_dir, conv_id, title):
    """Write the generated topic into web_chat's conversation index (atomic; web_chat preserves it)."""
    if not title:
        return
    idx_path = agent_dir / "state" / "chat" / "index.json"
    try:
        idx = json.loads(idx_path.read_text())
    except Exception:
        return
    e = next((c for c in idx if c.get("id") == conv_id), None)
    if not e:
        return
    e["title"] = title
    try:
        tmp = idx_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(idx))
        tmp.replace(idx_path)
    except Exception:
        pass

def chat_loop(agent_dir, log=print):
    agent_dir = pathlib.Path(agent_dir)
    chat_inbox = agent_dir / "state" / "chat-inbox.jsonl"
    reply_file = agent_dir / "state" / "chat-reply.md"
    chat_inbox.parent.mkdir(parents=True, exist_ok=True)
    brain = os.environ.get("BRAIN", "claude")
    timeout = int(os.environ.get("CHAT_TURN_TIMEOUT", "150"))
    lock = threading.Lock()
    # Baseline at EOF so a restart doesn't replay the whole backlog.
    seen = len(_read_jsonl(chat_inbox))
    log(f"chat responder up (plane=state/chat-inbox.jsonl, brain={brain}, model={_chat_model(agent_dir, brain)}, sessions=per-conversation)")

    while True:
        try:
            msgs = _read_jsonl(chat_inbox)
            new = msgs[seen:]
            seen = len(msgs)
            for m in new:
                text = (m.get("text") or "").strip()
                images = m.get("images") or []
                if not text and not images:
                    continue
                conv_id = (m.get("conversation") or "").strip()
                first = _is_first_turn(agent_dir, conv_id)   # check BEFORE answering (reply gets appended later)
                model = _chat_model(agent_dir, brain)     # re-resolve so a live UI model switch is honored
                with lock:
                    if brain == "claude":
                        reply = _answer_claude(agent_dir, conv_id, text, images, model, timeout, log)
                    else:
                        reply = _answer_api(_api_prompt(agent_dir, conv_id, text, images), model, timeout, log)
                if reply:
                    reply_file.write_text(reply)
                    log(f"chat reply sent ({len(reply)} chars)")
                    if first:                              # name the conversation by topic (not the verbatim 1st msg)
                        ti = _gen_title(agent_dir, brain, text, timeout, log)
                        if ti:
                            _set_title(agent_dir, conv_id, ti)
                            log(f"chat titled: {ti}")
                else:
                    reply_file.write_text("(couldn't generate a reply just now — please try again)")
        except Exception as e:
            log(f"chat loop error: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    chat_loop(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent"))
