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
import os, sys, json, time, re, pathlib, subprocess, threading, urllib.request, urllib.error

POLL_SECS = 1.5
HISTORY_CTX = 12  # recent turns of THIS conversation to include for context
_SAFE_ID = re.compile(r"^c[0-9]+$")
ERR = "⚠️ "   # prefix marking a reply that is a surfaced error (not a real answer; never titled)


def _read_jsonl(p):
    try:
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def _run_interruptible(cmd, cwd, timeout, stop_file, log):
    """Run a subprocess, but KILL it if `stop_file` appears (operator hit Stop) or timeout elapses.
    Returns (returncode, stdout, stderr) on completion, "STOPPED" if cancelled, None on timeout/spawn-fail."""
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        log(f"chat spawn failed: {e}"); return None
    deadline = time.time() + timeout
    while True:
        try:
            out, err = p.communicate(timeout=0.5)
            return (p.returncode, out, err)
        except subprocess.TimeoutExpired:
            if stop_file.exists() or time.time() > deadline:
                stopped = stop_file.exists()
                p.kill()
                try: p.communicate(timeout=5)
                except Exception: pass
                if stopped:
                    log("chat turn stopped by operator"); return "STOPPED"
                log("chat turn timed out"); return None


def _chat_model(agent_dir, brain):
    """Pick the chat model: an explicit UI pick (state/model.override) WINS, else CHAT_MODEL (the
    configured chat default), else the agent's own MODEL. So a CHAT_MODEL default (e.g. snappy Sonnet)
    still lets the operator switch models from the picker."""
    try:
        ov = (agent_dir / "state" / "model.override").read_text().strip().splitlines()
        if ov and ov[0].strip():
            return ov[0].strip()
    except Exception:
        pass
    m = os.environ.get("CHAT_MODEL", "").strip()
    if m:
        return m
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
    "queries — to actually do what's asked. Be concise; output only your reply (shown directly in chat).\n\n"
    "CAPTURE CORRECTIONS AUTOMATICALLY — do not wait to be asked, do not let them evaporate. When the "
    "operator corrects you, teaches you a fact, or states a lasting preference/decision, persist it to "
    "your DURABLE memory vault (use your memory tools — memory.py / wiki.py, exactly as your CLAUDE.md "
    "documents — and LINK it into the knowledge graph; an unlinked note is an orphan). Before saving, "
    "VERIFY where you can (your own knowledge via qmd/wiki, files in /work, read-only backoffice "
    "queries), and stamp every saved item with an explicit CONFIDENCE tag + its provenance "
    "(write the tag into the note, e.g. 'confidence=verified; source=…; date=<date>'). The ladder:\n"
    "  • confidence=unverified — operator asserted it and you could NOT check it → save ATTRIBUTED + "
    "provisional ('Operator stated (UNVERIFIED <date>): … — VERIFY'). Never bank it as plain truth.\n"
    "  • confidence=plausible — consistent with what you already know but not independently confirmed.\n"
    "  • confidence=verified — confirmed against ONE reliable source (cite it).\n"
    "  • confidence=strongly-verified — confirmed against MULTIPLE independent or authoritative sources "
    "(cite them); the 'beyond doubt' tier. (Maps to the studio's evidence grades D→C→B→A.)\n"
    "Saved facts can be RE-GRADED later as evidence arrives — promote an unverified note to verified once "
    "you confirm it, demote/strike one the operator overrides. If a correction CONTRADICTS what you hold, "
    "the operator wins for their own domain: save it as the new truth and note what it supersedes (don't "
    "silently keep the old fact). Then tell the operator in ONE line what you saved and at which "
    "confidence, so they can confirm or bump it. Only capture things with LASTING value — real facts, "
    "corrections, preferences, decisions — never chit-chat or one-off task steps.")


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
    stop_file = agent_dir / "state" / "chat-stop"

    def run(sid):
        cmd = base + (["--resume", sid, "-p", turn] if sid else ["-p", _seed_prompt(agent_dir, conv_id, turn)])
        return _run_interruptible(cmd, str(agent_dir), timeout, stop_file, log)

    sid = (sf.read_text().strip() or None) if (sf and sf.exists()) else None
    r = run(sid)
    if r == "STOPPED":
        return "STOPPED"
    if isinstance(r, tuple) and r[0] != 0 and sid:              # resume failed → drop pointer, retry fresh
        err = (r[2] or r[1] or "")
        log(f"chat resume failed (rc={r[0]}): {err[:160]} — starting a fresh session")
        try:
            if sf: sf.unlink()
        except OSError:
            pass
        r = run(None)                                           # retry fresh in the SAME turn (seeded w/ history)
        if r == "STOPPED":
            return "STOPPED"
    if not isinstance(r, tuple):                                # timeout / spawn-fail
        return None
    rc, out, err = r
    if rc != 0:
        low = ((err or out) or "").strip()
        log(f"chat turn failed (rc={rc}): {low[:200]}")
        ll = low.lower()
        if "model" in ll and ("not exist" in ll or "may not" in ll or "access to it" in ll):
            return ERR + (f"Model `{model}` isn't available (it may not exist, or this token lacks "
                          f"access). Pick a valid model from the dropdown at the top of the chat.")
        return ERR + ("The agent couldn't complete that turn (exit "
                      f"{rc}). " + (low[:200] or "Check `enclave logs` for details."))
    out = (out or "").strip()
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
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode(errors="ignore")[:200]
        except Exception:
            pass
        log(f"chat turn (api) failed: HTTP {e.code} {body}")
        return ERR + f"Model endpoint returned HTTP {e.code} for `{model}`. {body or 'Check the pool config / key.'}"
    except Exception as e:
        log(f"chat turn (api) failed: {e}")
        return ERR + f"Couldn't reach the model endpoint for `{model}` ({e}). Check the pool base URL / key."


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
                try:
                    (agent_dir / "state" / "chat-stop").unlink()   # clear any prior Stop before this turn
                except OSError:
                    pass
                with lock:
                    if brain == "claude":
                        reply = _answer_claude(agent_dir, conv_id, text, images, model, timeout, log)
                    else:
                        reply = _answer_api(_api_prompt(agent_dir, conv_id, text, images), model, timeout, log)
                if reply == "STOPPED":
                    reply_file.write_text("_(stopped)_")
                elif reply:
                    reply_file.write_text(reply)
                    log(f"chat reply sent ({len(reply)} chars)")
                    if first and not reply.startswith(ERR):   # don't title a conversation off an error message
                        ti = _gen_title(agent_dir, brain, text, timeout, log)
                        if ti:
                            _set_title(agent_dir, conv_id, ti)
                            log(f"chat titled: {ti}")
                else:
                    reply_file.write_text("⚠️ No reply — the turn timed out or the agent failed to start. "
                                          "See `enclave logs`; try again.")
        except Exception as e:
            log(f"chat loop error: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    chat_loop(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent"))
