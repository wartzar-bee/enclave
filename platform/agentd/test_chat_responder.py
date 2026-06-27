#!/usr/bin/env python3
"""Unit tests for chat_responder.py — the Enclave real-time chat plane.

Guards the two regressions that broke chat TWICE recently (from git log):
  (a) CHAT_SYSTEM referenced-but-undefined -> NameError on every reply (fixed b956dd4).
  (b) the api-key lookup was hardcoded to OPENROUTER_API_KEY -> BRAIN_API_KEY_ENV (NVIDIA) gave 500
      (now honored via _openrouter_key).
  (c) BRAIN=api/local chat must inject the agent's CLAUDE.md into the system prompt + stop fabricating
      confidence tags on the reply (064ca55).
Plus the pure helpers (jsonl read, tool-brief, model pick, history, title clean, first-turn, _SAFE_ID).

Hermetic — builds a temp fleet, never touches the network (no LLM call is invoked). Run:
    python3 test_chat_responder.py
"""
import json
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F
import chat_responder as C

check = F.Check()

# env vars these tests poke; cleared before each env-sensitive block for determinism
_TEST_KEYS = ["MODEL", "CHAT_MODEL", "BRAIN_MODEL", "BRAIN_API_KEY_ENV", "OPENROUTER_API_KEY",
              "LOCAL_BRAIN_KEY", "NVIDIA_API_KEY", "AGENT_DIR", "TOOLS_ROOT", "BRAIN_API_BASE",
              "BRAIN", "LOCAL_BRAIN_BASE", "LOCAL_BRAIN_MODEL", "CHAT_BASE", "CHAT_ALLOW_WRITES"]


def _clear_env():
    for k in _TEST_KEYS:
        os.environ.pop(k, None)


