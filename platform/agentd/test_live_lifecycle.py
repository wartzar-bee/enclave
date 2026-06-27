"""LIVE end-to-end lifecycle test — drives the REAL running console against REAL docker.

This is the one thing the hermetic suite cannot assert: that a spec POSTed to /api/create is actually
built + started by the spawn watcher, that a config edit force-recreates the running container, and that
down/up/restart really move docker state (and the operator-stopped marker). It creates a THROWAWAY agent,
exercises the full lifecycle, and tears it down in a finally — touching nothing else in the fleet.

OPT-IN: it self-skips (exit 0) unless ENCLAVE_LIVE=1 AND a console + docker + the spawn/control watchers
are actually up — so CI and the default `run_tests.sh` stay hermetic. Run it on the host with:

    ENCLAVE_LIVE=1 python3 test_live_lifecycle.py

Env knobs: ENCLAVE_CONSOLE_URL (default http://127.0.0.1:8700), ENCLAVE_STACKS_ROOTS (first path =
fleet root, default ~/Dev/agent-workspace/fleet), LIVE_PROVIDER/LIVE_SECRET/LIVE_MODEL (default the
NVIDIA $0 path), LIVE_NAME (default dashtest-<pid>).
"""
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F

BASE = os.environ.get("ENCLAVE_CONSOLE_URL", "http://127.0.0.1:8700")
FLEET = pathlib.Path(os.environ.get("ENCLAVE_STACKS_ROOTS", "").split(":")[0]
                     or (pathlib.Path.home() / "Dev/agent-workspace/fleet"))
NAME = os.environ.get("LIVE_NAME", f"dashtest-{os.getpid()}")
PROVIDER = os.environ.get("LIVE_PROVIDER", "nvidia")
SECRET = os.environ.get("LIVE_SECRET", "nvidia.env")
MODEL = os.environ.get("LIVE_MODEL", "qwen/qwen3-next-80b-a3b-instruct")
check = F.Check()


def _docker(*args, t=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=t)


def _running(name):
    r = _docker("inspect", "-f", "{{.State.Running}}", name)
    return r.returncode == 0 and r.stdout.strip() == "true"


def _cid(name):
    r = _docker("inspect", "-f", "{{.Id}}", name)
    return r.stdout.strip()[:12] if r.returncode == 0 else ""


def _exists(name):
    return _docker("inspect", name).returncode == 0


def _restart_count(name):
    r = _docker("inspect", "-f", "{{.RestartCount}}", name)
    try:
        return int(r.stdout.strip()) if r.returncode == 0 else -1
    except ValueError:
        return -1


