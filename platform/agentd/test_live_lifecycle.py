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
    """Best-effort: stop + remove the throwaway and its on-disk dir + any queue residue."""
    d = FLEET / NAME
    cf = d / "docker-compose.yml"
    if cf.exists():
        subprocess.run(["docker", "compose", "-f", str(cf), "down", "-v"],
                       capture_output=True, text=True, timeout=120)
    for n in (NAME, f"{NAME}-chat"):
        if _exists(n):
            _docker("rm", "-f", n)
    if d.exists():
        subprocess.run(["rm", "-rf", str(d)], timeout=30)
    q = FLEET / "_queue"
    for sub in ("incoming", "processed", "failed"):
        for f in (q / sub).glob(f"*{NAME}*.json") if (q / sub).exists() else []:
            try: f.unlink()
            except OSError: pass
    staging = q / "secrets-staging" / NAME
    if staging.exists():
        subprocess.run(["rm", "-rf", str(staging)], timeout=10)


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
        deadline = time.time() + 180
        while time.time() < deadline and not _running(NAME):
            time.sleep(3)
        check("spawn watcher built + started the container", _running(NAME),
              "container never came up — is the spawn watcher (org.enclave.spawn) running current code?")
        if not _running(NAME):
            raise SystemExit(check.report())

        # dashboard reflects it
        code, body = F.get(BASE, "/api/fleet")
        a = (body.get("agents") or {}).get(NAME, {}) if isinstance(body, dict) else {}
        check("dashboard shows the new agent up", bool(a.get("up")))
        check("new agent brain=api / model set", a.get("brain") == "api" and MODEL in (a.get("model") or ""))

        agent_env = FLEET / NAME / "home" / "agent.env"
        marker = FLEET / NAME / "home" / "state" / ".operator-stopped"

        # ---- CONFIG CHANGE while running -> force-recreate ----
        before = _cid(NAME)
        code, body = F.post(BASE, "/api/config", {"id": NAME, "updates": {"INTERVAL_SECONDS": "90000"}})
        check("config edit accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        time.sleep(6)
        check("config edit force-recreated the container (new id)", _cid(NAME) and _cid(NAME) != before)
        check("config edit persisted to agent.env",
              "INTERVAL_SECONDS=90000" in agent_env.read_text() if agent_env.exists() else False)
        check("agent still running after config change", _running(NAME))

        # ---- STOP (down) ----
        code, body = F.post(BASE, "/api/action", {"action": "down", "id": NAME})
        check("down accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        time.sleep(4)
        check("container stopped after down", not _running(NAME))
        check("operator-stopped marker written on down", marker.exists())

        # ---- START (up) ----
        code, body = F.post(BASE, "/api/action", {"action": "up", "id": NAME})
        check("up accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        time.sleep(6)
        check("container running after up", _running(NAME))
        check("operator-stopped marker cleared on up", not marker.exists())

        # ---- RESTART ----
        code, body = F.post(BASE, "/api/action", {"action": "restart", "id": NAME})
        check("restart accepted", code == 200 and isinstance(body, dict) and body.get("ok"), f"{code} {body}")
        time.sleep(6)
        check("container running after restart", _running(NAME))
    finally:
        teardown()
        gone = not _exists(NAME) and not (FLEET / NAME).exists()
        check("teardown removed the throwaway (container + dir)", gone)

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