def main():
    root = F.build_fleet(specs=[{"id": "alpha"}, {"id": "beta", "brain": "api", "model": "qwen"}])
    rootp = pathlib.Path(root)
    home = rootp / "alpha" / "home"          # the agent_dir chat_responder operates on (== /agent)
    charter = ("# CLAUDE.md\nYou are ALPHA, the build agent. MISSION: SENTINEL-MARKER-9X. "
               "Never fabricate metrics.\n")
    (home / "CLAUDE.md").write_text(charter)

    # ---------------------------------------------------------------- bug (a): CHAT_SYSTEM defined
    check("CHAT_SYSTEM is defined at module level", hasattr(C, "CHAT_SYSTEM"))
    check("CHAT_SYSTEM is a non-empty str",
          isinstance(getattr(C, "CHAT_SYSTEM", None), str) and len(C.CHAT_SYSTEM.strip()) > 0)
    check("CHAT_PREAMBLE is a non-empty str",
          isinstance(getattr(C, "CHAT_PREAMBLE", None), str) and len(C.CHAT_PREAMBLE.strip()) > 0)
    # the exact reference in _answer_claude's cmd list must resolve (this is what NameError'd)
    check("CHAT_SYSTEM mentions it's a live chat (not a work tick)",
          "live" in C.CHAT_SYSTEM.lower() and "chat" in C.CHAT_SYSTEM.lower())

    # ---------------------------------------------------------------- bug (c): no fabricated tags on reply
    # the preamble must instruct: confidence tag belongs only in a saved note, NEVER on the chat reply
    pre = C.CHAT_PREAMBLE.lower()
    check("CHAT_PREAMBLE forbids appending a confidence tag to the reply",
          "never" in pre and "confidence" in pre and "reply" in pre)

    # ---------------------------------------------------------------- bug (b): _openrouter_key resolution
    _clear_env()
    # 1) BRAIN_API_KEY_ENV names NVIDIA_API_KEY and that var is present -> return it
    os.environ["BRAIN_API_KEY_ENV"] = "NVIDIA_API_KEY"
    os.environ["NVIDIA_API_KEY"] = "nv-secret-123"
    check.eq("_openrouter_key honors BRAIN_API_KEY_ENV (NVIDIA)", C._openrouter_key(), "nv-secret-123")

    # 2) fallback: BRAIN_API_KEY_ENV set but its var absent -> falls back to OPENROUTER_API_KEY in env
    _clear_env()
    os.environ["BRAIN_API_KEY_ENV"] = "NVIDIA_API_KEY"   # no NVIDIA_API_KEY present
    os.environ["OPENROUTER_API_KEY"] = "or-fallback-456"
    check.eq("_openrouter_key falls back to OPENROUTER_API_KEY", C._openrouter_key(), "or-fallback-456")

    # 3) default key var: no BRAIN_API_KEY_ENV -> defaults to OPENROUTER_API_KEY
    _clear_env()
    os.environ["OPENROUTER_API_KEY"] = "or-default-789"
    check.eq("_openrouter_key default var is OPENROUTER_API_KEY", C._openrouter_key(), "or-default-789")

    # 4) nothing in env -> "" (no key found)
    _clear_env()
    os.environ["AGENT_DIR"] = str(rootp / "no-such-dir")
    os.environ["TOOLS_ROOT"] = str(rootp / "no-such-dir2")
    check.eq("_openrouter_key returns '' when nothing set", C._openrouter_key(), "")

    # 5) file fallback: scan AGENT_DIR/.secrets/*.env for the configured var name
    _clear_env()
    sdir = home / ".secrets"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "nvidia.env").write_text('# header\nNVIDIA_API_KEY="nv-from-file-abc"\nOTHER=1\n')
    os.environ["BRAIN_API_KEY_ENV"] = "NVIDIA_API_KEY"
    os.environ["AGENT_DIR"] = str(home)
    check.eq("_openrouter_key file-fallback reads .secrets/*.env", C._openrouter_key(), "nv-from-file-abc")
    _clear_env()

    # ---------------------------------------------------------------- bug (c): CLAUDE.md injected
    md = C._agent_claude_md(home)
    check("_agent_claude_md reads the agent's CLAUDE.md", "SENTINEL-MARKER-9X" in md)
    check("_agent_claude_md caps length", len(C._agent_claude_md(home)) <= 16000)
    check("_agent_claude_md missing file -> ''", C._agent_claude_md(rootp / "nope") == "")

    prompt = C._api_prompt(home, "", "what is your mission?", [])
    check("_api_prompt injects CLAUDE.md charter text", "SENTINEL-MARKER-9X" in prompt)
    check("_api_prompt includes the chat preamble", "LIVE" in prompt or "live" in prompt)
    check("_api_prompt appends the user message", prompt.rstrip().endswith("what is your mission?"))
    check("_api_prompt labels the operating-instructions block",
          "OPERATING INSTRUCTIONS" in prompt and "no-fabrication" in prompt)
    # images path is reflected
    pimg = C._api_prompt(home, "", "see this", ["/work/a.png"])
    check("_api_prompt lists attached images", "/work/a.png" in pimg)

    # ---------------------------------------------------------------- _read_jsonl
    jf = home / "state" / "sample.jsonl"
    jf.write_text(json.dumps({"a": 1}) + "\n\n" + json.dumps({"b": 2}) + "\n")
    rows = C._read_jsonl(jf)
    check.eq("_read_jsonl parses + skips blanks", rows, [{"a": 1}, {"b": 2}])
    check.eq("_read_jsonl missing file -> []", C._read_jsonl(home / "state" / "missing.jsonl"), [])

    # ---------------------------------------------------------------- _tool_brief
    check.eq("_tool_brief Bash -> command", C._tool_brief("Bash", {"command": "ls -la"}), "ls -la")
    check.eq("_tool_brief Read -> file_path", C._tool_brief("Read", {"file_path": "/work/x.py"}), "/work/x.py")
    check.eq("_tool_brief Grep -> pattern", C._tool_brief("Grep", {"pattern": "foo"}), "foo")
    check.eq("_tool_brief WebFetch -> url", C._tool_brief("WebFetch", {"url": "http://x"}), "http://x")
    check.eq("_tool_brief mcp -> query", C._tool_brief("mcp__qmd__query", {"query": "slot math"}), "slot math")
    check.eq("_tool_brief empty -> ''", C._tool_brief(None, None), "")
    check("_tool_brief Bash truncates to 100", len(C._tool_brief("Bash", {"command": "x" * 500})) == 100)

    # ---------------------------------------------------------------- _chat_model
    _clear_env()
    check.eq("_chat_model claude default", C._chat_model(home, "claude"), "claude-sonnet-4-6")
    os.environ["MODEL"] = "claude-opus-4-8"
    check.eq("_chat_model claude honors MODEL", C._chat_model(home, "claude"), "claude-opus-4-8")
    os.environ["CHAT_MODEL"] = "claude-haiku-4-5"
    check.eq("_chat_model CHAT_MODEL beats MODEL", C._chat_model(home, "claude"), "claude-haiku-4-5")
    _clear_env()
    check.eq("_chat_model api default", C._chat_model(home, "api"), "deepseek/deepseek-chat")
    os.environ["BRAIN_MODEL"] = "qwen/qwen3-next-80b"
    check.eq("_chat_model api honors BRAIN_MODEL", C._chat_model(home, "api"), "qwen/qwen3-next-80b")
    _clear_env()
    # model.override file wins over everything
    (home / "state" / "model.override").write_text("override/model-1\n")
    os.environ["CHAT_MODEL"] = "ignored"
    check.eq("_chat_model model.override wins", C._chat_model(home, "claude"), "override/model-1")
    (home / "state" / "model.override").unlink()
    _clear_env()

    # ---------------------------------------------------------------- _conv_history
    chatdir = home / "state" / "chat"
    chatdir.mkdir(parents=True, exist_ok=True)
    (chatdir / "c7.jsonl").write_text(
        json.dumps({"role": "user", "text": "hi"}) + "\n" +
        json.dumps({"role": "assistant", "text": "hello"}) + "\n" +
        json.dumps({"role": "user", "text": "just-arrived"}) + "\n")
    hist = C._conv_history(home, "c7")
    check("_conv_history drops the just-arrived user msg",
          [h["text"] for h in hist] == ["hi", "hello"])
    check("_conv_history bad conv id -> falls back (no crash)",
          isinstance(C._conv_history(home, "Bad Id"), list))

    # ---------------------------------------------------------------- _clean_title
    check.eq("_clean_title strips quotes/punct/space",
             C._clean_title('  "Slot math review."  '), "Slot math review")
    check.eq("_clean_title empty -> None", C._clean_title("   "), None)
    check("_clean_title caps at 60 chars", len(C._clean_title("word " * 40)) <= 60)

    # ---------------------------------------------------------------- _is_first_turn + _SAFE_ID
    (chatdir / "c9.jsonl").write_text(json.dumps({"role": "user", "text": "first"}) + "\n")
    check("_is_first_turn True with one msg", C._is_first_turn(home, "c9") is True)
    check("_is_first_turn False with two msgs", C._is_first_turn(home, "c7") is False)
    check("_is_first_turn False for bad conv id", C._is_first_turn(home, "nope") is False)
    check("_SAFE_ID accepts c123", bool(C._SAFE_ID.match("c123")))
    check("_SAFE_ID rejects abc", not C._SAFE_ID.match("abc"))
    check("_SAFE_ID rejects c12a", not C._SAFE_ID.match("c12a"))

    # ---------------------------------------------------------------- _write_reply: conv-id sidecar (cross-reply fix)
    _clear_env()
    rf = home / "state" / "chat-reply.md"
    C._write_reply(rf, "c424242", "hello world")
    check.eq("_write_reply writes the reply text", rf.read_text(), "hello world")
    check.eq("_write_reply writes the conversation-id sidecar",
             (home / "state" / "chat-reply.cid").read_text().strip(), "c424242")
    C._write_reply(rf, None, "no conv")
    check.eq("_write_reply tolerates a None conv id (empty sidecar)",
             (home / "state" / "chat-reply.cid").read_text().strip(), "")

    # ---------------------------------------------------------------- _chat_model: a local-brain agent chats local
    _clear_env()
    ml = C._chat_model(home, "local").lower()
    check("_chat_model local default is a local MLX id (not a cloud default)", "mlx" in ml or "qwen" in ml)
    os.environ["LOCAL_BRAIN_MODEL"] = "my/local-model"
    check.eq("_chat_model local honors LOCAL_BRAIN_MODEL", C._chat_model(home, "local"), "my/local-model")
    _clear_env()

    # ---------------------------------------------------------------- _disallowed_tools: read-only chat by default
    _clear_env()
    dt = C._disallowed_tools()
    check("chat blocks AskUserQuestion (interactive → stalls)", "AskUserQuestion" in dt)
    check("chat blocks Bash by default (no off-building)", "Bash" in dt)
    check("chat blocks Write+Edit by default", "Write" in dt and "Edit" in dt)
    os.environ["CHAT_ALLOW_WRITES"] = "1"
    dt2 = C._disallowed_tools()
    check("CHAT_ALLOW_WRITES=1 re-enables building tools", "Bash" not in dt2 and "Write" not in dt2)
    check("CHAT_ALLOW_WRITES still blocks AskUserQuestion", "AskUserQuestion" in dt2)
    _clear_env()
    check("CHAT_SYSTEM steers to read-only, answer-not-build",
          "read-only" in C.CHAT_SYSTEM.lower() and "build" in C.CHAT_SYSTEM.lower())

    # ---------------------------------------------------------------- _answer_api: BRAIN=local hits the LOCAL endpoint (no 401)
    _clear_env()
    os.environ["BRAIN"] = "local"
    os.environ["LOCAL_BRAIN_BASE"] = "http://host.docker.internal:8081/v1"
    os.environ["LOCAL_BRAIN_KEY"] = "mlx"
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def _fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        return _Resp()

    _orig = C.urllib.request.urlopen
    C.urllib.request.urlopen = _fake_urlopen
    try:
        out = C._answer_api("hi", "local-model", 5, lambda *a: None)
    finally:
        C.urllib.request.urlopen = _orig
    check("_answer_api local hits the LOCAL base (not OpenRouter)", "8081" in captured.get("url", ""))
    check("_answer_api local sends a bearer token (no missing-auth 401)", bool(captured.get("auth")))
    check.eq("_answer_api returns the model content", out, "ok")
    _clear_env()

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
