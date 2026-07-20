"""fleet_config — writable per-agent configuration (Dashboard control-center, P0 foundation).

The fleet control plane (`fleet.py`) is read-mostly + lifecycle (up/down/restart/send).
This module adds the WRITE half the dashboard needs: safely read and atomically patch an
agent's `home/agent.env`, behind a **key allowlist**, preserving comments + key order, with a
**config-history snapshot** before every change (one-click revert), and high-level setters for
the operator's asks — switch BRAIN (claude/api/local/optimize), toggle run MODE
(autonomous/chat/scheduled), and apply named PRESETS.

Pure stdlib, no deps. Every mutation is appended to the same `fleet-audit.log` the rest of the
control plane writes. The CALLER (fleet.py CLI / console) performs the container restart after a
write — this module only touches files, so it is trivially testable and never needs docker.

CLI (via fleet.py):  enclave fleet config <id> [--json]
                     enclave fleet set-config <id> KEY=VAL [KEY=VAL …]
                     enclave fleet set-brain  <id> <claude|api|local|optimize> [model]
                     enclave fleet set-mode   <id> <autonomous|chat|scheduled> [interval_seconds]
                     enclave fleet preset     <id> <preset-name>
"""
import os, json, time, pathlib, tempfile

AUDIT = pathlib.Path(os.environ.get("ENCLAVE_FLEET_AUDIT",
                     pathlib.Path.home() / ".config" / "enclave" / "fleet-audit.log"))

# Keys the dashboard may edit. Deliberately EXCLUDES identity/wiring (AGENT_ID, COMMS_URL,
# SECRETS, WEB_CHAT_*, WORK_DIR) — those define what the agent IS and where it connects, not
# how it runs, and a bad edit there wedges the deployment.
ALLOWED_KEYS = {
    "BRAIN", "MODEL", "MODEL_ROUTINE", "ROUTER",
    "INTERVAL_SECONDS", "SUPERVISE", "CONTINUOUS_COOLDOWN", "TICK_TIMEOUT", "MAX_TURNS",
    "DELEGATION_ENFORCE", "DELEGATION_MAX_CHARS", "COMPACT_ENFORCE", "PERMISSION", "WORKDIR",
    "LOCAL_BRAIN_MODEL", "LOCAL_BRAIN_BASE", "LOCAL_REQ_TIMEOUT",
    # BRAIN=api (any OpenAI-compatible provider — NVIDIA/OpenRouter/xAI/…): endpoint + driver model +
    # the NAME of the key var, plus a separate judgment-escalation model and a per-agent spend cap.
    "BRAIN_MODEL", "BRAIN_API_BASE", "BRAIN_API_KEY_ENV",
    "ESCALATION_BASE", "ESCALATION_MODEL", "ESCALATION_KEY", "API_BUDGET_USD",
    "GUARD_ALLOW_GIT", "GUARD_EGRESS_ENFORCE",
    "MONITOR_MODE",   # fleet health monitor policy for this agent (off|observe|alert|suggest|autofix)
}
MONITOR_MODES = {"off", "observe", "alert", "suggest", "autofix"}
BRAINS = {"claude", "api", "local", "optimize"}
MODES = {"autonomous", "chat", "scheduled"}

