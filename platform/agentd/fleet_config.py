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
    "INTERVAL_SECONDS", "SUPERVISE", "CONTINUOUS_COOLDOWN", "TICK_TIMEOUT",
    "DELEGATION_ENFORCE", "DELEGATION_MAX_CHARS", "PERMISSION", "WORKDIR",
    "LOCAL_BRAIN_MODEL", "LOCAL_BRAIN_BASE", "LOCAL_REQ_TIMEOUT",
    "GUARD_ALLOW_GIT", "GUARD_EGRESS_ENFORCE",
}
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
}


def _audit(action, agent, detail=""):
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a") as f:
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


def read_config(home):
    """Return {env: {K:V}, raw: str, path: str} for an agent's agent.env (env={} if absent)."""
    p = _env_path(home)
    raw = p.read_text(errors="ignore") if p.exists() else ""
    return {"env": parse_env(raw), "raw": raw, "path": str(p)}


def _validate(updates):
    bad = [k for k in updates if k not in ALLOWED_KEYS]
    if bad:
        raise ValueError(f"keys not editable from the dashboard: {sorted(bad)} "
                         f"(allowed: {sorted(ALLOWED_KEYS)})")
    if "BRAIN" in updates and updates["BRAIN"] not in BRAINS:
        raise ValueError(f"BRAIN must be one of {sorted(BRAINS)}")
    if "SUPERVISE" in updates and updates["SUPERVISE"] not in ("auto", "off"):
        raise ValueError("SUPERVISE must be 'auto' or 'off'")
    for intk in ("INTERVAL_SECONDS", "CONTINUOUS_COOLDOWN", "TICK_TIMEOUT", "LOCAL_REQ_TIMEOUT",
                 "DELEGATION_MAX_CHARS"):
        if intk in updates:
            try:
                int(str(updates[intk]))
            except ValueError:
                raise ValueError(f"{intk} must be an integer")


def _snapshot_history(home):
    """Copy the current agent.env into home/state/config-history/<ts>.env before mutating."""
    p = _env_path(home)
    if not p.exists():
        return None
    hist = pathlib.Path(home) / "state" / "config-history"
    hist.mkdir(parents=True, exist_ok=True)
    # microsecond suffix so rapid back-to-back edits don't overwrite each other's snapshot
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{int(time.time() * 1e6) % 1_000_000:06d}"
    dst = hist / (stamp + ".env")
    dst.write_text(p.read_text(errors="ignore"))
    return str(dst)


def patch_agent_env(home, updates, agent="?"):
    """Atomically apply {KEY: VALUE} to agent.env. Preserves comments + key order; appends new
    keys at the end. Snapshots prior state for revert. Returns the diff [(key, old, new)]."""
    if not updates:
        return []
    _validate(updates)
    p = _env_path(home)
    old_env = parse_env(p.read_text(errors="ignore")) if p.exists() else {}
    diff = [(k, old_env.get(k), str(v)) for k, v in updates.items() if old_env.get(k) != str(v)]
    if not diff:
        return []
    _snapshot_history(home)

    lines = p.read_text(errors="ignore").splitlines() if p.exists() else []
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
    for k, v in remaining.items():   # new keys appended
        out.append(f"{k}={v}")

    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, p)               # atomic
    _audit("set-config", agent, ", ".join(f"{k}:{a or '∅'}→{b}" for k, a, b in diff))
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
