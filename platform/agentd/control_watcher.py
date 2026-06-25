#!/usr/bin/env python3
"""control_watcher — turn dropped control specs into pod lifecycle actions (the manager-controls-pods
pattern, mirror of spawn_watcher for lifecycle instead of creation).

A MANAGER agent (e.g. an orchestrator) can only WRITE a control spec into the watched queue — who can
write is decided by mount topology, so only the manager controls. It never touches docker. This host-side
watcher (which CAN run docker) picks the spec up, runs the requested lifecycle verb via `enclave fleet
<verb> <id>` (up|down|restart|kick|logs|send), and moves the spec to processed/ (or failed/ with a
.error). Authorization = queue write access, by mounts — identical trust model to spawn_watcher.

Why this exists: without it, only the operator can start/stop/restart pods, so a manager that finds a
sub-agent wedged has to escalate and wait. With it, the manager drops a one-line spec and the host acts.

Usage:
  control_watcher.py <queue-dir> [--interval SECONDS] [--once]
    <queue-dir>     holds incoming/ processed/ failed/ (created if missing)
    --interval      poll seconds (default 5)
    --once          process the current incoming specs once and exit (no loop)

Spec (YAML or JSON; one action per file dropped in incoming/):
    agent: stoneforge        # target agent id (or omit and name the file <id>.yaml)
    action: restart          # up | down | restart | kick | logs | send
    text: "resume the swap"  # required only for action: send
    requested_by: studio     # optional provenance, recorded in the audit log

Safe by construction: agent id must match ^[a-z0-9][a-z0-9_-]*$, action must be in the allowlist, and
the underlying `enclave fleet` verb re-validates the agent exists + its compose file is under an
allowlisted stacks root before touching docker. Every action is appended to
~/.config/enclave/fleet-audit.log (same log spawn_watcher and fleet.py write).
"""
import os, sys, re, json, time, pathlib, subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]     # platform/agentd/ -> repo root
ENCLAVE = REPO / "bin" / "enclave"
SAFE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ACTIONS = {"up", "down", "restart", "kick", "logs", "send"}
AUDIT = pathlib.Path.home() / ".config" / "enclave" / "fleet-audit.log"


def _audit(action, name, result, detail=""):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with AUDIT.open("a") as f:
        f.write(json.dumps({"ts": ts, "who": "control_watcher", "action": action,
                            "agent": name, "result": result, "detail": detail}) + "\n")


def _load_spec(spec_path):
    """Read the control spec (YAML or JSON). agent falls back to the file stem; returns (agent, action,
    text, requested_by)."""
    text = spec_path.read_text()
    data = {}
    try:
        if spec_path.suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text) or {}
        else:
            data = json.loads(text)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    agent = str(data.get("agent") or spec_path.stem).strip()
    action = str(data.get("action") or "").strip().lower()
    return agent, action, data.get("text", ""), str(data.get("requested_by") or "").strip()


def _process(spec_path, queue):
    agent, action, text, who = _load_spec(spec_path)
    proc, fail = queue / "processed", queue / "failed"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    def _fail(reason):
        dest = fail / f"{stamp}-{spec_path.name}"
        spec_path.rename(dest)
        (fail / f"{stamp}-{spec_path.stem}.error").write_text(reason + "\n")
        _audit(action or "?", agent, "failed", reason.splitlines()[0][:200])
        print(f"  ✗ {agent} {action}: {reason.splitlines()[0]}")

    if not SAFE.match(agent or ""):
        return _fail(f"invalid agent id {agent!r} (must match {SAFE.pattern})")
    if action not in ACTIONS:
        return _fail(f"invalid action {action!r} (must be one of {sorted(ACTIONS)})")
    if action == "send" and not str(text).strip():
        return _fail("action 'send' requires a non-empty 'text'")

    print(f"  → {agent}: {action}" + (f" ({who})" if who else ""))
    verb = [action, agent] + ([str(text)] if action == "send" else [])
    r = subprocess.run([sys.executable, str(ENCLAVE), "fleet", *verb],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return _fail(f"enclave fleet {action} {agent} failed:\n" + (r.stderr or r.stdout))

    spec_path.rename(proc / f"{stamp}-{spec_path.name}")
    _audit(action, agent, "done", who)
    print(f"  ✓ {agent} {action} done")


def main():
    args = sys.argv[1:]
    pos = [a for a in args if not a.startswith("-")]
    if not pos:
        sys.exit(__doc__)
    queue = pathlib.Path(pos[0]).expanduser().resolve()
    interval = float(_flag(args, "--interval", "5"))
    for sub in ("incoming", "processed", "failed"):
        (queue / sub).mkdir(parents=True, exist_ok=True)
    once = "--once" in args
    print(f"control_watcher: queue={queue} interval={interval}s once={once}")

    while True:
        specs = sorted((queue / "incoming").glob("*"),
                       key=lambda p: p.stat().st_mtime)
        for s in specs:
            if s.is_file() and s.suffix in (".yaml", ".yml", ".json"):
                try:
                    _process(s, queue)
                except Exception as e:
                    _audit("?", s.stem, "error", str(e)[:200])
                    print(f"  ✗ {s.name}: {e}")
        if once:
            break
        time.sleep(interval)


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


if __name__ == "__main__":
    main()