# Named one-click profiles (operator-approved set, 2026-06-25). Data, not code.
PRESETS = {
    # Claude only, no local pool; delegates labor to its OWN subagents via delegate.py.
    "claude-managed": {"BRAIN": "claude", "MODEL": "claude-opus-4-8",
                       "MODEL_ROUTINE": "claude-sonnet-4-6", "ROUTER": "on",
                       "DELEGATION_ENFORCE": "on", "SUPERVISE": "auto"},
    # Cheap autonomous: local/NVIDIA-free worker brain, continuous.
    "autonomous-local-cheap": {"BRAIN": "local", "SUPERVISE": "auto", "ROUTER": "on"},
    # Interactive: replies to messages, no continuous loop, cheap top model.
    "chat-only-sonnet": {"BRAIN": "claude", "MODEL": "claude-sonnet-4-6", "SUPERVISE": "off"},
    # Cost-routed: route_brain picks cheapest reachable pool per tick.
    "optimize": {"BRAIN": "optimize", "ROUTER": "on", "SUPERVISE": "auto"},
    # No-Claude, NVIDIA-free brain (task-routed: qwen drives, MiniMax-M3 for hard judgment). Needs
    # NVIDIA_API_KEY in secrets/nvidia.env. Runs entirely off the Anthropic subscription.
    "no-claude-nvidia": {"BRAIN": "api",
                         "BRAIN_API_BASE": "https://integrate.api.nvidia.com/v1",
                         "BRAIN_API_KEY_ENV": "NVIDIA_API_KEY",
                         "BRAIN_MODEL": "qwen/qwen3-next-80b-a3b-instruct",
                         "ESCALATION_MODEL": "minimaxai/minimax-m3",
                         "SUPERVISE": "off"},
}


def _audit(action, agent, detail=""):
    try:
        # Resolve at CALL time, not import time — a test harness that redirects
        # ENCLAVE_FLEET_AUDIT after this module was first imported must still hit its own file,
        # never the operator's real audit log (truth review T4).
        audit = pathlib.Path(os.environ.get("ENCLAVE_FLEET_AUDIT", AUDIT))
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "who": "fleet_config", "action": action,
                                "agent": agent, "detail": str(detail)[:200]}) + "\n")
    except Exception:
        pass


def _env_path(home):
    return pathlib.Path(home) / "agent.env"


def parse_env(text):
    """Ordered {KEY: VALUE} from .env text (ignores comments/blanks; strips quotes)."""
    out = {}
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _dotenv_path(home):
    """The deployment .env sits beside the home dir (<dep>/.env; home = <dep>/home). Compose
    interpolates it AND the fleet Status snapshot reads it — so BRAIN/MODEL/INTERVAL_SECONDS are
    DUAL-HOMED (also in agent.env) and the container env from .env WINS at runtime. Any edit must
    keep both in sync or the change won't take effect / Status will show a stale value."""
    return pathlib.Path(home).parent / ".env"


def _write_keys(path, updates, append_new):
    """Atomically rewrite `path`, updating matching keys in place (comment/order-preserving);
    append unseen keys only when append_new (used for agent.env, NOT .env)."""
    lines = path.read_text(errors="ignore").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in remaining:
                out.append(f"{k}={remaining.pop(k)}")
                continue
        out.append(ln)
    if append_new:
        for k, v in remaining.items():
            out.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def read_config(home):
    """Effective config = agent.env overlaid with the deployment .env for any DUAL-HOMED key
    (.env wins at runtime), so what the dashboard shows matches the Status snapshot + the running
    container. `editable` = the allowlisted subset the UI should render as inputs."""
    p, d = _env_path(home), _dotenv_path(home)
    raw = p.read_text(errors="ignore") if p.exists() else ""
    env = parse_env(raw)
    dotenv = parse_env(d.read_text(errors="ignore")) if d.exists() else {}
    env.update(dotenv)   # .env is runtime-authoritative
    editable = sorted(k for k in env if k in ALLOWED_KEYS)
    return {"env": env, "raw": raw, "path": str(p), "editable": editable}


def _validate(updates):
    bad = [k for k in updates if k not in ALLOWED_KEYS]
    if bad:
        raise ValueError(f"keys not editable from the dashboard: {sorted(bad)} "
                         f"(allowed: {sorted(ALLOWED_KEYS)})")
    if "BRAIN" in updates and updates["BRAIN"] not in BRAINS:
        raise ValueError(f"BRAIN must be one of {sorted(BRAINS)}")
    if "SUPERVISE" in updates and updates["SUPERVISE"] not in ("auto", "off"):
        raise ValueError("SUPERVISE must be 'auto' or 'off'")
    if "MONITOR_MODE" in updates and updates["MONITOR_MODE"] not in MONITOR_MODES:
        raise ValueError(f"MONITOR_MODE must be one of {sorted(MONITOR_MODES)}")
    for intk in ("INTERVAL_SECONDS", "CONTINUOUS_COOLDOWN", "TICK_TIMEOUT", "LOCAL_REQ_TIMEOUT",
                 "DELEGATION_MAX_CHARS"):
        if intk in updates:
            try:
                int(str(updates[intk]))
            except ValueError:
                raise ValueError(f"{intk} must be an integer")


