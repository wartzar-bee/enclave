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

The chat turn is tool-capable for BRAIN=claude (it can read files + query qmd to answer from the
agent's knowledge), guard-protected by the agent's .claude/settings.json. For BRAIN=api/local it
falls back to a single-shot completion against the configured endpoint (no tools).

Env:
  CHAT_RESPONDER=off     disable (agentloop checks this before importing)
  CHAT_MODEL             model for chat turns (default: claude-haiku-4-5 for claude; else BRAIN_MODEL)
  CHAT_TURN_TIMEOUT      seconds per chat turn (default 150)
"""
import os, sys, json, time, pathlib, subprocess, threading, urllib.request

POLL_SECS = 1.5
HISTORY_CTX = 6  # recent turns of context to include


def _read_jsonl(p):
    try:
        return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def _chat_model(brain):
    m = os.environ.get("CHAT_MODEL", "").strip()
    if m:
        return m
    if brain == "claude":
        return "claude-haiku-4-5"          # fast + cheap for interactive Q&A
    return os.environ.get("BRAIN_MODEL", "").strip() or "deepseek/deepseek-chat"


def _recent_context(agent_dir):
    """A little live state so the responder answers grounded, not blind."""
    bits = []
    for rel in ("state/rollup.md", "state/recall.md"):
        f = agent_dir / rel
        if f.exists():
            t = f.read_text(errors="ignore").strip()
            if t:
                bits.append(f"## {rel}\n{t[:1500]}")
    hist = _read_jsonl(agent_dir / "state" / "chat-history.jsonl")[-HISTORY_CTX:]
    if hist:
        convo = "\n".join(f"{h.get('role','?')}: {h.get('text','')[:500]}" for h in hist)
        bits.append("## recent conversation\n" + convo)
    return "\n\n".join(bits)


def _build_prompt(agent_dir, msg, images):
    ctx = _recent_context(agent_dir)
    parts = []
    if ctx:
        parts.append("Context (your current state — for grounding, do not quote verbatim):\n" + ctx)
    if images:
        parts.append("The user attached image(s); read them with the Read tool:\n" +
                     "\n".join(f"- {p}" for p in images))
    parts.append(
        "Answer the user's chat message below. You may read your files and query qmd to ground the "
        "answer in your knowledge. Be concise and lead with the answer. Output ONLY your reply text "
        "(it is shown directly in a chat UI).")
    parts.append("\nUser: " + msg)
    return "\n\n".join(parts)


def _answer_claude(agent_dir, prompt, model, timeout, log):
    """Tool-capable chat turn via the claude CLI. cwd=agent_dir → CLAUDE.md + .claude/settings.json
    (guard hook) + .mcp.json (qmd) are auto-loaded. Guard still fires under skip-permissions."""
    cmd = ["claude", "-p", prompt, "--model", model, "--dangerously-skip-permissions"]
    try:
        r = subprocess.run(cmd, cwd=str(agent_dir), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        # CLI error (e.g. not logged in / cap) — log it, don't surface the raw text as the "reply".
        log(f"chat turn failed (rc={r.returncode}): {(out or r.stderr or '')[:200]}")
        return None
    return out or None


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


def chat_loop(agent_dir, log=print):
    agent_dir = pathlib.Path(agent_dir)
    chat_inbox = agent_dir / "state" / "chat-inbox.jsonl"
    reply_file = agent_dir / "state" / "chat-reply.md"
    chat_inbox.parent.mkdir(parents=True, exist_ok=True)
    brain = os.environ.get("BRAIN", "claude")
    model = _chat_model(brain)
    timeout = int(os.environ.get("CHAT_TURN_TIMEOUT", "150"))
    lock = threading.Lock()
    # Baseline at EOF so a restart doesn't replay the whole backlog.
    seen = len(_read_jsonl(chat_inbox))
    log(f"chat responder up (plane=state/chat-inbox.jsonl, brain={brain}, model={model})")

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
                prompt = _build_prompt(agent_dir, text, images)
                with lock:
                    if brain == "claude":
                        reply = _answer_claude(agent_dir, prompt, model, timeout, log)
                    else:
                        reply = _answer_api(prompt, model, timeout, log)
                if reply:
                    reply_file.write_text(reply)
                    log(f"chat reply sent ({len(reply)} chars)")
                else:
                    reply_file.write_text("(couldn't generate a reply just now — please try again)")
        except Exception as e:
            log(f"chat loop error: {e}")
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    chat_loop(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent"))
