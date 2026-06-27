"""Shared, dependency-free test helpers for the Enclave fleet console + control plane.

Hermetic by design: builds a fake fleet on disk under a temp root, points the code at it via
ENCLAVE_STACKS_ROOTS, and (for integration tests) boots the real console HTTP server on an ephemeral
port with no docker required — fleet.snapshot() degrades to a pure disk scan when the docker CLI returns
nothing, so every discovered deployment shows up marked "down" and every read-only endpoint works.

No pytest, no third-party deps — matches the framework's plain-stdlib style. Import this from a
test_*.py; pair it with the tiny check() harness below (or your own).

Typical use:
    import tests_fixtures as F
    root = F.build_fleet(specs=[{"id": "alpha"}, {"id": "beta", "brain": "api"}])
    con, base, stop = F.boot_console(root)
    try:
        code, body = F.get(base, "/api/fleet")
        ...
    finally:
        stop()
"""
import json
import os
import pathlib
import socket
import tempfile
import threading
import time
import urllib.request
import urllib.error


# --------------------------------------------------------------------------- tiny assert harness
class Check:
    """Minimal pass/fail tracker so a test file can `from tests_fixtures import Check; check = Check()`
    and end with `raise SystemExit(check.report())`. Mirrors the ad-hoc harnesses already in the repo."""

    def __init__(self):
        self.passed = 0
        self.failed = 0

    def __call__(self, name, cond, detail=""):
        if cond:
            self.passed += 1
        else:
            self.failed += 1
            extra = f"  ({detail})" if detail else ""
            print(f"  FAIL: {name}{extra}")
        return bool(cond)

    def eq(self, name, got, want):
        return self(name, got == want, f"got {got!r}, want {want!r}")

    def report(self):
        print()
        if self.failed:
            print(f"{self.failed} FAILED, {self.passed} passed")
            return 1
        print(f"OK {self.passed} passed, 0 failed")
        return 0


# --------------------------------------------------------------------------- fleet fixture builder
# 3 ticks, deliberately spread across the week by build_fleet (one ~1h ago, one ~3d ago, one ~6.5d ago)
# so the today/wtd/7d window-cutoff logic is actually exercised — with all ticks in one window the
# windows are indistinguishable and the cutoff code is unverified (external review).
_USAGE_DEFAULT = [
    {"reason": "heartbeat", "model": "claude-sonnet-4-6", "input": 500, "output": 1200,
     "cache_read": 900000, "cache_write": 12000, "cost_usd": 1.1, "duration_s": 42, "turns": 18,
     "rc": 0, "subtype": "success"},
    {"reason": "continue", "model": "claude-sonnet-4-6", "input": 480, "output": 1300,
     "cache_read": 1100000, "cache_write": 9000, "cost_usd": 1.4, "duration_s": 51, "turns": 22,
     "rc": 0, "subtype": "success"},
    {"reason": "continue", "model": "claude-sonnet-4-6", "input": 510, "output": 1100,
     "cache_read": 1000000, "cache_write": 10000, "cost_usd": 1.2, "duration_s": 47, "turns": 20,
     "rc": 0, "subtype": "success"},
]
# hours-ago for each tick when a spec doesn't pin its own ts: newest .. oldest, spanning ~6.5 days.
_TICK_AGES_H = [1.0, 72.0, 156.0]


