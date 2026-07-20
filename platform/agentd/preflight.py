#!/usr/bin/env python3
"""preflight.py — DETERMINISTIC startup capability check for an enclave agent (off-Opus).

Runs from runtime.sh BEFORE the (expensive) claude -p turn. For each capability the agent's mission
declares in REQUIRES, it runs a FUNCTIONAL probe — does the tool actually WORK, not just "does a file
exist". Writes state/capabilities.json and, for any required-but-broken capability, appends a clear
line to state/escalations.log. With PREFLIGHT_GATE=1, a broken required capability makes this exit 75
so runtime.sh DEFERS the tick (no Opus burned) until a human fixes it.

This is the framework fix for the "8 wasted ticks" incident: the agent's HTTPS dev server was fine but
mis-checked (wrong-protocol curl → 000), it concluded "sandbox blocked" and abandoned rendering for 8
ticks. The probes here use the CORRECT check (https + ignore-cert), so a false negative can't recur; and
the point is to prove the toolchain works BEFORE the model spends a token, not to rely on the model to
remember to check.

REQUIRES resolution (first found): --requires a,b,c  →  env REQUIRES  →  state/requires.json (JSON list).
Known capabilities: route, render, qmd, codegraph, image, deploy_key, voice, gcloud, web, delivery.
Unknown names are recorded as ok=null ("no probe") — never block on those.

CONTRACT (fixed 2026-07-20): capabilities.json is ALWAYS written, every agent, every boot — the
tick prompt tells agents to READ it FIRST, so its absence is a framework bug, not a config choice.
With no REQUIRES declared, a BASELINE probe set (web, qmd) runs ADVISORY-only: results are recorded
but never escalate and never gate. (channel-lab polled a file that was never written for 4 ticks
because preflight was silently skipped when REQUIRES was unset.)
"""
import os, sys, json, time, ssl, subprocess, pathlib, urllib.request, argparse


def _run(cmd, timeout=25, inp=None):
    try:
        r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout, input=inp)
        return r.returncode, ((r.stdout or "") + (r.stderr or ""))
    except Exception as e:
        return 1, str(e)


def _http(url, timeout=6):
    # tolerant of self-signed HTTPS — the exact trap that caused the false "server down" diagnosis.
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            return r.status, r.read(1024).decode("utf-8", "ignore")
    except Exception as e:
        return 0, str(e)


# --- functional probes: (ok: bool, detail: str) ---
def probe_route(env):
    rc, out = _run('echo "Reply with the single word OK." | node /workspace/tools/llm/route.mjs --task classify', timeout=45)
    ok = rc == 0 and len(out.strip()) > 0 and "no allowed pool" not in out and "all pools failed" not in out
    return ok, ("route.mjs returns content" if ok else f"route.mjs empty/failed: {out.strip()[:140]}")


def probe_render(env):
    port = env.get("RENDER_PORT", "3002")
    st, _ = _http(f"https://localhost:{port}/", 6)             # https + ignore-cert (NOT http://)
    return st == 200, (f"dev server :{port} serves 200 over https" if st == 200
                       else f":{port} not serving (https status={st}) — needs a live vite (studio keeps it up); a wrong-protocol 000 is NOT a hard block")


def probe_qmd(env):
    st, _ = _http(env.get("QMD_HEALTH", "http://host.docker.internal:18181/health"), 5)
    return st == 200, f"qmd gateway health={st}"


def probe_codegraph(env):
    st, _ = _http(env.get("CODEGRAPH_HEALTH", "http://host.docker.internal:18182/health"), 5)
    return st == 200, f"codegraph gateway health={st}"


def probe_image(env):
    p = pathlib.Path("/workspace/.secrets/openrouter.env")
    return p.exists(), ("openrouter key present (gen.py)" if p.exists() else "no /workspace/.secrets/openrouter.env")


def probe_deploy_key(env):
    k = env.get("DEPLOY_KEY", "/workspace/.secrets/stoneforge-deploy-key")
    repo = env.get("DEPLOY_REPO", "git@github.com:wartzar-bee/stoneforge.git")
    if not pathlib.Path(k).exists():
        return False, f"deploy key {k} missing"
    rc, out = _run(f'GIT_SSH_COMMAND="ssh -i {k} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null" git ls-remote {repo} HEAD', timeout=25)
    return rc == 0, ("deploy key reaches the repo" if rc == 0 else f"git ls-remote failed: {out.strip()[:120]}")


def probe_voice(env):
    st, _ = _http(env.get("VOICE_HEALTH", "http://host.docker.internal:18186/health"), 5)
    return st == 200, f"voice bridge health={st}"


def probe_gcloud(env):
    st, _ = _http(env.get("GCLOUD_HEALTH", "http://host.docker.internal:18187/health"), 5)
    return st == 200, f"gcloud bridge health={st}"


