#!/usr/bin/env python3
"""
fleet.py — Enclave fleet control plane (P1: discover + snapshot + list/open).

The ONE load-bearing rule (FLEET-CONSOLE-PLAN.md v2): agent state is read by ONE place that builds a
cached snapshot from DIRECT DISK READS — never N synchronous backend calls per consumer. For the CLI a
snapshot is built once per invocation (cheap); the web console (P2) will run this builder on a single
background thread feeding SSE.

Discovery (observed state, NOT identity): `docker compose ls --format json` enumerates every deployment
(project = AGENT_ID, with its ConfigFile). The agent's HOME (its /agent brain dir, which may be mounted
in-place from elsewhere) is read authoritatively from `docker inspect` of the agent container, falling
back to <deployment>/home. An optional manifest (~/.config/enclave/fleet.json) adds identity the runtime
can't infer — notably a `manager` (the master-agent → sub-agents hierarchy) the rail groups by.

Stdlib only. Runs on the host (needs the docker CLI). Lifecycle (up/down) lands in P1b via a fleetctl
privilege helper; this module is read-only + open-in-browser.

Usage: fleet.py list [--json] | fleet.py open <agent-id>
"""
import os, sys, json, subprocess, pathlib, re, webbrowser, time

MANIFEST = pathlib.Path(os.environ.get("ENCLAVE_FLEET_MANIFEST",
                        pathlib.Path.home() / ".config" / "enclave" / "fleet.json"))
_SAFE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _docker(*args, timeout=8):
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _compose_ls():
    """Every compose project docker knows about → [{name, status, configfile, dir}]. Label-derived, so it
    misses never-`up`'d deployments (folder-scan covers that later); de-dupe by resolved ConfigFile."""
    out = _docker("compose", "ls", "--all", "--format", "json")
    rows, seen = [], set()
    try:
        for p in json.loads(out or "[]"):
            cfg = (p.get("ConfigFiles") or "").split(",")[0].strip()
            key = str(pathlib.Path(cfg).resolve()) if cfg else p.get("Name", "")
            if key in seen:
                continue
            seen.add(key)
            rows.append({"name": p.get("Name", ""), "status": p.get("Status", ""),
                         "configfile": cfg, "dir": str(pathlib.Path(cfg).parent) if cfg else ""})
    except Exception:
        pass
    return rows


def _manifest():
    try:
        return json.loads(MANIFEST.read_text()).get("agents", {})
    except Exception:
        return {}


def _agent_home(name, dep_dir):
    """Authoritative /agent host path from `docker inspect` (handles mount-in-place); else <dir>/home."""
    out = _docker("inspect", name, "--format",
                  '{{range .Mounts}}{{if eq .Destination "/agent"}}{{.Source}}{{end}}{{end}}')
    src = (out or "").strip()
    if src and pathlib.Path(src).is_dir():
        return pathlib.Path(src)
    h = pathlib.Path(dep_dir) / "home" if dep_dir else None
    return h if (h and h.is_dir()) else None