def build_fleet(specs=None, root=None):
    """Create a temp fleet root populated with one deployment dir per spec. Returns the root path (str).

    Each spec is a dict (all keys optional except a unique id):
      id (str, required-ish; defaults alpha/beta/…), brain, model, manager, tags (list),
      headline (rollup head), work (list of {id,text,status}), usage (list of tick dicts),
      ext_spend (list of {model,usd}), cap ({five_hour:{pct},seven_day:{pct}}),
      events (list of {tool,summary}), running (bool — only affects nothing here; docker is absent).
    """
    if root is None:
        root = tempfile.mkdtemp(prefix="enclave-fleet-")
    rootp = pathlib.Path(root)
    rootp.mkdir(parents=True, exist_ok=True)
    specs = specs or [{"id": "alpha"}, {"id": "beta"}]
    now = time.time()
    for i, spec in enumerate(specs):
        aid = spec.get("id") or f"agent{i}"
        brain = spec.get("brain", "claude")
        model = spec.get("model", "claude-sonnet-4-6")
        dep = rootp / aid
        home = dep / "home"
        st = home / "state"
        logs = home / "logs"
        for d in (st, logs):
            d.mkdir(parents=True, exist_ok=True)

        # compose + env: the markers fleet._is_enclave_deployment() requires
        (dep / "docker-compose.yml").write_text(
            "services:\n  agent:\n    image: enclave-agent:latest\n")
        env_lines = [f"AGENT_ID={aid}", f"BRAIN={brain}", f"MODEL={model}",
                     "CHAT_PORT=8" + str(900 + i).zfill(3), "WORK_DIR=/work"]
        for extra in ("BRAIN_API_BASE", "BRAIN_API_KEY_ENV", "ESCALATION_MODEL"):
            if extra in spec:
                env_lines.append(f"{extra}={spec[extra]}")
        (dep / ".env").write_text("\n".join(env_lines) + "\n")
        # agent.env is what fleet_config reads/writes (config plane)
        (home / "agent.env").write_text("\n".join(env_lines + [
            "INTERVAL_SECONDS=10800", "CONTINUOUS_COOLDOWN=600", "SUPERVISE=auto"]) + "\n")

        # brain state files
        head = spec.get("headline", f"{aid} is working on something")
        (st / "rollup.md").write_text(f"# {aid} rollup\n{head}\n\nmore detail here.\n")
        work = spec.get("work", [{"id": "w1", "text": "do the thing", "status": "doing"},
                                 {"id": "w2", "text": "next thing", "status": "todo"},
                                 {"id": "w3", "text": "old thing", "status": "done"}])
        (home / "work.json").write_text(json.dumps(work))
        (st / "tick-status.json").write_text(json.dumps({"status": "continue", "waiting_on": ""}))
        (st / "activity.log").write_text("12:00 started\n12:05 progressed\n")
        (home / "inbox.md").write_text(spec.get("inbox", f"# {aid} inbox\n"))

        # runner.log: a started-but-not-ended tick => "working"
        (logs / "runner.log").write_text(
            f"2026-06-27T12:00:00Z tick start\n2026-06-27T12:00:42Z tick end\n"
            f"2026-06-27T12:30:00Z tick start\n")

        # events.jsonl: real tool events + noise (to test filtering)
        events = spec.get("events", [{"tool": "Edit", "summary": "edit frontend/game.js"},
                                     {"tool": "Bash", "summary": "npm run build"},
                                     {"tool": "Write", "summary": "write /agent/state/rollup.md"}])
        with (st / "events.jsonl").open("w") as f:
            for j, e in enumerate(events):
                f.write(json.dumps({"ts": now - 60 + j, "event": "tool",
                                    "tool": e.get("tool"), "summary": e.get("summary")}) + "\n")

        # usage.jsonl with relative timestamps over the last week. Production writes ISO-8601 'Z'
        # strings and usage._parse_ts accepts ONLY that form (a numeric epoch parses to None and the
        # tick is dropped from every window) — and the dashboard JS calls .slice on ts — so the
        # fixture must mirror that shape, or the cost/overview/diagnostics paths test empty data.
        usage = spec.get("usage", _USAGE_DEFAULT)
        n = len(usage)
        with (st / "usage.jsonl").open("w") as f:
            for j, rec in enumerate(usage):
                rec = dict(rec)
                # spread across ~6.5 days (newest first) so today/wtd/7d differ; honor an explicit ts.
                age_h = _TICK_AGES_H[j] if (usage is _USAGE_DEFAULT and j < len(_TICK_AGES_H)) \
                    else 1.0 + (155.0 * j / max(1, n - 1))
                rec.setdefault("ts", _iso(now - age_h * 3600))
                f.write(json.dumps(rec) + "\n")

        # external api spend
        if spec.get("ext_spend"):
            with (st / "api_spending.jsonl").open("w") as f:
                for e in spec["ext_spend"]:
                    f.write(json.dumps({"ts": now - 100, "model": e.get("model", "x"),
                                        "tokens": e.get("tokens", 1000), "usd": e.get("usd", 0.01)}) + "\n")

        if spec.get("cap"):
            (st / "claude-usage.json").write_text(json.dumps({**spec["cap"], "ts": now}))

    return str(rootp)