def probe_web(env):
    """FUNCTIONAL web egress: can this container GET a real page and see real content?
    Exists because a pod once claimed its web access "returns mocked responses" and nobody could
    check the claim against an instrument — now the instrument checks itself every boot. Two
    independent hosts so one being down doesn't read as "egress broken"."""
    for url, marker in (("https://example.com/", "Example Domain"),
                        ("https://httpbin.org/get", '"Host"')):
        st, body = _http(url, 8)
        if st == 200 and marker in body:
            return True, f"egress OK: GET {url} → 200 with expected content"
        if st == 200:
            return False, f"GET {url} → 200 but UNEXPECTED content (proxy/mock?): {body[:80]!r}"
    return False, "no probe URL reachable (egress down or filtered)"


def probe_delivery(env):
    """Is this agent's output pipeline CONNECTED? The delivery daemon (deliver.py, host-side or
    in-pod) touches a heartbeat file each run; a stale/missing heartbeat means outputs are being
    filed into a pipe attached to nothing (ideas-scout ran for days like that — permanent false
    STARVED). Configure DELIVERY_MARKER (+ optional DELIVERY_MAX_AGE_H, default 3)."""
    marker = env.get("DELIVERY_MARKER", "")
    if not marker:
        return None, "no DELIVERY_MARKER configured (not verified)"
    max_age = float(env.get("DELIVERY_MAX_AGE_H", "3")) * 3600
    try:
        age = time.time() - os.path.getmtime(marker)
    except OSError:
        return False, f"delivery heartbeat {marker} missing — output pipeline NOT connected"
    return (age <= max_age), (f"delivery heartbeat {int(age/60)}m old"
                              + ("" if age <= max_age else f" (> {int(max_age/60)}m — daemon stalled?)"))


PROBES = {"route": probe_route, "render": probe_render, "qmd": probe_qmd, "codegraph": probe_codegraph,
          "image": probe_image, "deploy_key": probe_deploy_key, "voice": probe_voice, "gcloud": probe_gcloud,
          "web": probe_web, "delivery": probe_delivery}

# Probed for EVERY agent even with no REQUIRES — advisory-only (recorded, never escalates/gates).
BASELINE = ["web", "qmd"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("AGENT_STATE", "/agent/state"))
    ap.add_argument("--requires", default=os.environ.get("REQUIRES", ""))
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    st = pathlib.Path(a.state); st.mkdir(parents=True, exist_ok=True)
    capfile = st / "capabilities.json"
    reqs = [x.strip() for x in a.requires.split(",") if x.strip()]
    if not reqs and (st / "requires.json").exists():
        try:
            reqs = json.loads((st / "requires.json").read_text())
        except Exception:
            reqs = []

    # The full probe list = REQUIRED (may escalate/gate) + BASELINE (advisory-only, every agent).
    probe_list = list(reqs) + [b for b in BASELINE if b not in reqs]

    # cached: skip if the last run covered the same reqs, all required were ok, and it is <24h old
    # (re-run on --force / tooling change). The freshness bound keeps a long-running pod's
    # capabilities.json from asserting a toolchain state probed weeks ago.
    if capfile.exists() and not a.force:
        try:
            prev = json.loads(capfile.read_text())
            fresh = (time.time() - float(prev.get("_ts", 0))) < 86400
            if fresh and prev.get("_reqs") == sorted(reqs) and all(prev.get(r, {}).get("ok") for r in reqs):
                print("preflight: capabilities.json fresh + all required OK — skip")
                return 0
        except Exception:
            pass

    env = dict(os.environ); env.setdefault("AGENT_DIR", str(st.parent))
    results = {"_ts": int(time.time()), "_reqs": sorted(reqs)}
    broken = []
    for r in probe_list:
        required = r in reqs
        probe = PROBES.get(r)
        if not probe:
            results[r] = {"ok": None, "required": required, "detail": "no probe for this capability (not verified)"}
            print(f"  [ -- ] {r}: no probe")
            continue
        try:
            ok, detail = probe(env)
        except Exception as e:
            ok, detail = False, f"probe error: {e}"
        results[r] = {"ok": ok, "required": required, "detail": detail}
        tag = "OK " if ok else ("-- " if ok is None else "FAIL")
        print(f"  [{tag}] {r}{'' if required else ' (baseline)'}: {detail}")
        if ok is False and required:
            broken.append(r)
    capfile.write_text(json.dumps(results, indent=2))

    if broken:
        with (st / "escalations.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} PREFLIGHT: required capability BROKEN "
                    f"→ {broken}. See state/capabilities.json. FIX the tool/env (or the probe if it's a false "
                    f"negative — e.g. server needs to be up); do NOT abandon it as 'blocked' and build unverifiable work.\n")
        print(f"preflight: BROKEN required capabilities: {broken} → escalated")
        return 3
    print("preflight: all required capabilities OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