def _env(dep_dir):
    """Parse a deployment .env for the bits the console needs (defensive: host:port, quotes, comments)."""
    d = {}
    f = pathlib.Path(dep_dir) / ".env" if dep_dir else None
    if not (f and f.exists()):
        return d
    for ln in f.read_text(errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d


def _port(env):
    b = env.get("WEB_CHAT_BIND", "")
    if ":" in b:
        return b.rsplit(":", 1)[-1]
    return b or "8888"


def _state(home):
    """Cheap disk read of the agent's brain: headline + open work + tick liveness. mtime-gated reads."""
    s = {"headline": "", "work_open": 0, "tick": "", "last_seen": 0}
    if not home:
        return s
    roll = home / "state" / "rollup.md"
    try:
        s["last_seen"] = roll.stat().st_mtime
        s["headline"] = next((l.strip() for l in roll.read_text(errors="ignore").splitlines()
                              if l.strip() and not l.startswith("#")), "")[:80]
    except Exception:
        pass
    try:
        wk = json.loads((home / "work.json").read_text())
        s["work_open"] = sum(1 for w in wk if isinstance(w, dict) and w.get("status") in ("todo", "doing"))
    except Exception:
        pass
    # tick liveness from runner.log tail
    try:
        tail = (home / "logs" / "runner.log").read_text(errors="ignore").splitlines()[-12:]
        starts = [l for l in tail if "tick start" in l]
        ends = [l for l in tail if "tick end" in l]
        s["tick"] = "working" if (starts and (not ends or tail.index(starts[-1]) > tail.index(ends[-1]))) else "idle"
    except Exception:
        pass
    return s


def snapshot():
    """The single source of truth: one dict {id: {...}} from disk reads. No backend HTTP calls."""
    man = _manifest()
    agents = {}
    for row in _compose_ls():
        name = row["name"]
        # heuristic: an enclave agent project has an agent container + a web-chat sibling
        env = _env(row["dir"])
        home = _agent_home(name, row["dir"])
        if not home and "enclave" not in name and name not in man:
            continue   # skip non-enclave compose projects we can't resolve a brain for
        st = _state(home)
        m = man.get(name, {})
        running = "running" in (row["status"] or "").lower()
        agents[name] = {
            "id": name,
            "up": running,
            "status": row["status"],
            "brain": env.get("BRAIN", "?"),
            "model": env.get("MODEL") or env.get("BRAIN_MODEL", "?"),
            "port": _port(env),
            "dir": row["dir"],
            "configfile": row["configfile"],
            "home": str(home) if home else "",
            "manager": m.get("manager", ""),
            "tags": m.get("tags", []),
            "headline": st["headline"],
            "work_open": st["work_open"],
            "tick": (st["tick"] or "idle") if running else "down",
            "last_seen": st["last_seen"],
        }
    # Folder-scan: surface DOWN / never-`up`'d deployments compose-ls can't see (incl. standalone
    # agents outside the main fleet root), marked down — so the console shows the whole fleet.
    for aid, dep in _scan_deployments().items():
        if aid in agents:
            continue
        env = _env(dep)
        home = pathlib.Path(dep) / "home"
        home = home if home.is_dir() else None
        st = _state(home)
        m = man.get(aid, {})
        agents[aid] = {
            "id": aid, "up": False, "status": "stopped",
            "brain": env.get("BRAIN", "?"), "model": env.get("MODEL") or env.get("BRAIN_MODEL", "?"),
            "port": _port(env), "dir": dep,
            "configfile": str(pathlib.Path(dep) / "docker-compose.yml"),
            "home": str(home) if home else "",
            "manager": m.get("manager", ""), "tags": m.get("tags", []),
            "headline": st["headline"], "work_open": st["work_open"],
            "tick": "down", "last_seen": st["last_seen"],
        }
    # Auto-classify standalone vs fleet from the manager hierarchy (no config): an agent is part of a
    # FLEET if it has a manager OR is itself a manager of someone; otherwise it runs STANDALONE
    # (its own independent enclave, not wired into any master→sub-agents tree).
    managers = {a["manager"] for a in agents.values() if a.get("manager")}
    for aid, a in agents.items():
        a["kind"] = "fleet" if (a.get("manager") or aid in managers) else "standalone"
    return agents


# Dirs the recursive scan never descends into (vcs/vendor/build noise + anything that looks like a
# backup/archive copy — those hold stale duplicate deployments we must not surface as live agents).
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "env", ".cache",
              "dist", "build", ".next", "site-packages", ".terraform", "vendor"}


def _is_deployment(d):
    d = pathlib.Path(d)
    return (d / "docker-compose.yml").is_file() and (d / ".env").is_file()


def _is_enclave_deployment(d):
    """A compose deployment that is specifically an ENCLAVE agent — has compose + .env AND an enclave
    marker (AGENT_ID in .env, or `enclave` referenced in the compose file). The marker is what lets us
    scan a broad root (e.g. ~/Dev) for ANY enclave session without dragging in unrelated compose projects."""
    d = pathlib.Path(d)
    if not _is_deployment(d):
        return False
    if _env(str(d)).get("AGENT_ID"):
        return True
    try:
        return "enclave" in (d / "docker-compose.yml").read_text(errors="ignore").lower()
    except Exception:
        return False