def _snapshot_history(home):
    """Snapshot BOTH agent.env and the deployment .env into home/state/config-history/ before
    mutating, so a revert restores the exact prior pair."""
    hist = pathlib.Path(home) / "state" / "config-history"
    hist.mkdir(parents=True, exist_ok=True)
    # microsecond suffix so rapid back-to-back edits don't overwrite each other's snapshot
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{int(time.time() * 1e6) % 1_000_000:06d}"
    for src, suffix in ((_env_path(home), ".env"), (_dotenv_path(home), ".dotenv")):
        if src.exists():
            (hist / (stamp + suffix)).write_text(src.read_text(errors="ignore"))
    return stamp


def patch_agent_env(home, updates, agent="?"):
    """Apply {KEY: VALUE} to agent.env (append new keys), AND sync any DUAL-HOMED key that already
    exists in the deployment .env so the change takes effect + the Status snapshot reflects it.
    Comment/order-preserving, atomic, snapshots both files for revert. Returns diff [(key, old, new)]
    computed against the EFFECTIVE (merged) prior value."""
    if not updates:
        return []
    _validate(updates)
    p, d = _env_path(home), _dotenv_path(home)
    old_agent = parse_env(p.read_text(errors="ignore")) if p.exists() else {}
    old_dot = parse_env(d.read_text(errors="ignore")) if d.exists() else {}
    effective = {**old_agent, **old_dot}   # .env wins, matches runtime
    diff = [(k, effective.get(k), str(v)) for k, v in updates.items() if effective.get(k) != str(v)]
    if not diff:
        return []
    _snapshot_history(home)
    _write_keys(p, updates, append_new=True)                       # agent.env: full set, append new
    dot_sync = {k: v for k, v in updates.items() if k in old_dot}  # .env: ONLY keys already present
    if dot_sync:
        _write_keys(d, dot_sync, append_new=False)
    synced = (" [.env-synced: " + ",".join(dot_sync) + "]") if dot_sync else ""
    _audit("set-config", agent, ", ".join(f"{k}:{a or '∅'}→{b}" for k, a, b in diff) + synced)
    return diff


def set_brain(home, brain, model=None, agent="?"):
    brain = (brain or "").strip().lower()
    if brain not in BRAINS:
        raise ValueError(f"brain must be one of {sorted(BRAINS)}")
    upd = {"BRAIN": brain}
    if model:
        upd["MODEL"] = model
    diff = patch_agent_env(home, upd, agent)
    _audit("set-brain", agent, f"{brain}" + (f" model={model}" if model else ""))
    return diff


def set_mode(home, mode, interval=None, agent="?"):
    """autonomous = continuous (SUPERVISE=auto). chat = reply-only (SUPERVISE=off).
    scheduled = heartbeat cadence (SUPERVISE=off + INTERVAL_SECONDS)."""
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    if mode == "autonomous":
        upd = {"SUPERVISE": "auto"}
    elif mode == "chat":
        upd = {"SUPERVISE": "off"}
    else:  # scheduled
        upd = {"SUPERVISE": "off", "INTERVAL_SECONDS": str(int(interval or 10800))}
    diff = patch_agent_env(home, upd, agent)
    _audit("set-mode", agent, mode + (f" interval={interval}" if interval else ""))
    return diff


def apply_preset(home, name, agent="?"):
    if name not in PRESETS:
        raise ValueError(f"unknown preset '{name}' (have: {sorted(PRESETS)})")
    diff = patch_agent_env(home, dict(PRESETS[name]), agent)
    _audit("preset", agent, name)
    return diff
