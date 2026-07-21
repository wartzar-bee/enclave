#!/usr/bin/env python3
"""
local_agent.py — a model-AGNOSTIC agent brain that drops into the one `claude -p` slot.

This is the piece that frees the platform from the Claude Code harness: runtime.sh runs it
instead of `claude -p` when BRAIN=local, and the SAME assembled package (CLAUDE.md system
prompt + tick.txt turn + memory.py + qmd + the scoped secrets + the guard hooks) drives it.
Everything else in platform/ is already model-agnostic; this is the only new moving part.

Design (deliberately small — we own + vet it, vs adopting a sprawling framework):
  • Brain runs LOCAL-FIRST on an OpenAI-compatible endpoint (MLX Qwen3-Coder on :8081 by
    default; any OpenAI-compatible URL via env). Pure stdlib HTTP — no SDK, no new pip dep.
  • A ReAct TEXT protocol (one fenced ```tool JSON block per step) — portable across local
    engines whose server-side function-calling is unreliable. Parse → guard → execute → observe.
  • GUARDRAILS are the same hook the Claude path uses: every tool call is screened by
    guard.py (exit 2 = block) BEFORE it runs. Claude Code's PreToolUse
    hooks don't fire here, so the executor enforces them itself — one source of truth.
  • JUDGMENT escalates to an external REASONING API (OpenRouter / xAI / OpenAI / Anthropic API)
    via the `escalate` tool — NOT the interactive Claude Code session/cap. Hybrid: when offline
    the item is queued to state/pending-judgment.md and the agent proceeds with verifiable work.

  python3 local_agent.py <agent-dir>
Env (all optional — sane defaults from tools/llm/policy.json):
  LOCAL_BRAIN_BASE  LOCAL_BRAIN_MODEL  LOCAL_BRAIN_KEY        # the local brain endpoint
  ESCALATION_BASE   ESCALATION_MODEL   ESCALATION_KEY         # the external reasoning endpoint
  GUARD_HOOK (default: guard.py; set to a custom hook path to override)
  LOCAL_MAX_STEPS(24)  LOCAL_MAX_TOKENS(6144)  LOCAL_TEMP(0.3)  LOCAL_REQ_TIMEOUT(180)
"""
import os, sys, re, json, glob as globmod, subprocess, pathlib, urllib.request, urllib.error, time

# One credential definition for the whole framework (platform/agentd/secrets.py). The local patterns
# below remain ONLY as a fallback for a deployment where that module is not on disk.
sys.path.insert(0, os.environ.get("ENCLAVE_AGENTD", "/workspace/platform/agentd"))
try:
    import secrets as _sec
except Exception:
    _sec = None


HERE = pathlib.Path(__file__).resolve().parent
# Context trim budget for the ReAct loop, in CHARS of message content (~4 chars/token). The trim
# keeps system+turn plus the newest tail that fits this budget (max 24 messages) — see the loop.
_CTX_CHARS = int(os.environ.get("LOCAL_CTX_CHARS", "60000"))
WORKSPACE = pathlib.Path(os.environ.get("TOOLS_ROOT", "/workspace"))


# ── config: resolve brain + escalation endpoints from policy.json (env overrides win) ──────
def _policy():
    for p in (WORKSPACE / "tools/llm/policy.json", HERE.parents[1] / "tools/llm/policy.json"):
        try:
            return json.loads(p.read_text())
        except Exception:
            continue
    return {}