_scan_cache = {"ts": 0.0, "data": {}}


def _scan_deployments(max_depth=4, ttl=30.0):
    """Auto-discover enclave deployments on disk under STACKS_ROOTS, at ANY depth (bounded) — so the
    console finds standalone agents AND never-`up`'d fleet members with no per-dir
    config, just a search root (default ~/Dev). Marker-gated + skips vcs/vendor/backup dirs so a broad
    root stays clean; de-dupes by AGENT_ID (first match wins). {id: dir}.

    TTL-cached: the snapshot loop runs every few seconds but the on-disk deployment set changes rarely,
    so a full recursive walk per tick is wasteful — serve a recent scan (default 30s)."""
    now = time.time()
    if _scan_cache["data"] and (now - _scan_cache["ts"]) < ttl:
        return dict(_scan_cache["data"])
    out = {}

    def walk(d, depth):
        if depth > max_depth:
            return
        try:
            if _is_enclave_deployment(d):
                aid = _env(str(d)).get("AGENT_ID") or pathlib.Path(d).name
                if _SAFE.match(aid):
                    out.setdefault(aid, str(d))
                return   # a deployment is a leaf — don't descend into its home/state trees
            for c in sorted(pathlib.Path(d).iterdir()):
                if c.is_dir() and c.name not in _SKIP_DIRS and not c.name.startswith(".") \
                        and "backup" not in c.name.lower():
                    walk(c, depth + 1)
        except Exception:
            return

    for root in STACKS_ROOTS:
        walk(pathlib.Path(root), 0)
    _scan_cache.update(ts=now, data=out)
    return out



AUDIT = pathlib.Path(os.environ.get("ENCLAVE_FLEET_AUDIT",
                     pathlib.Path.home() / ".config" / "enclave" / "fleet-audit.log"))
STACKS_ROOTS = [pathlib.Path(p).expanduser().resolve()
                for p in os.environ.get("ENCLAVE_STACKS_ROOTS", str(pathlib.Path.home() / "Dev")).split(":") if p]


def _audit(action, target, extra=""):
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {action} {target} {extra}\n".rstrip() + "\n")
    except Exception:
        pass


def _resolve(aid):
    if not _SAFE.match(aid or ""):
        sys.exit(f"invalid agent id '{aid}'")
    a = snapshot().get(aid)
    if not a:
        sys.exit(f"unknown agent '{aid}' (see `enclave fleet list`)")
    return a


def _allowed_stack(cfg):
    """The compose file must live under an allowlisted stacks root — never run arbitrary compose files."""
    try:
        r = pathlib.Path(cfg).resolve()
        return any(str(r).startswith(str(root) + os.sep) for root in STACKS_ROOTS) and r.is_file()
    except Exception:
        return False


def _compose(a, *verb, timeout=180):
    """docker compose -f <ConfigFile> --project-directory <dir> <verb> — the M1-correct addressing
    (project name is baked via `name:`; -p won't reach these stacks). Validated + audited."""
    cfg = a.get("configfile", "")
    if not cfg or not _allowed_stack(cfg):
        sys.exit(f"refusing: {a['id']}'s compose file is missing or outside ENCLAVE_STACKS_ROOTS")
    # Passing an explicit `-f` disables Compose's automatic merge of docker-compose.override.yml, so
    # include it ourselves when present (standard Compose convention) — otherwise a CLI/dashboard
    # up/restart silently drops override-only mounts (e.g. studio host-mounted tools/knowledge).
    cmd = ["docker", "compose", "-f", cfg]
    override = pathlib.Path(cfg).with_name("docker-compose.override.yml")
    if override.is_file():
        cmd += ["-f", str(override)]
    cmd += ["--project-directory", a["dir"], *verb]
    _audit(verb[0], a["id"], " ".join(verb[1:]))
    return subprocess.run(cmd, timeout=timeout)


def cmd_up(aid):
    a = _resolve(aid); print(f"starting {aid} …"); _compose(a, "up", "-d")
def cmd_down(aid):
    a = _resolve(aid); print(f"stopping {aid} …"); _compose(a, "stop")
