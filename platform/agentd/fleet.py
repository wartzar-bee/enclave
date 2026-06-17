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
            "home": str(home) if home else "",
            "manager": m.get("manager", ""),
            "tags": m.get("tags", []),
            "headline": st["headline"],
            "work_open": st["work_open"],
            "tick": (st["tick"] or "idle") if running else "down",
            "last_seen": st["last_seen"],
        }
    return agents


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


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "list"
    if cmd == "list":
        cmd_list(as_json="--json" in args)
    elif cmd == "open" and len(args) > 1:
        cmd_open(args[1])
    else:
        sys.exit("usage: fleet.py list [--json] | fleet.py open <agent-id>")


if __name__ == "__main__":
    main()