def _secret(name, key):
    """Read KEY=val from a .secrets/<name> file (the scoped mount). '' if absent."""
    for base in (WORKSPACE / ".secrets", HERE.parents[1] / ".secrets"):
        f = base / name
        try:
            for ln in f.read_text().splitlines():
                if ln.strip().startswith(key + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


def resolve_endpoints():
    pol = _policy()
    pools = pol.get("pools", {})
    models = pol.get("models", {})
    mlx = pools.get("mlx", {})
    top = pools.get("top", {})
    brain = {
        "base": os.environ.get("LOCAL_BRAIN_BASE") or mlx.get("base_url_default", "http://host.docker.internal:8081/v1"),
        "model": os.environ.get("LOCAL_BRAIN_MODEL") or (models.get("mlx", {}) or {}).get("default", "lmstudio-community/Qwen3-Coder-Next-MLX-4bit"),
        "key": os.environ.get("LOCAL_BRAIN_KEY") or mlx.get("api_key_default", "mlx"),
    }
    esc = {
        "base": os.environ.get("ESCALATION_BASE") or top.get("base_url_default", "https://openrouter.ai/api/v1"),
        "model": os.environ.get("ESCALATION_MODEL") or (models.get("top", {}) or {}).get("reasoning", "anthropic/claude-sonnet-4.6"),
        "key": os.environ.get("ESCALATION_KEY") or _secret("openrouter.env", "OPENROUTER_API_KEY"),
    }
    return brain, esc


# ── dashboard telemetry (events.jsonl + usage.jsonl) ────────────────────────────────────────
# The Claude path emits these via Claude-Code hooks (event_log.py) + usage_capture.py. The local
# ReAct loop fires no Claude hooks, so a BRAIN=local/api agent was invisible to the dashboard. We
# emit the SAME records here so Activity/Status/Diagnostics work for every brain. All best-effort.
_USAGE_ACC = {"input": 0, "output": 0, "cost": 0.0, "had_usage": False}
_EVENT_TOOL = {"bash": "Bash", "read": "Read", "write": "Write", "edit": "Edit",
               "glob": "Glob", "grep": "Grep", "qmd": "Qmd", "escalate": "Escalate"}

# Redaction. Capturing tool OUTPUT means a command that prints a credential would write it into
# events.jsonl, which downstream tooling renders into git-tracked reports. Scrub at the source so
# the secret is never on disk in the first place: a redactor that only runs at render time leaves
# the raw value sitting in the log file. CLAUDE.md: never log a credential.
_REDACT = re.compile(
    r"(sk-[A-Za-z0-9_-]{12,}"
    r"|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|gho_[A-Za-z0-9]{16,}"
    r"|nvapi-[A-Za-z0-9_-]{16,}"
    r"|AIza[0-9A-Za-z_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN[A-Z ]*PRIVATE KEY-----"
    r"|\b[A-Za-z0-9._%+-]+:[^\s@/]{6,}@)", re.I)
_REDACT_KV = re.compile(
    r"\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
    r"(\s*[=:]\s*)(\"?[^\s\"']{6,}\"?)", re.I)


def _redact(text):
    if _sec is not None:
        return _sec.redact(text)
    return _redact_local(text)


def _redact_local(text):
    if not text:
        return text
    text = _REDACT.sub("[REDACTED]", text)
    return _REDACT_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


def _event_summary(tool, inp):
    inp = inp or {}
    if tool == "bash":
        # 140 chars truncated mid-command on almost every real invocation, so the log could show
        # THAT a command ran but not WHAT it did. Evaluation needs the whole command.
        return (inp.get("command", "") or "").strip().replace("\n", " ")[:400]
    if tool in ("write", "edit", "read"):
        return inp.get("file_path", "") or ""
    if tool in ("glob", "grep"):
        return (inp.get("pattern", "") or inp.get("query", ""))[:100]
    if tool == "qmd":
        return (inp.get("query", "") or "")[:100]
    return ""

def _emit_event(sd, rec):
    """Append one line to state/events.jsonl — the dashboard's live 'doing now' feed. Mirrors
    hooks/event_log.py for the local path. NEVER raises (telemetry must never break a tick)."""
    try:
        sd.mkdir(parents=True, exist_ok=True)
        with (sd / "events.jsonl").open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ── OpenAI-compatible chat call (stdlib only; works for MLX/Ollama/OpenRouter) ──────────────
_SPEND_LOG = pathlib.Path(os.environ.get("SPEND_LOG", "")) if os.environ.get("SPEND_LOG") else None

def chat(endpoint, messages, max_tokens, temperature, timeout, tools=None):
    """One chat/completions call. With `tools`, requests NATIVE tool-calling (N3, 2026-07-20) and
    returns (text, tool_calls); without, returns plain text (call sites unpack accordingly).
    Native extraction exists because the fenced-text protocol is where a whole failure class lives:
    heredoc/fence breakage, escaping blowups, and the model free-writing 'OBSERVATION:' fabrications
    (D-113). A structured tool_call is parsed by the API layer, not fished out of prose."""
    payload = {
        "model": endpoint["model"], "messages": messages,
        "max_tokens": max_tokens, "temperature": temperature, "stream": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = json.dumps(payload).encode()
    req = urllib.request.Request(endpoint["base"].rstrip("/") + "/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if endpoint.get("key"):
        req.add_header("Authorization", "Bearer " + endpoint["key"])
    with urllib.request.urlopen(req, timeout=timeout) as r:
        j = json.loads(r.read())
    # Accumulate this tick's token usage for the dashboard usage.jsonl record (best-effort; many
    # local servers return usage, some don't → the record falls back to 0/unknown like the Claude path).
    _u = j.get("usage") or {}
    if _u:
        _USAGE_ACC["had_usage"] = True
        _USAGE_ACC["input"] = max(_USAGE_ACC["input"], _u.get("prompt_tokens", 0) or 0)
        _USAGE_ACC["output"] += _u.get("completion_tokens", 0) or 0
        _USAGE_ACC["cost"] += float(_u.get("cost", 0) or 0)
    # Track spend for BRAIN=api (OpenRouter returns usage.prompt_tokens + cost in some responses)
    if _SPEND_LOG and j.get("usage"):
        try:
            entry = {"ts": time.strftime("%FT%TZ", time.gmtime()), "model": endpoint["model"],
                     "prompt_tokens": j["usage"].get("prompt_tokens", 0),
                     "completion_tokens": j["usage"].get("completion_tokens", 0),
                     "usd": float(j.get("usage", {}).get("cost", 0) or 0)}
            _SPEND_LOG.parent.mkdir(parents=True, exist_ok=True)
            with _SPEND_LOG.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass
    msg = (j.get("choices") or [{}])[0].get("message", {}) or {}
    # Reasoning models (e.g. NVIDIA Nemotron) split thinking into a separate `reasoning` field and
    # only fill `content` once they stop reasoning — if a long trace hits max_tokens first
    # (finish_reason=length), `content` is absent ENTIRELY. Resolve robustly (content → reasoning)
    # instead of KeyError-crashing the whole tick.
    text = (msg.get("content") or msg.get("reasoning") or "")
    if tools is not None:
        return text, (msg.get("tool_calls") or [])
    return text


# ── tool protocol: parse the model's {tool,input} call ──────────────────────────────────────
# Must survive a tool call whose JSON value is a whole source file (braces, quotes, raw newlines).
# A regex can't (non-greedy stops at the first inner '}'); so scan for brace-balanced, STRING-AWARE
# JSON objects and parse leniently (strict=False tolerates literal newlines/tabs in string values).
def _json_objects(text):
    """Yield (start, substring) for every brace-balanced {...} (string-aware, so braces/quotes in code
    don't fool it)."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1; continue
        depth = 0; in_str = False; esc = False; j = i
        while j < n:
            c = text[j]
            if in_str:
                if esc: esc = False
                elif c == "\\": esc = True
                elif c == '"': in_str = False
            elif c == '"': in_str = True
            elif c == "{": depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    out.append((i, text[i:j + 1])); break
            j += 1
        i = (j + 1) if j > i else i + 1
    return out


# ESCAPING-FREE raw forms — a local coding model can't reliably escape quotes/newlines into JSON
# (every bash command or source file with a quote → unterminated/invalid JSON). So the two heaviest
# tools take a raw fenced body instead, matching how the model naturally writes:
#   ```write <path>          ```bash
#   <raw file content>       <raw shell command(s)>
#   ```                      ```
# Everything else stays JSON ```tool (short/structured args, rarely quote-heavy).
# The newline(s) around the body are OPTIONAL so SINGLE-LINE fences also parse — local models (e.g.
# qwen) very often emit `​``bash cat foo``​` or `​``write /p <content>``​` on ONE line instead of the
# multi-line form, which the strict \n-anchored version rejected → wasted "no tool call" steps.
_WRITE_RE = re.compile(r"```write[ \t]+(\S+)[ \t]*\r?\n?(.*?)\r?\n?```", re.DOTALL)
_BASH_RE = re.compile(r"```(?:bash|sh|shell)[ \t]*\r?\n?(.*?)\r?\n?```", re.DOTALL)


def parse_tool_call(text):
    """Return the FIRST actionable tool call in document order, or None. PURE (unit-tested).
    FIRST (not last) because the model often emits SEVERAL calls per reply (the trailing one is
    frequently truncated by max_tokens); executing the first + feeding back its observation lets the
    model proceed step-by-step. Handles raw ```write / ```bash forms + fenced/unfenced/code-bearing JSON."""
    text = text or ""
    spans, candidates = [], []                         # raw-form spans (exclude their JSON), (pos, call)
    for m in _WRITE_RE.finditer(text):
        spans.append((m.start(), m.end()))
        candidates.append((m.start(), {"tool": "write",
                                       "input": {"file_path": m.group(1).strip(), "content": m.group(2)}}))
    for m in _BASH_RE.finditer(text):
        spans.append((m.start(), m.end()))
        candidates.append((m.start(), {"tool": "bash", "input": {"command": m.group(1)}}))
    for pos, raw in _json_objects(text):
        if any(a <= pos < b for a, b in spans):        # braces inside a raw body aren't calls
            continue
        try:
            obj = json.loads(raw, strict=False)        # strict=False: allow raw \n/\t inside strings
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "tool" in obj:
            obj.setdefault("input", {})
            candidates.append((pos, obj))
    if candidates:
        candidates.sort(key=lambda c: c[0])
        return candidates[0][1]
    return None


# ── NATIVE tool-calling (N3, 2026-07-20): OpenAI function-calling schemas for the same tools ──
# Gated by LOCAL_NATIVE_TOOLS=auto|off (default auto): request native tool_calls from providers
# that support them (sonnet/qwen via OpenRouter/NVIDIA do); on a request error, fall back to the
# fenced-text protocol for the rest of the tick. History stays TEXT-shaped either way (the native
# call is re-rendered as a ```tool block in the assistant message) so both extraction paths coexist
# and context-trimming logic is untouched.
def _p(props, req):
    return {"type": "object", "properties": props, "required": req}


_S = {"type": "string"}
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "bash", "description": "Run shell command(s). All code/CLIs (python3/node/curl/git) run through this.",
     "parameters": _p({"command": _S}, ["command"])}},
    {"type": "function", "function": {"name": "read", "description": "Read a file (optionally offset/limit lines).",
     "parameters": _p({"file_path": _S, "offset": {"type": "integer"}, "limit": {"type": "integer"}}, ["file_path"])}},
    {"type": "function", "function": {"name": "write", "description": "Write/overwrite a file with the FULL raw content.",
     "parameters": _p({"file_path": _S, "content": _S}, ["file_path", "content"])}},
    {"type": "function", "function": {"name": "edit", "description": "Replace old_string with new_string in a file (old_string must match exactly).",
     "parameters": _p({"file_path": _S, "old_string": _S, "new_string": _S}, ["file_path", "old_string", "new_string"])}},
    {"type": "function", "function": {"name": "glob", "description": "Find files by glob pattern.",
     "parameters": _p({"pattern": _S, "path": _S}, ["pattern"])}},
    {"type": "function", "function": {"name": "grep", "description": "Search file contents by regex.",
     "parameters": _p({"pattern": _S, "path": _S, "glob": _S}, ["pattern"])}},
    {"type": "function", "function": {"name": "qmd", "description": "Semantic recall over the workspace + your memory.",
     "parameters": _p({"query": _S}, ["query"])}},
    {"type": "function", "function": {"name": "rlm", "description": "Reason over a HUGE file that cannot be read whole (map-reduce on the cheap brain).",
     "parameters": _p({"query": _S, "file": _S}, ["query", "file"])}},
    {"type": "function", "function": {"name": "escalate", "description": "Ask the stronger external model ONE hard judgment question. Not for routine work.",
     "parameters": _p({"question": _S, "context": _S}, ["question"])}},
    {"type": "function", "function": {"name": "finish", "description": "End the tick when work is done AND verified. decisions[] = every real decision made this tick; evidence must be something you actually observed via a tool THIS tick, or the word 'none'. Pass [] if the tick truly decided nothing.",
     "parameters": _p({"summary": _S,
                       "decisions": {"type": "array", "items": _p({"decision": _S, "why": _S, "evidence": _S,
                                                                   "rejected": _S, "confidence": _S},
                                                                  ["decision", "why", "evidence"])}},
                      ["summary", "decisions"])}},
]


def native_tool_call(tool_calls):
    """First native tool_call → our {tool, input} shape, or None. Tolerates bad-JSON arguments."""
    for tc in tool_calls or []:
        fn = (tc or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args, strict=False)
            except (json.JSONDecodeError, ValueError):
                return {"tool": name, "input": {}, "_bad_args": True}
        return {"tool": name, "input": args if isinstance(args, dict) else {}}
    return None


# ── guardrails: reuse the SAME hook the Claude path uses (one source of truth) ──────────────
# map our tool → the {tool_name, tool_input} contract guard.py expects.
def _guard_payload(tool, inp):
    if tool == "bash":
        return {"tool_name": "Bash", "tool_input": {"command": inp.get("command", "")}}
    if tool == "write":
        return {"tool_name": "Write", "tool_input": {"file_path": inp.get("file_path", ""), "content": inp.get("content", "")}}
    if tool == "edit":
        return {"tool_name": "Edit", "tool_input": {"file_path": inp.get("file_path", ""), "content": inp.get("new_string", "")}}
    if tool == "read":
        return {"tool_name": "Read", "tool_input": {"file_path": inp.get("file_path", "")}}
    return None   # glob/grep/qmd/escalate/finish are not security-sensitive tool classes


def guard_check(agent_dir, hook, tool, inp):
    """Run the role guard hook. Returns (allow, reason). Fail-OPEN on a missing hook (matches the
    Claude path's fail-open on unparseable input) but fail-CLOSED on an explicit exit-2 block."""
    payload = _guard_payload(tool, inp)
    if payload is None:
        return True, ""
    hook_path = HERE / "hooks" / hook
    if not hook_path.exists():
        return True, ""
    try:
        p = subprocess.run(["python3", str(hook_path)], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=15)
    except Exception:
        return True, ""   # never wedge the agent on a guard infra error
    if p.returncode == 2:
        return False, (p.stderr or "blocked by guard").strip()
    return True, ""


# ── tool executors ─────────────────────────────────────────────────────────────────────────
def _truncate(s, n=8000):
    return s if len(s) <= n else s[:n] + f"\n…[truncated {len(s)-n} chars]"


def exec_bash(agent_dir, inp):
    cmd = inp.get("command", "")
    if not cmd:
        return "error: no command"
    try:
        # errors="replace": a command that emits BINARY to stdout (e.g. a screenshot/render piping a
        # PNG — byte 0x89…) must not crash the tick on a UTF-8 decode. The broad except below is the
        # backstop: a single tool error returns a message, it never kills the whole tick.
        p = subprocess.run(["bash", "-lc", cmd], cwd=str(agent_dir), capture_output=True,
                           text=True, errors="replace", timeout=int(inp.get("timeout", 180)))
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        out = out.strip() or "(no output)"
        # STRUCTURED failure: the exit code is the truth, never a string prefix. The old
        # `obs.startswith("error") or obs.startswith("[")` heuristic was wrong both ways —
        # stdout beginning "[" (JSON arrays, [INFO] logs) counted as failure, and
        # "(exit 1, no output)" counted as success (evaluation 2026-07-19, I-3).
        if p.returncode != 0:
            out += f"\n(exit {p.returncode})"
        return _truncate(out), p.returncode != 0
    except subprocess.TimeoutExpired:
        return "error: command timed out", True
    except Exception as e:
        return f"error: {type(e).__name__}: {e}", True


def _resolve(agent_dir, path):
    p = pathlib.Path(path)
    return p if p.is_absolute() else (pathlib.Path(agent_dir) / p)


def exec_read(agent_dir, inp):
    try:
        txt = _resolve(agent_dir, inp.get("file_path", "")).read_text(errors="replace")
    except Exception as e:
        return f"error: {e}"
    lines = txt.splitlines()
    off = int(inp.get("offset", 0)); lim = int(inp.get("limit", 0)) or len(lines)
    sel = lines[off:off + lim]
    return _truncate("\n".join(f"{off+i+1}\t{ln}" for i, ln in enumerate(sel)))


# Binary asset extensions an LLM must NEVER author as text — pasting base64/byte content yields a
# corrupt file (it can't emit valid image/font/audio bytes) that silently renders as an EMPTY BOX.
# Root-caused on fireforge: the model wrote ```write …/symbol.png iVBORw0KGgo…<hallucinated bytes>```.
_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".woff", ".woff2", ".ttf",
               ".otf", ".eot", ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".webm", ".mov", ".atlas",
               ".ktx2", ".basis", ".bin", ".wasm", ".pdf", ".zip", ".gz")

_RO_CMDS = {"ls", "cat", "head", "tail", "find", "grep", "rg", "stat", "wc", "pwd", "echo",
            "file", "du", "tree", "which", "printenv", "env", "realpath", "basename", "dirname"}

def _readonly_bash(cmd):
    """True iff a bash command is pure read-only inspection (no writes/redirects) → safe to dedup when
    repeated verbatim. Conservative: any redirect or any non-read-only leading token → not read-only
    (so re-renders / re-builds / python scripts are never skipped)."""
    c = (cmd or "").strip()
    if not c or ">" in c or ">>" in c:
        return False
    for seg in re.split(r"\|\||&&|;|\|", c):
        toks = seg.strip().lstrip("(").strip().split()
        if toks and toks[0] not in _RO_CMDS:
            return False
    return True

def exec_write(agent_dir, inp):
    fp = inp.get("file_path", "")
    if fp.lower().endswith(_BINARY_EXT):
        return ("error: refused — you cannot author a BINARY file (image/font/audio) as text. Pasted "
                "base64/byte content produces a CORRUPT file that renders as an empty box. "
                "To CREATE art: `bash python3 /workspace/tools/image/gen.py --prompt \"…\" --out " + fp + "`. "
                "To get per-symbol PNGs FROM an existing atlas/spritesheet: write a small Pillow script "
                "(open the atlas, .crop() each region, .save() each cell) and run it via bash. "
                "Never put base64/byte data in a write.")
    # secret-leak gate (2026-07-20): never persist a credential VALUE into a durable file.
    try:
        from hooks import secret_scan
        leak = secret_scan.scan(fp, inp.get("content", ""))
        if leak:
            return "error: refused — " + leak, True
    except Exception:
        pass
    try:
        f = _resolve(agent_dir, fp)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(inp.get("content", ""))
        return f"wrote {f}{_post_write_check(f)}"
    except Exception as e:
        return f"error: {e}"


def _post_write_check(f):
    """Regression guard: cheap syntax check after a code write, surfaced inline so the model fixes it
    THIS step instead of carrying a broken file forward (I-010: a rewrite silently broke the client)."""
    s = str(f)
    try:
        if s.endswith(".py"):
            p = subprocess.run(["python3", "-m", "py_compile", s], capture_output=True, text=True, timeout=30)
            if p.returncode: return f"  ⚠ SYNTAX ERROR (fix before continuing):\n{(p.stderr or '')[-400:]}"
        elif s.endswith(".js") or s.endswith(".mjs"):
            if subprocess.run(["bash", "-lc", "command -v node"], capture_output=True).returncode == 0:
                p = subprocess.run(["node", "--check", s], capture_output=True, text=True, timeout=30)
                if p.returncode: return f"  ⚠ JS SYNTAX ERROR (fix before continuing):\n{(p.stderr or '')[-400:]}"
        elif s.endswith(".json"):
            json.loads(pathlib.Path(s).read_text())
    except json.JSONDecodeError as e:
        return f"  ⚠ INVALID JSON (fix before continuing): {e}"
    except Exception:
        pass
    return ""


def exec_edit(agent_dir, inp):
    try:
        f = _resolve(agent_dir, inp.get("file_path", ""))
        txt = f.read_text()
        old, new = inp.get("old_string", ""), inp.get("new_string", "")
        if old not in txt:
            return "error: old_string not found"
        if txt.count(old) > 1 and not inp.get("replace_all"):
            return "error: old_string is not unique (set replace_all or add context)"
        f.write_text(txt.replace(old, new))
        return f"edited {f}"
    except Exception as e:
        return f"error: {e}"


def exec_glob(agent_dir, inp):
    base = _resolve(agent_dir, inp.get("path", "."))
    hits = globmod.glob(str(base / "**" / inp.get("pattern", "*")), recursive=True)
    return _truncate("\n".join(sorted(hits)[:200]) or "(no matches)")


def exec_grep(agent_dir, inp):
    args = ["grep", "-rniE", inp.get("pattern", "")]
    if inp.get("glob"):
        args += ["--include", inp["glob"]]
    args.append(str(_resolve(agent_dir, inp.get("path", "."))))
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return _truncate(p.stdout.strip() or "(no matches)")
    except Exception as e:
        return f"error: {e}"


def _mcp_post(url, payload, session=None, timeout=20):
    """One MCP streamable-HTTP POST. Returns (parsed_json_or_None, response_headers)."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if session:
        req.add_header("Mcp-Session-Id", session)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        hdrs = r.headers
    # body is either plain JSON or SSE ("event: message\ndata: {...}")
    if "data:" in raw:
        for ln in raw.splitlines():
            if ln.startswith("data:"):
                try:
                    return json.loads(ln[5:].strip()), hdrs
                except json.JSONDecodeError:
                    continue
        return None, hdrs
    try:
        return json.loads(raw), hdrs
    except json.JSONDecodeError:
        return None, hdrs


def exec_qmd(agent_dir, inp):
    """Semantic recall via the host qmd MCP (streamable HTTP). Does the initialize→initialized→
    tools/call handshake the protocol requires. Local + offline-capable; best-effort."""
    url = os.environ.get("QMD_URL", "http://host.docker.internal:18181/mcp")
    q = inp.get("query", "")
    try:
        _, hdrs = _mcp_post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                             "clientInfo": {"name": "local_agent", "version": "1"}}})
        session = hdrs.get("Mcp-Session-Id") or hdrs.get("mcp-session-id")
        _mcp_post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        res, _ = _mcp_post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                 "params": {"name": "query", "arguments":
                                            {"intent": q, "searches": [{"type": "vec", "query": q}]}}}, session)
        if not res:
            return "qmd: empty response"
        if res.get("error"):
            return f"qmd error: {res['error']}"
        # MCP tools/call result → content[].text
        parts = [c.get("text", "") for c in (res.get("result", {}).get("content") or []) if isinstance(c, dict)]
        return _truncate("\n".join(parts).strip() or json.dumps(res.get("result", {}))[:2000])
    except Exception as e:
        return f"qmd unavailable (offline?): {e}"


def exec_escalate(agent_dir, inp, esc, max_tokens, timeout):
    """Send a JUDGMENT question to the external reasoning API. Hybrid: if it's unreachable
    (offline / no key), QUEUE it to state/pending-judgment.md and tell the agent to proceed."""
    question, context = inp.get("question", ""), inp.get("context", "")
    if not esc.get("key") and "openrouter" in esc.get("base", ""):
        return _queue_judgment(agent_dir, question, context, "no escalation key in scope")
    msgs = [{"role": "system", "content": "You are a senior reasoning advisor to an autonomous worker agent. "
             "Give a decisive, well-reasoned verdict it can act on. Be concise."},
            {"role": "user", "content": f"DECISION NEEDED:\n{question}\n\nCONTEXT:\n{context}"}]
    try:
        ans = chat(esc, msgs, max_tokens, 0.2, timeout)
        # (obs, err) tuple: a delivered verdict is a SUCCESS — the old "[" prefix heuristic
        # counted every one of these as a failure (16/16 false failure rate, evaluation I-2).
        return f"[escalation verdict from {esc['model']}]\n{ans}", False
    except Exception as e:
        return _queue_judgment(agent_dir, question, context, f"escalation unreachable: {e}"), True


def _queue_judgment(agent_dir, question, context, why):
    f = pathlib.Path(agent_dir) / "state" / "pending-judgment.md"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as h:
            h.write(f"\n## {time.strftime('%FT%TZ', time.gmtime())} ({why})\n**Q:** {question}\n\n{context}\n")
    except OSError:
        pass
    return (f"escalation queued to state/pending-judgment.md ({why}). Proceed with bounded, "
            f"verifiable work you CAN do now; the judgment item waits for an online/operator pass.")


def exec_rlm(agent_dir, inp):
    """Reason over a HUGE input (giant log/file/blob) without context rot: chunk → map a
    sub-query over each chunk on the cheap brain → recursively reduce. Own impl (rlm.py),
    no external dep. Input: {"query": "...", "file": "...path..."} or {"query","text"}."""
    query = (inp.get("query") or inp.get("question") or "").strip()
    if not query:
        return "rlm error: provide a 'query'."
    text = inp.get("text")
    if text is None:
        fp = inp.get("file") or inp.get("file_path")
        if not fp:
            return "rlm error: provide 'file' (a path) or 'text'."
        p = pathlib.Path(fp)
        if not p.is_absolute():
            p = pathlib.Path(agent_dir) / fp
        try:
            text = p.read_text(errors="replace")
        except OSError as e:
            return f"rlm error: cannot read {fp}: {e}"
    try:
        from rlm import run_rlm
        return _truncate(run_rlm(query, text))
    except Exception as e:
        return f"rlm unavailable: {e}"


EXECUTORS = {"bash": exec_bash, "read": exec_read, "write": exec_write, "edit": exec_edit,
             "glob": exec_glob, "grep": exec_grep, "qmd": exec_qmd, "rlm": exec_rlm}


# ── the harness preamble that teaches the model the tool protocol ───────────────────────────
HARNESS = """\
# LOCAL AGENT HARNESS — how you operate

You act by emitting EXACTLY ONE tool call per reply. If NATIVE function-calling tools are available
to you, use them — one call per turn; the fenced-block forms below are the fallback text protocol.
After each call you receive an `OBSERVATION`. Keep going — read, run, write, verify — until the task
in your turn is genuinely done, then call the `finish` tool. Brief reasoning first is fine; emit
only ONE action (native call or block) and nothing after it. NEVER write an "OBSERVATION" yourself —
observations come only from the runtime; a narrated observation you did not receive is fabrication.

Tools — the two you use most take a RAW fenced body (NO JSON, NO escaping):

  run a shell command:                 write/overwrite a file (great for code):
  ```bash                              ```write <file_path>
  <your shell command(s)>              <the raw file content, exactly as-is>
  ```                                  ```

  Put quotes, newlines, braces, code RAW inside those blocks — do NOT wrap them in JSON (JSON with a
  quoted command or a whole source file WILL break and be discarded). One block = one action.
  (A single-line block is also accepted, e.g. ```bash ls -la``` — but you MUST close the fence with ```.)

The remaining tools use a JSON ```tool block (their args are short — no quote-heavy content):
- read      {"tool":"read","input":{"file_path":"...","offset"?,"limit"?}}
- edit      {"tool":"edit","input":{"file_path":"...","old_string":"...","new_string":"..."}}
- glob      {"tool":"glob","input":{"pattern":"*.py","path"?:"."}}
- grep      {"tool":"grep","input":{"pattern":"regex","path"?:".","glob"?:"*.py"}}
- qmd       {"tool":"qmd","input":{"query":"..."}}        semantic recall over workspace + your memory
- rlm       {"tool":"rlm","input":{"query":"...","file":"big.log"}}  reason over a HUGE file/blob
            (chunks + map-reduces it on the cheap brain — use when one file is too big to read in full)
- escalate  {"tool":"escalate","input":{"question":"...","context":"..."}}  HARD judgment → external model
- finish    {"tool":"finish","input":{"summary":"what you did + evidence",
             "decisions":[{"decision":"...","why":"...","evidence":"cmd/URL/observation that backs it — or the word none","rejected"?:"...","confidence"?:"high|medium|low"}]}}
            The runtime writes `decisions` to state/decisions.jsonl (your reasoning is REVIEWED, not
            just your output). Log every real decision made this tick; a routine tick with none is
            fine — pass "decisions":[]. `evidence` must be something you actually OBSERVED via a tool
            THIS tick (it is cross-checked against the event log); if you have none, write "none" —
            an honest guess is fine, a narrated observation you never received is fabrication.

Rules:
- You are a LOCAL worker model: do BOUNDED, verifiable work yourself (build/edit/run/measure/scan/
  draft) and VERIFY it (run the test/script, check the output). For genuine high-judgment or
  strategic forks, use `escalate` rather than guessing — but don't escalate routine work.
- WRITE THE LEAST CODE THAT WORKS. Before writing code, stop at the first rung that holds: (1) does it
  need to exist? — no → skip it, say so in one line; (2) stdlib or a native platform feature covers it?
  → use it; (3) an already-installed dependency solves it? → use it; never add a new dep for what a few
  lines do (and a new dep needs a security pass first); (4) one line? → one line; (5) only then the
  minimum that works. Lazy ≠ careless: input/trust-boundary validation, security, data-loss handling,
  and the guardrails are NEVER cut. Shortest working diff wins; deletion over addition; no abstraction
  for one caller. Mark a deliberate shortcut with a `# minimal:` comment naming its ceiling + upgrade
  path (`# minimal: O(n^2) scan; index if it grows`) so it reads as intent, not ignorance.
- Run ALL code/commands via a ```bash block (there is no python3/node/pip tool — call them inside bash).
- Do NOT guess file paths. A path you can't see is probably wrong — locate it with a ```bash `ls`/`find`
  (or glob/grep) before reading; don't repeatedly try plausible-looking paths that error.
- Guardrails are enforced mechanically: some actions are BLOCKED (you'll see why in the OBSERVATION)
  — adapt, don't retry verbatim.
- ONE action block per reply. Examples:
```bash
ls -la /agent/work/eval && echo 'done'
```
or to save a file:
```write /agent/work/eval/run.py
#!/usr/bin/env python3
print("hello")
```
"""


def run(agent_dir):
    agent_dir = pathlib.Path(agent_dir).resolve()
    brain, esc = resolve_endpoints()
    max_steps = int(os.environ.get("LOCAL_MAX_STEPS", "24"))
    max_tokens = int(os.environ.get("LOCAL_MAX_TOKENS", "6144"))   # build pods write whole files; 2048 truncated
    temp = float(os.environ.get("LOCAL_TEMP", "0.3"))
    timeout = int(os.environ.get("LOCAL_REQ_TIMEOUT", "360"))   # 80B cold-reload after idle can exceed 180s
    hook = os.environ.get("GUARD_HOOK") or "guard.py"

    if os.environ.get("WORKER_MODE"):
        # Delegated worker: not an autonomous tick — execute ONE subtask handed down by a manager.
        # Ignore CLAUDE.md/recall (no persona/world-rebuild); the task IS the instruction. Escalation
        # is blocked below (workers never call the frontier; that's the manager's job).
        system = ("You are a WORKER sub-agent. A manager delegated you ONE concrete task. Do EXACTLY "
                  "that task using your tools (bash/read/write/edit/glob/grep/qmd) — write real, "
                  "COMPLETE files, run code to check it. Do not ask questions, do not expand scope, do "
                  "not git/commit. When the task is done AND verified, call `finish` with a 2-5 line "
                  "summary of what you did and which files you changed.")
        turn = os.environ.get("DELEGATE_TASK", "Complete the delegated task, then call finish.")
    else:
        system = (agent_dir / "CLAUDE.md").read_text() if (agent_dir / "CLAUDE.md").exists() else ""
        turn = (agent_dir / "tick.txt").read_text() if (agent_dir / "tick.txt").exists() else "Run one autonomous tick."
        recall = (agent_dir / "state" / "recall.md")
        if recall.exists():
            turn = f"{turn}\n\n<recall>\n{recall.read_text()[:6000]}\n</recall>"

    messages = [{"role": "system", "content": system + "\n\n" + HARNESS},
                {"role": "user", "content": turn}]

    def log(m):
        print(f"{time.strftime('%FT%TZ', time.gmtime())} — [local_agent] {m}", flush=True)

    log(f"start brain={brain['model']} @ {brain['base']} (esc={esc['model']}, guard={hook}, max_steps={max_steps})")
    # ── dashboard telemetry: tick_start event + a per-tick usage.jsonl record at every exit ──
    sd = agent_dir / "state"
    aid = os.environ.get("AGENT_ID", "")
    reason = os.environ.get("TICK_REASON", "heartbeat")
    started = time.time()
    _USAGE_ACC.update({"input": 0, "output": 0, "cost": 0.0, "had_usage": False})
    _tool_n = {}                                           # tool → [calls, fails]
    _proto_n = {"native": 0, "text": 0}                    # N3: extraction channel per call (visibility)
    _steps_done = [0]
    _emit_event(sd, {"ts": int(time.time()), "agent": aid, "event": "tick_start", "source": "local"})

    def _finish(rc, subtype):
        _emit_event(sd, {"ts": int(time.time()), "agent": aid, "event": "tick_end"})
        rt = {"tool_calls": sum(v[0] for v in _tool_n.values()),
              "tool_failures": sum(v[1] for v in _tool_n.values()),
              "tools": {k: {"n": v[0], "fail": v[1]} for k, v in _tool_n.items()},
              "protocol": dict(_proto_n),
              "available": True}
        log(f"tool extraction: {_proto_n['native']} native / {_proto_n['text']} text")
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "agent": aid, "reason": reason,
               "model": brain["model"], "input": _USAGE_ACC["input"], "output": _USAGE_ACC["output"],
               "cache_read": 0, "cache_write": 0,
               "cost_usd": round(_USAGE_ACC["cost"], 6) if _USAGE_ACC["had_usage"] else 0.0,
               "duration_s": round(time.time() - started, 1), "turns": _steps_done[0],
               "rc": rc, "subtype": subtype, "runtime": rt}
        try:
            sd.mkdir(parents=True, exist_ok=True)
            with (sd / "usage.jsonl").open("a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:
            pass
        return rc

    nudges = 0
    seen_calls = {}                                        # signature → count (anti-repetition)
    finish_nudged = False                                  # decisions-at-finish: one nudge, never a wedge
    # Native tool-calling (N3): auto = request structured tool_calls, fall back to text on a 4xx.
    native_mode = os.environ.get("LOCAL_NATIVE_TOOLS", "auto").lower() not in ("off", "0", "false")
    produce_by = max(4, int(max_steps * 0.6))             # after this, force produce+finish (anti-wander)
    forced = False
    for step in range(1, max_steps + 1):
        _steps_done[0] = step
        # Forcing function: local models tend to research forever. Past the budget, steer HARD to
        # commit a deliverable + finish (the wander-til-max_steps failure mode, observed 2026-06-13).
        if step >= produce_by and not forced:
            forced = True
            messages.append({"role": "user", "content":
                f"STOP RESEARCHING — you have ~{max_steps - step + 1} steps left. You have read enough. "
                "Now: (1) WRITE one concrete deliverable to /agent/work (real content, from what you "
                "found), (2) VERIFY it (read it back, and if it's code RUN it via bash), (3) update your "
                "work queue — `bash`: python3 /agent/bin/memory.py --base /agent work done <id> "
                "--evidence \"<what+path>\" (or `work doing <id>` if multi-tick), (4) update "
                "/agent/state/rollup.md (one line) + write a memory lesson, (5) call `finish`. "
                "Do NOT read or qmd again unless a write fails."})
        # Retry with backoff: the Mac runs ONE shared MLX server (:8081). When several local pods
        # tick concurrently the busy server can drop a connection ("Remote end closed ...") — that's
        # transient contention, not a real failure, so ride it out instead of abandoning the tick.
        reply, native_calls = None, []
        for attempt in range(4):
            try:
                if native_mode:
                    try:
                        reply, native_calls = chat(brain, messages, max_tokens, temp, timeout,
                                                   tools=TOOL_SCHEMAS)
                    except urllib.error.HTTPError as e:
                        # Provider rejects the tools payload (unsupported model/endpoint) → text
                        # protocol for the rest of the tick. Only 4xx means "unsupported"; a 5xx is
                        # transient and takes the normal retry path.
                        if 400 <= e.code < 500:
                            native_mode = False
                            log(f"native tools rejected (HTTP {e.code}) — falling back to text protocol")
                            reply = chat(brain, messages, max_tokens, temp, timeout)
                        else:
                            raise
                else:
                    reply = chat(brain, messages, max_tokens, temp, timeout)
                break
            except Exception as e:
                if attempt == 3:
                    log(f"brain call failed after 4 tries: {e}")
                    return _finish(1, "brain_error")
                wait = 3 * (attempt + 1)   # 3s, 6s, 9s
                log(f"brain call retry {attempt + 1}/3 ({e}); waiting {wait}s for the shared MLX server")
                time.sleep(wait)
        # Native call (structured, API-parsed) wins over fishing the text. The assistant message is
        # stored TEXT-shaped either way — the native call re-rendered as a ```tool block — so history,
        # trimming, and the fallback parser see one consistent protocol.
        call = native_tool_call(native_calls)
        if call is not None:
            _proto_n["native"] += 1
            rendered = json.dumps({"tool": call["tool"], "input": call.get("input", {})})[:4000]
            messages.append({"role": "assistant", "content": (reply or "") + "\n```tool\n" + rendered + "\n```"})
            if call.pop("_bad_args", None):
                log(f"step {step}: native {call.get('tool')} had unparseable arguments — nudging")
                messages.append({"role": "user", "content":
                    f"OBSERVATION:\n[malformed] Your native {call.get('tool')} call had unparseable "
                    "arguments. Call it again with valid JSON arguments."})
                continue
        else:
            messages.append({"role": "assistant", "content": reply})
            call = parse_tool_call(reply)
            if call is not None:
                _proto_n["text"] += 1
        if not call:
            # No tool block. Don't bail on prose — the model often narrates (or, as a coding model,
            # dumps a ```python block) before/instead of acting. NUDGE it; only give up after several.
            nudges += 1
            preview = (reply or "").strip().replace("\n", " ")[:160]
            log(f"step {step}: no tool call ({nudges}) — reply: {preview}")
            if "```" in (reply or ""):                  # dump full fenced reply for diagnosis (I-005)
                try: (agent_dir / "state" / "last-failed-reply.txt").write_text(reply or "")
                except OSError: pass
            if nudges >= 5:
                log(f"step {step}: no tool call after {nudges} nudges — treating as final")
                return _finish(0, "no_tool")
            # Format-corrective: if the reply contains a fenced code block, the model produced content
            # but in the wrong wrapper — tell it EXACTLY how to save it as a write call (I-004).
            has_code = "```" in (reply or "")
            if has_code:
                msg = ('I see a fenced code block but NOT a parseable tool call, so nothing was saved '
                       '(a JSON ```tool block with a whole file usually breaks: the escaping balloons it '
                       'past the token limit and it truncates mid-string). To SAVE that file, re-emit it '
                       'as the RAW ```write form — NO JSON, NO escaping:\n```write <path under /agent/work>\n'
                       '<the complete file content, exactly as-is>\n```\nPut the raw content (quotes, '
                       'newlines, braces) directly between the fences. One block only. If the file is '
                       'large, write it in pieces: first ```write the file with the top half, then a '
                       'follow-up ```bash that appends the rest (cat >> with a heredoc).')
            else:
                msg = ("You did not emit a ```tool block (only prose). Take the NEXT concrete action as a "
                       "single ```tool block now (write/edit/bash/read…), or call `finish` if the "
                       "deliverable is already written, verified, and the work queue + rollup updated.")
            messages.append({"role": "user", "content": msg})
            continue
        nudges = 0
        tool, inp = call.get("tool"), call.get("input", {})
        # Robust normalization: models sometimes nest the call as {"tool":{"name":..,"arguments":..}}
        # or use "parameters"/"args" for the input. Coerce instead of crashing (I-001).
        if isinstance(tool, dict):
            inp = tool.get("input") or tool.get("arguments") or tool.get("parameters") or inp
            tool = tool.get("name") or tool.get("tool")
        if not isinstance(inp, dict):
            inp = {}
        if not isinstance(tool, str) or not tool:
            log(f"step {step}: malformed tool call — nudging")
            messages.append({"role": "user", "content":
                'OBSERVATION:\n[malformed] Emit exactly one ```tool block: {"tool":"<name>","input":{...}} '
                'where <name> is one of bash/read/write/edit/glob/grep/qmd/escalate/finish. Try again.'})
            continue
        if tool == "finish":
            # Decision capture is a RUNTIME MECHANISM, not brief prose (2026-07-20: "an instruction
            # agents can ignore is not a mechanism" — channel-lab ignored the logging convention for
            # a full day). The finish contract carries `decisions`; the RUNTIME writes the file, so
            # capture cannot be skipped silently. One nudge if the key is missing after a working
            # tick; a second finish always succeeds (never wedge an agent on paperwork).
            decs = inp.get("decisions")
            worked = sum(n for n, _ in _tool_n.values()) >= 5
            if decs is None and worked and not finish_nudged:
                finish_nudged = True
                messages.append({"role": "user", "content":
                    'OBSERVATION:\n[finish needs `decisions`] You did real work this tick but your '
                    'finish carried no "decisions" list. Re-emit finish WITH it: every real decision '
                    'as {"decision","why","evidence"} — evidence = a command/URL/observation you '
                    'actually received THIS tick, or the word "none". If the tick truly decided '
                    'nothing, pass "decisions":[].'})
                continue
            if isinstance(decs, dict):
                decs = [decs]
            if isinstance(decs, list):
                try:
                    with (sd / "decisions.jsonl").open("a") as fh:
                        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
                        for d in decs:
                            if isinstance(d, dict) and d.get("decision"):
                                fh.write(json.dumps({"ts": now_iso, **d, "_by": "finish",
                                                     "_steps": step}) + "\n")
                except Exception:
                    pass
            log(f"finish: {inp.get('summary','')[:200]} ({len(decs) if isinstance(decs, list) else 0} decision(s))")
            # Log the finish itself BEFORE returning — the old code returned before the per-tool
            # emit, so events.jsonl held no trace of an explicit finish and "count Finish events"
            # measured the logger, not the agent (evaluation I-1).
            _emit_event(sd, {"ts": int(time.time()), "agent": aid, "event": "tool", "tool": "finish",
                             "summary": (inp.get("summary", "") or "")[:200],
                             "decisions": len(decs) if isinstance(decs, list) else 0})
            return _finish(0, "ok")
        allow, reason = guard_check(agent_dir, hook, tool, inp)
        if not allow:
            log(f"step {step}: {tool} BLOCKED — {reason}")
            # Blocked calls MUST leave an event trace (2026-07-20): a guard-blocked call used to
            # vanish from events.jsonl entirely, so when a pod then NARRATED results for the call it
            # never ran, the audit trail could neither confirm nor refute it. Every attempt is
            # auditable now — the event log is the instrument the fabrication check reads.
            _emit_event(sd, {"ts": int(time.time()), "agent": aid, "event": "tool", "step": step,
                             "tool": _EVENT_TOOL.get(tool, tool), "blocked": True, "ok": False,
                             "summary": _redact(_event_summary(tool, inp)),
                             "result": _redact(f"[BLOCKED by guard] {reason}")[:400]})
            messages.append({"role": "user", "content": f"OBSERVATION:\n[BLOCKED by guard] {reason}"})
            continue
        # Anti-repetition: a read-only call repeated verbatim is wasted budget (a wander symptom).
        # Includes read-only BASH (ls/cat/head/find/grep/…) — the model does everything via bash, so
        # without this it re-`cat`s work.json / re-`ls`es dirs dozens of times until it hits max_steps.
        sig = tool + "|" + json.dumps(inp, sort_keys=True)[:200]
        _ro = tool in ("read", "qmd", "glob", "grep") or (tool == "bash" and _readonly_bash(inp.get("command", "")))
        if _ro and seen_calls.get(sig, 0) >= 1:
            seen_calls[sig] += 1
            log(f"step {step}: {tool} DUPLICATE — skipped")
            messages.append({"role": "user", "content":
                "OBSERVATION:\n[DUPLICATE] You already ran this exact call and have its result above. "
                "Do NOT repeat it — act on what you already have, or call `finish`."})
            continue
        seen_calls[sig] = seen_calls.get(sig, 0) + 1
        # Empty-write guard (I-007): a write with no/blank content means the model emitted an empty
        # heredoc body — don't clobber the file with 0 bytes; dump the reply + tell it to include content.
        if tool == "write" and not (inp.get("content") or "").strip():
            try: (agent_dir / "state" / "last-failed-reply.txt").write_text(reply or "")
            except OSError: pass
            log(f"step {step}: write EMPTY content — refused (dumped reply)")
            messages.append({"role": "user", "content":
                "OBSERVATION:\n[empty write REFUSED] Your write had NO content. Use the heredoc form with the "
                "FULL file body BETWEEN the fences:\n```write " + str(inp.get("file_path", "<path>")) +
                "\n<the complete file content here>\n```\nEmit it now in ONE block."})
            continue
        if tool == "escalate" and os.environ.get("WORKER_MODE"):
            res = ("error: a delegated worker cannot escalate to the frontier model — that is the "
                   "manager's job. Complete the task with your own tools (bash/read/write/edit/grep/qmd) "
                   "or call `finish` reporting what blocked you.")
        elif tool == "escalate":
            res = exec_escalate(agent_dir, inp, esc, max_tokens, timeout)
        elif tool in EXECUTORS:
            res = EXECUTORS[tool](agent_dir, inp)
        else:
            res = (f"error: no '{tool}' tool exists. Run code/commands via the bash tool "
                   "(e.g. bash 'python3 x.py'). Valid tools: bash, read, write, edit, glob, grep, qmd, escalate, finish.")
        # STRUCTURED failure detection: executors that know their outcome return (obs, err).
        # String-only executors keep the "error"-prefix convention — but never bare "[" (I-2/I-3).
        if isinstance(res, tuple):
            obs, _err = res
        else:
            obs = res
            _err = obs.startswith(("error", "qmd error", "rlm error"))
        log(f"step {step}: {tool} → {obs[:120].rstrip()}")
        _slot = _tool_n.setdefault(tool, [0, 0])
        _slot[0] += 1
        if _err:
            _slot[1] += 1
        # Record the RESULT, not just the attempt. events.jsonl was built as the dashboard's live
        # "doing now" ticker, so it logged the command and discarded `obs` — which meant the only
        # durable record of an agent's work was a list of truncated command strings with no
        # outcomes. Evaluating what a pod actually accomplished then requires reconstructing it by
        # hand from fragments (done painfully on 2026-07-20, and done badly). The operator's ask is
        # to evaluate work and performance; that needs outcomes attached to actions.
        _emit_event(sd, {"ts": int(time.time()), "agent": aid, "event": "tool", "step": step,
                         "tool": _EVENT_TOOL.get(tool, tool),
                         "summary": _redact(_event_summary(tool, inp)),
                         "ok": not _err, "result": _redact((obs or "")[:400].rstrip()),
                         **({"error": True} if _err else {})})
        messages.append({"role": "user", "content": f"OBSERVATION:\n{obs}"})
        # Lean context: keep system + turn, then trim the tail by a CHAR BUDGET, not a message
        # count — the old messages[:2] + messages[-24:] was token-blind (24 × an 8000-char
        # observation ≈ 190KB unaccounted; evaluation §3.1).
        if len(messages) > 26 or sum(len(m.get("content", "")) for m in messages[2:]) > _CTX_CHARS:
            head, tail = messages[:2], []
            budget = _CTX_CHARS
            for m in reversed(messages[2:]):
                budget -= len(m.get("content", ""))
                if budget < 0 and tail:
                    break
                tail.append(m)
                if len(tail) >= 24:
                    break
            messages = head + list(reversed(tail))
    log(f"hit max_steps={max_steps} — stopping")
    # Distinct rc: a wandered tick must be distinguishable from a clean finish to the loop and
    # to runtime.sh (the old rc=0 made 65% cap-wander on fireforge invisible; agentloop treats
    # any rc other than 75 the same, so this is telemetry-safe).
    return _finish(4, "max_steps")


def main():
    agent_dir = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent")
    sys.exit(run(agent_dir))


if __name__ == "__main__":
    main()