def cmd_restart(aid):
    a = _resolve(aid); print(f"restarting {aid} …"); _compose(a, "restart")
def cmd_kick(aid):
    """Wake the agent to tick NOW: restart only its `agent` service, leaving web-chat/relay up. Lighter
    than `restart` (which bounces the whole stack) — the brain re-enters its loop on container boot."""
    a = _resolve(aid); print(f"kicking {aid} (agent service) …"); _compose(a, "restart", "agent")
def cmd_logs(aid, tail="80"):
    a = _resolve(aid); _compose(a, "logs", "--tail", str(tail), timeout=30)


def cmd_send(aid, text):
    """Operator directive → the agent. Try the comms bridge (wakes the tick); fall back to inbox.md."""
    a = _resolve(aid)
    if not (text or "").strip():
        sys.exit("empty directive")
    env = _env(a["dir"])
    url = env.get("COMMS_URL", "")
    sent = False
    if url:
        # resolve the comms token from the deployment's mounted secrets
        tok = ""
        sf = pathlib.Path(a["dir"]) / "secrets" / "comms-bridge.env"
        try:
            for ln in sf.read_text().splitlines():
                if "TOKEN=" in ln and not ln.startswith("#"):
                    tok = ln.split("=", 1)[1].strip(); break
        except Exception:
            pass
        try:
            import urllib.request
            body = json.dumps({"agent": aid, "from": "operator", "text": text}).encode()
            req = urllib.request.Request(url.rstrip("/") + "/send", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "X-Comms-Token": tok})
            urllib.request.urlopen(req, timeout=8)
            sent = True
        except Exception as e:
            print(f"  (comms send failed: {e}; falling back to inbox)")
    if not sent and a["home"]:
        try:
            with (pathlib.Path(a["home"]) / "inbox.md").open("a") as f:
                f.write(f"\n- [ ] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} — {text}\n")
            sent = True
        except Exception as e:
            sys.exit(f"could not deliver directive: {e}")
    _audit("send", aid, text[:80])
    print(f"directive → {aid} ({'comms (live)' if url and sent else 'inbox (next tick)'})")


def _fmt_age(ts):
    if not ts:
        return "—"
    d = time.time() - ts
    if d < 90:
        return f"{int(d)}s"
    if d < 5400:
        return f"{int(d/60)}m"
    return f"{int(d/3600)}h"


def cmd_list(as_json=False):
    snap = snapshot()
    if as_json:
        print(json.dumps(snap, indent=2)); return
    if not snap:
        print("No Enclave deployments found (docker compose ls returned none)."); return
    # group by manager (hierarchy), then standalone — rail-style, not a flat bloating table
    by_mgr = {}
    for a in snap.values():
        by_mgr.setdefault(a["manager"] or "", []).append(a)
    dot = {"working": "●", "idle": "○", "down": "✗"}
    def line(a, indent=""):
        d = dot.get(a["tick"], "?")
        print(f"  {indent}{d} {a['id']:<22} {a['brain']:<9} {a['model']:<20} :{a['port']:<5} "
              f"work:{a['work_open']:<2} seen:{_fmt_age(a['last_seen']):<4} {a['headline']}")
    standalone = by_mgr.pop("", [])
    for mgr, subs in by_mgr.items():
        print(f"▸ {mgr} (manager)")
        for a in sorted(subs, key=lambda x: x["id"]):
            line(a, "  ")
    if standalone:
        if by_mgr:
            print("▸ standalone")
        for a in sorted(standalone, key=lambda x: x["id"]):
            line(a)
    print(f"\n  {len(snap)} agent(s)   ● working  ○ idle  ✗ down")


def _restart_after(a, diff):
    """Print the applied config diff, then restart the stack so the change takes effect."""
    if not diff:
        print("no change (already set)"); return
    for k, old, new in diff:
        print(f"  {k}: {old or '∅'} → {new}")
    print(f"restarting {a['id']} to apply …")
    _compose(a, "restart")


