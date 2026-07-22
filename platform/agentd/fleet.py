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
import calendar, os, sys, json, subprocess, pathlib, re, webbrowser, time

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


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _agent_env_interval(home):
    """INTERVAL_SECONDS lives in home/agent.env (runtime defaults), not the deployment .env."""
    try:
        for ln in (pathlib.Path(home) / "agent.env").read_text(errors="ignore").splitlines():
            if ln.startswith("INTERVAL_SECONDS="):
                return _int_or_none(ln.split("=", 1)[1])
    except Exception:
        pass
    return None


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


def _override_env(dep_dir):
    """Environment pinned in docker-compose.override.yml.

    The base .env only passes a handful of vars through, so per-agent brain wiring (BRAIN_MODEL,
    BRAIN_API_BASE, ...) is pinned in the override's `environment:` block. A console that reads
    only .env cannot see the brain the pod actually runs — on 2026-07-20 it displayed scoutpod
    as `qwen/qwen3-next-80b-a3b-instruct` (a stale MODEL line in .env) while the pod was really
    running anthropic/claude-sonnet-4.6, i.e. the dashboard reported the exact wrong-brain failure
    the operator had just paid to fix. Telemetry that lies about the brain is worse than none.

    Deliberately a line parser, not a YAML dep: this file stays dependency-free.
    """
    d = {}
    f = pathlib.Path(dep_dir) / "docker-compose.override.yml" if dep_dir else None
    if not (f and f.exists()):
        return d
    in_env = False
    for raw in f.read_text(errors="ignore").splitlines():
        ln = raw.strip()
        if ln.startswith("#") or not ln:
            continue
        if ln.startswith("environment:"):
            in_env = True
            continue
        if in_env:
            if not ln.startswith("- "):
                if ln.endswith(":"):      # next key at any level ends the block
                    in_env = False
                continue
            item = ln[2:].strip().strip('"').strip("'")
            if "=" in item:
                k, v = item.split("=", 1)
                # strip trailing inline comment, then quotes
                v = v.split("#", 1)[0].strip().strip('"').strip("'")
                d.setdefault(k.strip(), v)   # first occurrence wins, matching compose
    return d


def _model_of(env):
    """The model the pod ACTUALLY runs, resolved by brain.

    BRAIN=api reads BRAIN_MODEL — MODEL is inert for it, and is routinely left behind as stale
    cruft from an earlier brain. Preferring MODEL unconditionally is what made the console
    misreport scoutpod. For every other brain, MODEL remains authoritative.
    """
    if (env.get("BRAIN") or "").strip() == "api":
        return env.get("BRAIN_MODEL") or env.get("MODEL") or "?"
    return env.get("MODEL") or env.get("BRAIN_MODEL") or "?"


def _port(env):
    b = env.get("WEB_CHAT_BIND", "")
    if ":" in b:
        return b.rsplit(":", 1)[-1]
    return b or "8888"


PRODUCTIVITY_WINDOW_S = 7200


def _productivity(home, window_s=PRODUCTIVITY_WINDOW_S):
    """How much PRODUCT this agent wrote recently, read from the record the framework already keeps.

    Every tick appends {"writes": {"product": N, "tooling": N, ...}} to state/tick-scorecard.jsonl.
    Anything that wants to know whether a pod is producing must read THAT — the numbers are computed
    in-container by scorecard.py, where the agent's own globs resolve natively.

    Written because the studio had grown a second, host-side implementation that re-globbed the same
    question and got it wrong: container paths like /workspace and /work do not exist on the host, so
    three of four pods scored a permanent product=0 while producing normally, and one glob pattern
    walked node_modules and hung. None of that was possible from this file. `blind` is reported
    distinctly from zero — a pod with no scorecard record has not been measured, which is not the
    same as measured-and-idle, and conflating them is what makes a productivity backoff punish a
    working agent."""
    f = (home / "state" / "tick-scorecard.jsonl") if home else None
    if not f or not f.exists():
        return {"product": None, "tooling": None, "ticks": 0, "blind": True}
    cut = time.time() - window_s
    prod = tool = ticks = 0
    try:
        for line in f.read_text(errors="replace").splitlines()[-400:]:
            try:
                r = json.loads(line)
            except Exception:
                continue
            ts = _utc_epoch(r.get("ts"))
            if ts is None or ts < cut:
                continue
            w = r.get("writes") or {}
            prod += int(w.get("product") or 0)
            tool += int(w.get("tooling") or 0)
            ticks += 1
    except Exception:
        return {"product": None, "tooling": None, "ticks": 0, "blind": True}
    return {"product": prod, "tooling": tool, "ticks": ticks, "blind": False}