def _iso(epoch):
    """Epoch seconds -> ISO-8601 'Z' UTC string, the shape production agents write to usage.jsonl."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def write_manifest(root, manifest):
    """Write a fleet.json manifest (id -> {manager, tags}) at the fleet root, if a test needs hierarchy."""
    (pathlib.Path(root) / "fleet.json").write_text(json.dumps(manifest))


# --------------------------------------------------------------------------- console boot (integration)
def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def boot_console(root, token="", control_queue=None, bridges="", extra_env=None, hermetic=True):
    """Boot the real console against `root` on an ephemeral loopback port. Returns (console_module,
    base_url, stop_fn). No docker needed. Env is set BEFORE importing fleet/console so import-time
    globals (STACKS_ROOTS, MON_*) resolve to the fixture; already-imported modules are also patched
    defensively so re-runs in one process stay correct.

    hermetic=True (default) neutralizes the docker-CLI calls (`_compose_ls`/`_manifest`) so the ONLY
    agents discovered are the on-disk fixture deployments — identical result whether or not a real
    docker daemon with real agents happens to be running on the test host (i.e. CI == laptop)."""
    os.environ["ENCLAVE_STACKS_ROOTS"] = str(root)
    os.environ["CONSOLE_TOKEN"] = token or ""
    os.environ["ENCLAVE_DOCTOR_BRIDGES"] = bridges or ""
    cq = control_queue or str(pathlib.Path(root) / "_control")
    # Assign (NOT setdefault) every env key to THIS root, and remember prior values so stop() can
    # restore them — otherwise a second boot in the same process reuses the first root's paths
    # (external review). Monitor paths point at this root so the cap probe never hits the network.
    _env_keys = {"ENCLAVE_STACKS_ROOTS": str(root), "CONSOLE_TOKEN": token or "",
                 "ENCLAVE_DOCTOR_BRIDGES": bridges or "", "ENCLAVE_CONTROL_QUEUE": cq,
                 "ENCLAVE_MONITOR_HEARTBEAT": str(pathlib.Path(root) / "monitor-heartbeat.json"),
                 "ENCLAVE_MONITOR_STATE": str(pathlib.Path(root) / "monitor-state.json"),
                 **(extra_env or {})}
    _env_prev = {k: os.environ.get(k) for k in _env_keys}
    os.environ.update(_env_keys)

    import fleet
    # snapshot the module globals we mutate so stop() can put them back (keeps suites isolated even
    # when several boot in one process).
    _fleet_prev = {"STACKS_ROOTS": fleet.STACKS_ROOTS, "_scan_cache": fleet._scan_cache,
                   "_compose_ls": fleet._compose_ls, "_manifest": fleet._manifest}
    fleet.STACKS_ROOTS = [pathlib.Path(root).resolve()]
    fleet._scan_cache = {"ts": 0.0, "data": {}}
    if hermetic:
        fleet._compose_ls = lambda: []      # no real docker projects bleed in
        fleet._manifest = lambda: {}        # no host fleet.json manifest
    import console
    # defensively re-point module globals derived from env at import (for same-process re-boots)
    console.TOKEN = token or ""
    console.MON_CONTROL_QUEUE = cq
    console.MON_HEARTBEAT = pathlib.Path(os.environ["ENCLAVE_MONITOR_HEARTBEAT"])
    console.MON_STATE = pathlib.Path(os.environ["ENCLAVE_MONITOR_STATE"])

    # populate the caches once (the production loops are while-True; we just run their bodies here).
    snap = fleet.snapshot()
    for a in snap.values():
        a["reachable"] = False
    with console._lock:
        console._cache["agents"] = snap
        console._cache["ts"] = time.time()
    _run_cost_once(console)

    port = _free_port()
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", port), console.H)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    # wait until it accepts a connection
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.02)

    def stop():
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        # restore mutated fleet globals + os.environ so the next boot/suite starts clean
        for k, v in _fleet_prev.items():
            setattr(fleet, k, v)
        for k, prev in _env_prev.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev

    return console, base, stop


def _run_cost_once(console):
    """Run one iteration of the cost-loop body (the loop itself is infinite in production)."""
    import usage as _usage
    try:
        paths, snap = console._snap_homes()
        wins, ext = {}, {}
        for w in ("today", "wtd", "7d"):
            cut, _ = _usage.window_cutoff(w)
            fleet_t, agents_t = _usage.aggregate(paths, cut)
            wins[w] = {"fleet": fleet_t, "agents": agents_t}
            fusd, agext, bym = 0.0, {}, {}
            for aid, up in paths.items():
                r = _usage.api_rollup(str(pathlib.Path(up).parent / "api_spending.jsonl"), cut)
                if r["calls"]:
                    agext[aid] = r
                fusd += r["usd"]
            ext[w] = {"fleet": {"usd": round(fusd, 4), "by_model": bym}, "agents": agext}
        cut7, _ = _usage.window_cutoff("7d")
        ser = {by: _usage.series(paths, cut7, "day", by) for by in ("agent", "model", "reason")}
        last = {aid: _usage.last_record(p) for aid, p in paths.items()}
        cap = console._read_cap(paths)
        graph = console._build_graph(snap, paths, wins.get("wtd", {}).get("agents", {}))
        alerts = console._alerts(snap, wins.get("wtd", {}), cap)
        with console._cost_lock:
            console._cost.update(usage=wins, external=ext, cap=cap, series=ser,
                                 alerts=alerts, last=last, graph=graph, ts=time.time())
    except Exception as e:
        # FAIL LOUD in tests (production's loop is fail-open, but a test harness that swallows a
        # cost-computation error lets every shape-only cost/overview assertion pass against empty
        # data — external review). Surface it so the suite goes red.
        raise RuntimeError(f"_run_cost_once failed — cost subsystem is broken, not 'fail-open': {e}") from e


# --------------------------------------------------------------------------- HTTP helpers
def get(base, path, token=None, raw=False):
    """GET base+path. Returns (status_code, parsed_or_text). On HTTPError returns (code, body)."""
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("X-Console-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            body = r.read().decode("utf-8", "ignore")
            return r.status, body if raw else _maybe_json(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        return e.code, body if raw else _maybe_json(body)


def post(base, path, data=None, token=None, raw=False, origin="http://127.0.0.1",
         csrf=True, xrw="fetch"):
    """POST JSON. By default sends the X-Requested-With:fetch CSRF header the console requires; pass
    csrf=False (or xrw=None) to exercise the rejection path."""
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(base + path, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if csrf and xrw:
        req.add_header("X-Requested-With", xrw)
    if origin:
        req.add_header("Origin", origin)
    if token:
        req.add_header("X-Console-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            out = r.read().decode("utf-8", "ignore")
            return r.status, out if raw else _maybe_json(out)
    except urllib.error.HTTPError as e:
        out = e.read().decode("utf-8", "ignore")
        return e.code, out if raw else _maybe_json(out)


def _maybe_json(body):
    try:
        return json.loads(body)
    except Exception:
        return body