def _wait(fn, timeout=40, interval=0.5):
    """Poll until fn() is truthy or timeout — replaces fixed time.sleep so the test is fast when the
    system is fast and robust when it's slow (external review: fixed sleeps make E2E flaky)."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _skip(why):
    print(f"SKIP: {why}")
    raise SystemExit(0)


def _preflight():
    if os.environ.get("ENCLAVE_LIVE") != "1":
        _skip("set ENCLAVE_LIVE=1 to run the live lifecycle test (mutates real docker)")
    try:
        if _docker("version", t=10).returncode != 0:
            _skip("docker not available")
    except Exception:
        _skip("docker not available")
    code, _ = F.get(BASE, "/api/fleet")
    if code != 200:
        _skip(f"console not reachable at {BASE} (start tools/studio-console.sh)")
    # the spawn watcher must be draining the queue for create to work
    code, body = F.get(BASE, "/api/secrets-available")
    names = (body.get("available") or body.get("secrets") or []) if isinstance(body, dict) else []
    names = [s.get("name", s) if isinstance(s, dict) else s for s in names]
    if SECRET not in names:
        _skip(f"secret {SECRET} not in the console's library (have: {names[:6]}…) — set LIVE_SECRET")


def teardown():
    """Stop + remove the throwaway and its on-disk dir + any queue residue. Returns a list of cleanup
    failures (non-zero rc) so the caller can surface them — a silent teardown failure leaves orphaned
    containers/dirs that poison later runs (external review)."""
    fails = []
    d = FLEET / NAME
    cf = d / "docker-compose.yml"
    if cf.exists():
        r = subprocess.run(["docker", "compose", "-f", str(cf), "down", "-v"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            fails.append(f"compose down rc={r.returncode}: {(r.stderr or '')[-160:]}")
    for n in (NAME, f"{NAME}-chat"):
        if _exists(n):
            r = _docker("rm", "-f", n)
            if r.returncode != 0:
                fails.append(f"rm {n} rc={r.returncode}")
    if d.exists():
        r = subprocess.run(["rm", "-rf", str(d)], capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            fails.append(f"rm -rf {d} rc={r.returncode}")
    q = FLEET / "_queue"
    for sub in ("incoming", "processed", "failed"):
        for f in (q / sub).glob(f"*{NAME}*.json") if (q / sub).exists() else []:
            try: f.unlink()
            except OSError as e: fails.append(f"unlink {f}: {e}")
    staging = q / "secrets-staging" / NAME
    if staging.exists():
        subprocess.run(["rm", "-rf", str(staging)], timeout=10)
    return fails


def main():
    _preflight()
    print(f"[live] console={BASE} fleet={FLEET} throwaway={NAME} provider={PROVIDER}")
    teardown()  # clean any prior remnant of this name
    try:
        # ---- CREATE from scratch via the dashboard ----
        code, body = F.post(BASE, "/api/create", {
            "name": NAME, "template": "ops", "brain": "api", "provider": PROVIDER,
            "model": MODEL, "interval_seconds": 86400,
            "mission": "Throwaway live-lifecycle test agent. Do nothing of consequence.",
            "secrets": [SECRET]})
        check("create accepted (200 + ok)", code == 200 and isinstance(body, dict) and body.get("ok"),
              f"{code} {body}")

        # ---- spawn watcher builds + starts it (poll up to 180s for the image build) ----
        check("spawn watcher built + started the container", _wait(lambda: _running(NAME), timeout=180, interval=3),
              "container never came up — is the spawn watcher (org.enclave.spawn) running current code?")
        if not _running(NAME):
            raise SystemExit(check.report())

        # HEALTH, not just 'running': give it a moment and assert it isn't crash-looping (docker would
        # flip a crash-looping container in/out of Running while RestartCount climbs).
        time.sleep(5)
        check("agent is healthy, not crash-looping (RestartCount==0)", _restart_count(NAME) == 0,
              f"RestartCount={_restart_count(NAME)}")

        # dashboard reflects it
        code, body = F.get(BASE, "/api/fleet")
        a = (body.get("agents") or {}).get(NAME, {}) if isinstance(body, dict) else {}
        check("dashboard shows the new agent up", bool(a.get("up")))
        check("new agent brain=api / model set", a.get("brain") == "api" and MODEL in (a.get("model") or ""))

        agent_env = FLEET / NAME / "home" / "agent.env"
        marker = FLEET / NAME / "home" / "state" / ".operator-stopped"

        # ---- CONFIG CHANGE while running -> force-recreate (poll for the new container id) ----
        before = _cid(NAME)
        code, body = F.post(BASE, "/api/config", {"id": NAME, "updates": {"INTERVAL_SECONDS": "90000"}})
        check("config edit accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        check("config edit force-recreated the container (new id)",
              _wait(lambda: _cid(NAME) and _cid(NAME) != before, timeout=40))
        check("config edit persisted to agent.env",
              "INTERVAL_SECONDS=90000" in agent_env.read_text() if agent_env.exists() else False)
        check("agent still running after config change", _wait(lambda: _running(NAME), timeout=30))

        # ---- STOP (down) ----
        code, body = F.post(BASE, "/api/action", {"action": "down", "id": NAME})
        check("down accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        check("container stopped after down", _wait(lambda: not _running(NAME), timeout=40))
        check("operator-stopped marker written on down", _wait(lambda: marker.exists(), timeout=10))

        # ---- START (up) ----
        code, body = F.post(BASE, "/api/action", {"action": "up", "id": NAME})
        check("up accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        check("container running after up", _wait(lambda: _running(NAME), timeout=40))
        check("operator-stopped marker cleared on up", _wait(lambda: not marker.exists(), timeout=10))

        # ---- RESTART ----
        code, body = F.post(BASE, "/api/action", {"action": "restart", "id": NAME})
        check("restart accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        check("container running after restart", _wait(lambda: _running(NAME), timeout=40))
    finally:
        fails = teardown()
        gone = not _exists(NAME) and not (FLEET / NAME).exists()
        check("teardown removed the throwaway (container + dir)", gone)
        check("teardown had no cleanup failures (no orphans left)", not fails, "; ".join(fails))

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