def cmd_config(aid, as_json=False):
    """Show an agent's editable runtime config (agent.env)."""
    import fleet_config
    a = _resolve(aid)
    if not a.get("home"):
        sys.exit(f"{aid} has no home dir on this host — config not editable")
    cfg = fleet_config.read_config(a["home"])
    if as_json:
        print(json.dumps(cfg["env"], indent=2)); return
    print(f"{aid}  ({cfg['path']})")
    for k in sorted(cfg["env"]):
        print(f"  {k:<22} {cfg['env'][k]}")


def cmd_set_config(aid, kvs):
    """Patch one or more KEY=VALUE pairs in agent.env, then restart."""
    import fleet_config
    a = _resolve(aid)
    if not a.get("home"):
        sys.exit(f"{aid} has no home dir on this host")
    updates = {}
    for kv in kvs:
        if "=" not in kv:
            sys.exit(f"expected KEY=VALUE, got '{kv}'")
        k, v = kv.split("=", 1); updates[k.strip()] = v.strip()
    try:
        diff = fleet_config.patch_agent_env(a["home"], updates, aid)
    except ValueError as e:
        sys.exit(str(e))
    _restart_after(a, diff)


def cmd_set_brain(aid, brain, model=None):
    import fleet_config
    a = _resolve(aid)
    if not a.get("home"):
        sys.exit(f"{aid} has no home dir on this host")
    try:
        diff = fleet_config.set_brain(a["home"], brain, model, aid)
    except ValueError as e:
        sys.exit(str(e))
    _restart_after(a, diff)


def cmd_set_mode(aid, mode, interval=None):
    import fleet_config
    a = _resolve(aid)
    if not a.get("home"):
        sys.exit(f"{aid} has no home dir on this host")
    try:
        diff = fleet_config.set_mode(a["home"], mode, interval, aid)
    except ValueError as e:
        sys.exit(str(e))
    _restart_after(a, diff)


def cmd_preset(aid, name):
    import fleet_config
    a = _resolve(aid)
    if not a.get("home"):
        sys.exit(f"{aid} has no home dir on this host")
    try:
        diff = fleet_config.apply_preset(a["home"], name, aid)
    except ValueError as e:
        sys.exit(str(e))
    _restart_after(a, diff)


def cmd_open(aid):
    a = snapshot().get(aid)
    if not a:
        sys.exit(f"unknown agent '{aid}' (see `enclave fleet list`)")
    url = f"http://127.0.0.1:{a['port']}/"
    print(f"opening {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "list"
    pos = [a for a in args[1:] if not a.startswith("-")]
    if cmd == "list":
        cmd_list(as_json="--json" in args)
    elif cmd == "open" and pos:
        cmd_open(pos[0])
    elif cmd in ("up", "start") and pos:
        cmd_up(pos[0])
    elif cmd in ("down", "stop") and pos:
        cmd_down(pos[0])
    elif cmd == "restart" and pos:
        cmd_restart(pos[0])
    elif cmd == "kick" and pos:
        cmd_kick(pos[0])
    elif cmd == "logs" and pos:
        cmd_logs(pos[0], _flag(args, "--tail", "80"))
    elif cmd == "send" and len(pos) >= 2:
        cmd_send(pos[0], " ".join(pos[1:]))
    elif cmd == "config" and pos:
        cmd_config(pos[0], as_json="--json" in args)
    elif cmd == "set-config" and len(pos) >= 2:
        cmd_set_config(pos[0], pos[1:])
    elif cmd == "set-brain" and len(pos) >= 2:
        cmd_set_brain(pos[0], pos[1], pos[2] if len(pos) > 2 else None)
    elif cmd == "set-mode" and len(pos) >= 2:
        cmd_set_mode(pos[0], pos[1], pos[2] if len(pos) > 2 else None)
    elif cmd == "preset" and len(pos) >= 2:
        cmd_preset(pos[0], pos[1])
    else:
        sys.exit("usage: fleet.py list [--json] | open|up|down|restart|kick|logs <id> | send <id> <text>\n"
                 "       config <id> [--json] | set-config <id> KEY=VAL… | set-brain <id> <brain> [model]\n"
                 "       set-mode <id> <autonomous|chat|scheduled> [interval] | preset <id> <name>")


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


if __name__ == "__main__":
    main()
