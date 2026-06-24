#!/usr/bin/env python3
"""spawn_watcher — turn dropped agent specs into running enclave deployments (the manager-spawns-agents
pattern, generically).

A MANAGER agent (e.g. an orchestrator) can only WRITE a spec into the watched queue — who can write is
decided by mount topology, so only the manager spawns. It never touches docker. This host-side watcher
(which CAN run docker) picks the spec up, runs `enclave new --image-only --spec` + `enclave run`, and
moves the spec to processed/ (or failed/ with a .error). Authorization = queue write access, by mounts.

Usage:
  spawn_watcher.py <queue-dir> [--interval SECONDS] [--stacks-root DIR] [--once]
    <queue-dir>     holds incoming/ processed/ failed/ (created if missing)
    --stacks-root   where new deployments are created (default $ENCLAVE_STACKS_ROOTS first entry, or ~/Dev)
    --interval      poll seconds (default 5)
    --once          process the current incoming specs once and exit (no loop)

Safe by construction: agent name must match ^[a-z0-9][a-z0-9_-]*$, the target must resolve directly
under the stacks root (no path escape), and an existing target is refused. Every action is appended to
~/.config/enclave/fleet-audit.log.
"""
import os, sys, re, json, time, pathlib, subprocess

REPO = pathlib.Path(__file__).resolve().parents[2]     # platform/agentd/ -> repo root
ENCLAVE = REPO / "bin" / "enclave"
SAFE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AUDIT = pathlib.Path.home() / ".config" / "enclave" / "fleet-audit.log"


def _audit(action, name, result, detail=""):
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with AUDIT.open("a") as f:
        f.write(json.dumps({"ts": ts, "who": "spawn_watcher", "action": action,
                            "agent": name, "result": result, "detail": detail}) + "\n")


def _load_name(spec_path):
    """Best-effort read of the spec's `name` (YAML or JSON); fall back to the file stem."""
    text = spec_path.read_text()
    try:
        if spec_path.suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        if isinstance(data, dict) and data.get("name"):
            return str(data["name"]).strip()
    except Exception:
        pass
    return spec_path.stem


def _process(spec_path, stacks_root, queue):
    name = _load_name(spec_path)
    proc, fail = queue / "processed", queue / "failed"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    def _fail(reason):
        dest = fail / f"{stamp}-{spec_path.name}"
        spec_path.rename(dest)
        (fail / f"{stamp}-{spec_path.stem}.error").write_text(reason + "\n")
        _audit("spawn", name, "failed", reason.splitlines()[0][:200])
        print(f"  ✗ {name}: {reason.splitlines()[0]}")

    if not SAFE.match(name or ""):
        return _fail(f"invalid agent name {name!r} (must match {SAFE.pattern})")
    target = (stacks_root / name).resolve()
    if target.parent != stacks_root.resolve():
        return _fail(f"target {target} is not directly under stacks root {stacks_root}")
    if target.exists() and any(target.iterdir()):
        return _fail(f"target {target} already exists and is non-empty")

    print(f"  → graduating {name} → {target}")
    new = subprocess.run([sys.executable, str(ENCLAVE), "new", name, "--dir", str(target),
                          "--image-only", "--spec", str(spec_path), "--yes"],
                         capture_output=True, text=True)
    if new.returncode != 0:
        return _fail("enclave new failed:\n" + (new.stderr or new.stdout))
    run = subprocess.run([sys.executable, str(ENCLAVE), "run", "--dir", str(target), "--no-build",
                          "--no-open"], capture_output=True, text=True)
    if run.returncode != 0:
        return _fail("enclave run failed (deployment created but not started):\n" + (run.stderr or run.stdout))

    spec_path.rename(proc / f"{stamp}-{spec_path.name}")
    _audit("spawn", name, "started", str(target))
    print(f"  ✓ {name} created + started")


def main():
    args = sys.argv[1:]
    pos = [a for a in args if not a.startswith("-")]
    if not pos:
        sys.exit(__doc__)
    queue = pathlib.Path(pos[0]).expanduser().resolve()
    interval = float(_flag(args, "--interval", "5"))
    stacks_root = pathlib.Path(_flag(args, "--stacks-root")
                               or os.environ.get("ENCLAVE_STACKS_ROOTS", str(pathlib.Path.home() / "Dev")).split(":")[0]
                               ).expanduser().resolve()
    for sub in ("incoming", "processed", "failed"):
        (queue / sub).mkdir(parents=True, exist_ok=True)
    once = "--once" in args
    print(f"spawn_watcher: queue={queue} stacks_root={stacks_root} interval={interval}s once={once}")

    while True:
        specs = sorted((queue / "incoming").glob("*"),
                       key=lambda p: p.stat().st_mtime)
        for s in specs:
            if s.is_file() and s.suffix in (".yaml", ".yml", ".json"):
                try:
                    _process(s, stacks_root, queue)
                except Exception as e:
                    _audit("spawn", s.stem, "error", str(e)[:200])
                    print(f"  ✗ {s.name}: {e}")
        if once:
            break
        time.sleep(interval)


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


if __name__ == "__main__":
    main()
