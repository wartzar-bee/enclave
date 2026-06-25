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
can't infer — notably a `manager` (the studio-agent → sub-agents hierarchy) the rail groups by.

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
    """The single source of truth: one dict {id: {...}} from disk reads. No backend HTTP calls.
    Sources: `docker compose ls` (running/known projects) + a folder-scan of the stacks roots, so
    DOWN / never-started deployments (e.g. a standalone agent that isn't up) still appear, marked down."""
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
    # Folder-scan: surface DOWN / never-`up`'d deployments compose-ls can't see (marked down).
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
    return agents


def _is_deployment(d):
    d = pathlib.Path(d)
    return (d / "docker-compose.yml").is_file() and (d / ".env").is_file()


def _scan_deployments():
    """Folder-scan the stacks roots for enclave deployments `docker compose ls` misses (down or never
    started). A root may be a PARENT of deployments, or a deployment dir itself (a standalone agent).
    Returns {agent_id: dir}."""
    out = {}
    for root in STACKS_ROOTS:
        root = pathlib.Path(root)
        try:
            cands = [root] if _is_deployment(root) else [c for c in root.iterdir() if c.is_dir()]
        except Exception:
            continue
        for c in cands:
            if not _is_deployment(c):
                continue
            aid = _env(str(c)).get("AGENT_ID") or pathlib.Path(c).name
            if _SAFE.match(aid):
                out.setdefault(aid, str(c))
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
    cmd = ["docker", "compose", "-f", cfg, "--project-directory", a["dir"], *verb]
    _audit(verb[0], a["id"], " ".join(verb[1:]))
    return subprocess.run(cmd, timeout=timeout)


def cmd_up(aid):
    a = _resolve(aid); print(f"starting {aid} …"); _compose(a, "up", "-d")
def cmd_down(aid):
    a = _resolve(aid); print(f"stopping {aid} …"); _compose(a, "stop")
def cmd_restart(aid):
    a = _resolve(aid); print(f"restarting {aid} …"); _compose(a, "restart")
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


def _parse_req(path):
    """Tiny stdlib parser for a control request file (`key: value` per line — a YAML/JSON subset).
    Handles both `agent: x` lines and a flat JSON object. No yaml dependency (enclave is stdlib-only)."""
    try:
        txt = pathlib.Path(path).read_text()
    except Exception:
        return {}
    s = txt.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            return {k: str(v) for k, v in d.items()}
        except Exception:
            pass
    d = {}
    for ln in txt.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or ":" not in ln:
            continue
        k, v = ln.split(":", 1)
        d[k.strip()] = v.strip().strip('"').strip("'")
    return d


_CONTROL_ACTIONS = {"up": cmd_up, "start": cmd_up, "down": cmd_down, "stop": cmd_down,
                    "restart": cmd_restart}


def cmd_control_watch(control_dir, poll=5):
    """Host-side LIFECYCLE watcher — how the studio (guard-blocked from docker) actually kickstarts
    agents. It drops a request into <control_dir>/incoming/<name>.yaml ({agent, action: up|down|restart|
    kick, requested_by}); this loop executes it via the SAME validated + audited helpers as the CLI and
    moves the file to processed/ or failed/. Authorization is mount topology: only the studio has this
    dir mounted rw. Runs on the host (needs docker); the agent never touches docker."""
    base = pathlib.Path(control_dir)
    inc, done, fail = base / "incoming", base / "processed", base / "failed"
    for d in (inc, done, fail):
        d.mkdir(parents=True, exist_ok=True)
    print(f"[control-watch] watching {inc} (poll {poll}s) — actions: {', '.join(sorted(_CONTROL_ACTIONS))}", flush=True)
    while True:
        files = sorted(inc.glob("*.yml")) + sorted(inc.glob("*.yaml")) + sorted(inc.glob("*.json"))
        for f in files:
            req = _parse_req(f)
            aid = req.get("agent", "")
            action = (req.get("action", "") or "").lower()
            ok, msg = False, ""
            try:
                if action not in _CONTROL_ACTIONS:
                    raise ValueError(f"unknown action '{action}' (allowed: {', '.join(sorted(_CONTROL_ACTIONS))})")
                if not _SAFE.match(aid):
                    raise ValueError(f"bad agent id '{aid}'")
                _CONTROL_ACTIONS[action](aid)        # validated (_resolve/_allowed_stack) + audited inside
                ok = True
            except SystemExit as e:                  # cmd_* sys.exit on a bad target — capture, never die
                msg = str(e)
            except Exception as e:
                msg = str(e)
            try:
                f.rename((done if ok else fail) / f.name)
            except Exception:
                pass
            _audit("control:" + (action or "?"), aid or "?",
                   ("ok" if ok else "FAIL " + msg) + f" by={req.get('requested_by', '?')}")
            print(f"[control-watch] {action} {aid} -> {'OK' if ok else 'FAIL: ' + msg}", flush=True)
        time.sleep(poll)


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
    elif cmd == "logs" and pos:
        cmd_logs(pos[0], _flag(args, "--tail", "80"))
    elif cmd == "send" and len(pos) >= 2:
        cmd_send(pos[0], " ".join(pos[1:]))
    elif cmd in ("control-watch", "watch"):
        d = pos[0] if pos else (str(STACKS_ROOTS[0] / "_control") if STACKS_ROOTS else "_control")
        cmd_control_watch(d, int(_flag(args, "--poll", "5") or 5))
    else:
        sys.exit("usage: fleet.py list [--json] | open|up|down|restart|kick|logs <id> | send <id> <text> | control-watch [dir]")


def _flag(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


if __name__ == "__main__":
    main()
