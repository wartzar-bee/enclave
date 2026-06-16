#!/usr/bin/env python3
"""
telegram_relay.py — bidirectional Telegram ↔ agent inbox bridge.

Runs as a sidecar alongside an agent container. No dependencies beyond stdlib.

Inbound:  Telegram message → append to agent's inbox.md (triggers agentloop wake)
Outbound: agent writes state/chat-reply.md → relay reads + sends to Telegram + clears file

The agent is instructed (via CLAUDE.md) to write replies destined for the human
operator to state/chat-reply.md. This file is the outbound channel.

Usage:
  python3 telegram_relay.py
  (or via docker-compose as a sidecar sharing the agent-data volume)

Env (required):
  TELEGRAM_BOT_TOKEN  — from @BotFather
  AGENT_DIR           — path to the mounted agent data directory (default: /agent)

Env (optional):
  TELEGRAM_CHAT_ID    — pre-authorized chat ID (if unset, accept first message then lock to it)
  RELAY_POLL_SECONDS  — Telegram long-poll timeout (default: 30)
  RELAY_CHECK_SECONDS — How often to check the outbox (default: 2)
  RELAY_ADMIN_IDS     — comma-separated Telegram user IDs allowed to message the agent
"""
import os, sys, json, time, pathlib, urllib.request, urllib.error

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
if not BOT_TOKEN:
    sys.exit("TELEGRAM_BOT_TOKEN is required")

AGENT_DIR = pathlib.Path(os.environ.get("AGENT_DIR", "/agent"))
REPLY_FILE = AGENT_DIR / "state" / "chat-reply.md"
CHAT_ID_FILE = AGENT_DIR / "state" / "telegram-chat-id"
INBOX = AGENT_DIR / "inbox.md"

POLL_TIMEOUT = int(os.environ.get("RELAY_POLL_SECONDS", "30"))
CHECK_INTERVAL = float(os.environ.get("RELAY_CHECK_SECONDS", "2"))
ADMIN_IDS = set(filter(None, os.environ.get("RELAY_ADMIN_IDS", "").split(",")))

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _tg_call(method, timeout=35, **kwargs):
    data = json.dumps(kwargs).encode() if kwargs else None
    req = urllib.request.Request(
        f"{TG_API}/{method}",
        data=data, method="POST" if data else "GET",
    )
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"[relay] TG error {e.code}: {body[:200]}", flush=True)
        return {"ok": False}
    except Exception as e:
        print(f"[relay] TG call failed: {e}", flush=True)
        return {"ok": False}


def poll_updates(offset):
    url = f"{TG_API}/getUpdates?timeout={POLL_TIMEOUT}&allowed_updates=message"
    if offset is not None:
        url += f"&offset={offset}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 5) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"[relay] poll error: {e}", flush=True)
        time.sleep(5)
        return {"ok": False, "result": []}


def send_to_telegram(chat_id, text):
    if not text.strip():
        return
    # Split long messages (Telegram limit: 4096 chars)
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        result = _tg_call("sendMessage", chat_id=int(chat_id), text=chunk)
        if not result.get("ok"):
            print(f"[relay] send failed: {result}", flush=True)


def deliver_to_agent(sender_name, sender_id, text):
    ts = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
    entry = (
        f"\n\n---\n"
        f"**[Telegram from @{sender_name} (id:{sender_id}) at {ts}]**\n\n"
        f"{text}\n\n"
        f"*Reply to this message by writing your response to `state/chat-reply.md`.*\n"
    )
    try:
        INBOX.parent.mkdir(parents=True, exist_ok=True)
        with INBOX.open("a") as f:
            f.write(entry)
        print(f"[relay] delivered to inbox.md: {text[:80]}", flush=True)
    except Exception as e:
        print(f"[relay] inbox write error: {e}", flush=True)


def load_chat_id():
    env_id = os.environ.get("TELEGRAM_CHAT_ID")
    if env_id:
        return env_id
    try:
        return CHAT_ID_FILE.read_text().strip() or None
    except OSError:
        return None


def save_chat_id(chat_id):
    try:
        CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHAT_ID_FILE.write_text(str(chat_id))
    except Exception:
        pass


_last_reply_mtime = None
_last_reply_content = ""


def check_outbox(chat_id):
    global _last_reply_mtime, _last_reply_content
    try:
        mtime = REPLY_FILE.stat().st_mtime
    except OSError:
        return  # file doesn't exist yet
    if mtime == _last_reply_mtime:
        return
    _last_reply_mtime = mtime
    try:
        content = REPLY_FILE.read_text().strip()
    except OSError:
        return
    if not content or content == _last_reply_content:
        return
    _last_reply_content = content
    print(f"[relay] outbox has reply ({len(content)} chars), sending to {chat_id}", flush=True)
    send_to_telegram(chat_id, content)
    # Clear the file after sending so we don't re-send
    try:
        REPLY_FILE.write_text("")
        _last_reply_content = ""
    except OSError:
        pass


def main():
    print(f"[relay] starting — agent_dir={AGENT_DIR}, poll={POLL_TIMEOUT}s", flush=True)

    # Verify the bot token works
    me = _tg_call("getMe")
    if not me.get("ok"):
        sys.exit(f"[relay] getMe failed — check TELEGRAM_BOT_TOKEN: {me}")
    bot_name = me["result"]["username"]
    print(f"[relay] connected as @{bot_name}", flush=True)

    chat_id = load_chat_id()
    if chat_id:
        print(f"[relay] using stored chat_id={chat_id}", flush=True)

    offset = None
    last_check = time.time()

    while True:
        # Poll Telegram for new messages
        updates = poll_updates(offset)
        if updates.get("ok"):
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                if not msg:
                    continue

                sender_id = str(msg["chat"]["id"])
                sender_name = msg.get("from", {}).get("username") or msg.get("from", {}).get("first_name", "user")
                text = msg.get("text", "").strip()

                if not text:
                    continue

                # Access control: if ADMIN_IDS set, only accept from those IDs
                if ADMIN_IDS and sender_id not in ADMIN_IDS:
                    print(f"[relay] rejecting message from unauthorized id={sender_id}", flush=True)
                    _tg_call("sendMessage", chat_id=int(sender_id),
                             text="Unauthorized. This agent only accepts messages from its authorized operators.")
                    continue

                # Lock to first sender if no chat_id set
                if not chat_id:
                    chat_id = sender_id
                    save_chat_id(chat_id)
                    print(f"[relay] locked to chat_id={chat_id} (@{sender_name})", flush=True)
                elif sender_id != chat_id and sender_id not in ADMIN_IDS:
                    print(f"[relay] ignoring message from non-primary sender {sender_id}", flush=True)
                    continue

                print(f"[relay] @{sender_name}: {text[:100]}", flush=True)
                deliver_to_agent(sender_name, sender_id, text)

                # Acknowledge receipt
                _tg_call("sendMessage", chat_id=int(sender_id),
                         text=f"⚡ Received. Working on it...")

        # Check outbox at regular intervals (not only after inbound messages)
        now = time.time()
        if chat_id and now - last_check >= CHECK_INTERVAL:
            check_outbox(chat_id)
            last_check = now


if __name__ == "__main__":
    main()
