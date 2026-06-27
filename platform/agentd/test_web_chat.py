#!/usr/bin/env python3
"""Unit test for web_chat reply ROUTING — the cross-reply fix.

A reply must go to the conversation the responder TAGGED (the state/chat-reply.cid sidecar), not to the
FIFO front (_pending.pop(0)) — the old behaviour crossed replies when turns finished out of send-order
(e.g. a 240s timeout in one conversation while another answered first). Falls back to the FIFO when the
sidecar is absent/empty (older responder / proactive reply).

Pure-ish: redirects web_chat's path globals to a temp tree; no server, no network.
Run: python3 test_web_chat.py
"""
import json
import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import web_chat as W

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"ok  {name}")
    else:
        failed += 1
        print(f"XX  {name}")


def _arm(state, content, cid_sidecar):
    """Point web_chat at the temp tree, set the sidecar, and write a fresh reply (new mtime)."""
    (state / "chat-reply.cid").write_text(cid_sidecar)
    W._last_reply_mtime[0] = None              # force check_new_reply to treat it as changed
    W.REPLY_FILE.write_text(content)


def main():
    td = pathlib.Path(tempfile.mkdtemp(prefix="wc-test-"))
    state = td / "state"
    chat = state / "chat"
    chat.mkdir(parents=True)
    W.REPLY_FILE = state / "chat-reply.md"
    W.CHAT_DIR = chat
    W.INDEX = chat / "index.json"               # _save_index target (also a module-level path global)

    # out-of-order: two conversations pending; the responder tagged the reply for the SECOND
    W._pending.clear()
    W._pending.extend(["c111", "c222"])
    _arm(state, "the answer", "c222")
    text, cid = W.check_new_reply()
    check("routes to the SIDECAR conversation, not the FIFO front", cid == "c222")
    check("returns the reply text", text == "the answer")
    check("drains the routed conv from the FIFO", "c222" not in W._pending)
    check("leaves the other pending conv intact", "c111" in W._pending)
    check("clears the reply file after consuming", W.REPLY_FILE.read_text() == "")
    check("appended the reply to the routed conversation file",
          json.loads((chat / "c222.jsonl").read_text().splitlines()[-1])["text"] == "the answer")

    # legacy fallback: empty sidecar → FIFO front
    W._pending.clear()
    W._pending.extend(["c333", "c444"])
    _arm(state, "legacy reply", "")
    _, cid = W.check_new_reply()
    check("empty sidecar falls back to the FIFO front", cid == "c333")
    check("FIFO front drained on fallback", "c333" not in W._pending)

    print(f"\n{'OK' if not failed else 'FAIL'} {passed} passed, {failed} failed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