def _utc_epoch(ts):
    try:
        return calendar.timegm(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _loop_wait(home):
    """The loop's OWN last decision — continue / backoff / blocked / idle — and how long it waits.

    "idle" and "backing off to 4800s" look identical in a liveness badge but mean opposite things:
    one is the design, the other is the loop having decided this pod is not worth paying for."""
    try:
        lines = (home / "logs" / "runner.log").read_text(errors="ignore").splitlines()[-400:]
    except Exception:
        return {"kind": "", "wait_s": None}
    for l in reversed(lines):
        m = re.search(r"(backing off to (\d+)s|continue in (\d+)s|next tick in (\d+)s|idle|BLOCKED)", l)
        if not m:
            continue
        t = m.group(1)
        kind = ("backoff" if t.startswith("backing off") else "blocked" if t == "BLOCKED"
                else "idle" if t == "idle" else "continue")
        wait = next((int(g) for g in m.groups()[1:] if g and g.isdigit()), None)
        return {"kind": kind, "wait_s": wait}
    return {"kind": "", "wait_s": None}


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
    # tick liveness from runner.log: a long MAX_TURNS tick spews hundreds of lines, so a tiny tail can
    # miss the 'tick start' and falsely read 'idle' mid-work. Look back far + compare marker TIMESTAMPS
    # (leading ISO ts → lexicographic == chronological): working iff the latest start is after the
    # latest end/timeout (i.e. a tick is in progress).
    try:
        lines = (home / "logs" / "runner.log").read_text(errors="ignore").splitlines()[-1500:]
        last_start = last_end = ""
        for l in lines:
            if "tick start" in l:
                last_start = l[:20]
            elif "tick end" in l or "tick TIMED OUT" in l:
                last_end = l[:20]
        # "working" only while a tick is GENUINELY in progress: latest start newer than latest end AND
        # recent. A tick that dies WITHOUT writing "tick end" (crash / OOM / kill / container killed
        # mid-tick) leaves an orphaned "tick start" that would otherwise LATCH the badge to "working"
        # forever (the "shut it down but it says working for hours" bug). Require the start to be within
        # the max tick window (TICK_TIMEOUT + 10m grace); a stale unmatched start reads idle, not working.
        working = bool(last_start and last_start > last_end)
        if working:
            try:
                import calendar
                st_epoch = calendar.timegm(time.strptime(last_start[:19], "%Y-%m-%dT%H:%M:%S"))
                max_tick = int(os.environ.get("TICK_TIMEOUT", "2400")) + 600
                if (time.time() - st_epoch) > max_tick:
                    working = False   # orphaned/stale start — the tick died; nothing is running
            except Exception:
                pass
        s["tick"] = "working" if working else "idle"
    except Exception:
        pass
    # PAUSED is a distinct state, not a flavour of idle. runtime.sh skips every tick while
    # state/paused exists, so a paused pod is up, looping, and doing nothing — which rendered
    # identically to a healthy agent resting between ticks. forgepod sat like that for 15 days
    # (paused by a venture decision on 2026-07-04) while the console showed plain "idle", so
    # "deliberately stopped" and "waiting to work" were indistinguishable at a glance.
    try:
        if home and (home / "state" / "paused").exists():
            s["tick"] = "paused"
    except Exception:
        pass
    # Schedule-awareness (dashboard truth review T3, 2026-07-20): for a tick-based agent "Idle" is
    # the healthy steady state — the operator-relevant fact is WHEN it fires next, and whether it is
    # OVERDUE (loop wedged/dead while the container is up). Heartbeat = runtime.sh tick start.
    try:
        hb = home / "state" / ".heartbeat"
        s["hb_age_s"] = int(time.time() - hb.stat().st_mtime) if hb.exists() else None
    except Exception:
        s["hb_age_s"] = None
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
        env = {**env, **_override_env(row["dir"])}
        st = _state(home)
        m = man.get(name, {})
        running = "running" in (row["status"] or "").lower()
        agents[name] = {
            "id": name,
            "up": running,
            "status": row["status"],
            "brain": env.get("BRAIN", "?"),
            "model": _model_of(env),
            "port": _port(env),
            "chat_token": env.get("WEB_CHAT_TOKEN", ""),
            "dir": row["dir"],
            "configfile": row["configfile"],
            "home": str(home) if home else "",
            "manager": m.get("manager", ""),
            "tags": m.get("tags", []),
            "headline": st["headline"],
            "work_open": st["work_open"],
            "tick": (st["tick"] or "idle") if running else "down",
            "last_seen": st["last_seen"],
            "hb_age_s": st.get("hb_age_s"),
            "interval_s": _int_or_none(env.get("INTERVAL_SECONDS")) or _agent_env_interval(home),
            "subtitle": env.get("ENCLAVE_SUBTITLE", ""),
            "productivity": _productivity(home),
            "loop": _loop_wait(home) if home else {"kind": "", "wait_s": None},
        }
    # Folder-scan: surface DOWN / never-`up`'d deployments compose-ls can't see (incl. standalone
    # agents outside the main fleet root), marked down — so the console shows the whole fleet.
    for aid, dep in _scan_deployments().items():
        if aid in agents:
            continue
        env = {**_env(dep), **_override_env(dep)}
        home = pathlib.Path(dep) / "home"
        home = home if home.is_dir() else None
        st = _state(home)
        m = man.get(aid, {})
        agents[aid] = {
            "id": aid, "up": False, "status": "stopped",
            "brain": env.get("BRAIN", "?"), "model": _model_of(env),
            "port": _port(env), "chat_token": env.get("WEB_CHAT_TOKEN", ""), "dir": dep,
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
        pr = a.get("productivity") or {}
        # BLIND is not zero. A pod that has never been measured must not read as an idle one.
        prod = "prod:blind" if pr.get("blind") else f"prod:{pr.get('product', 0)}/{pr.get('ticks', 0)}t"
        lp = a.get("loop") or {}
        wait = lp.get("kind") or ""
        if wait == "backoff" and lp.get("wait_s"):
            wait = f"backoff:{lp['wait_s']}s"     # the loop has decided this pod is not worth paying for
        print(f"  {indent}{d} {a['id']:<22} {a['brain']:<9} {a['model']:<20} :{a['port']:<5} "
              f"work:{a['work_open']:<2} {prod:<12} {wait:<13} seen:{_fmt_age(a['last_seen']):<4} "
              f"{a['headline']}")
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
    """Print the applied config diff, then make it LIVE NOW by recreating the agent container.
    `docker compose restart` reuses the old container's environment, so it would NOT pick up the
    new .env/agent.env values until the agent's next natural tick (hours away on a slow heartbeat).
    `up -d --force-recreate` rebuilds the container with the fresh env and boots it straight into a
    new tick → the change applies immediately."""
    if not diff:
        print("no change (already set)"); return
    for k, old, new in diff:
        print(f"  {k}: {old or '∅'} → {new}")
    if not a.get("up"):
        print(f"{a['id']} is stopped — config saved; it will apply when you Start the agent.")
        return
    print(f"applying to {a['id']} now (recreating agent container) …")
    _compose(a, "up", "-d", "--force-recreate", "--no-deps", "agent")


def cmd_config(aid, as_json=False):
    """Show an agent's editable runtime config (agent.env)."""
    import fleet_config
    a = _resolve(aid)
    if not a.get("home"):
        sys.exit(f"{aid} has no home dir on this host — config not editable")
    cfg = fleet_config.read_config(a["home"])
    if as_json:
        print(json.dumps({"env": cfg["env"], "editable": cfg["editable"], "path": cfg["path"]})); return
    print(f"{aid}  ({cfg['path']})  ★=editable")
    for k in sorted(cfg["env"]):
        star = "★" if k in cfg["editable"] else " "
        print(f"  {star} {k:<22} {cfg['env'][k]}")


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
