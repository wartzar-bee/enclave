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
but never escalate and never gate. (labpod polled a file that was never written for 4 ticks
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
    k = env.get("DEPLOY_KEY", "/workspace/.secrets/forgepod-deploy-key")
    repo = env.get("DEPLOY_REPO", "git@github.com:demopod/forgepod.git")
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
    filed into a pipe attached to nothing (scoutpod ran for days like that — permanent false
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


# --- config-correctness checks: (level, msg) or None if fine ---
# DECLARE-then-DIFF: each reads a DECLARATION from the pod's own env, never a hardcoded constant, so the
# framework ships ZERO deployment policy (a metered team and a subscription studio both pass with their
# own declarations). ADVISORY: these ALARM (record + escalate) but NEVER gate boot — a false positive
# must not brick a pod, which is the exact failure preflight itself exists to prevent.
def _free_tier(env):
    """DECLARED free-at-margin model tier (COST_FREE_TIER, comma-sep substrings). Empty by default → a
    metered deployment gets NO false alarm; the orchestrator declares COST_FREE_TIER=claude for its subscription."""
    return tuple(s.strip().lower() for s in env.get("COST_FREE_TIER", "").split(",") if s.strip())


def cfg_cost(env):
    """BRAIN=api paying per-token for a model in the deployment's DECLARED free tier = the $300 leak.
    (The inverse of the rev-1 plan's wording; the real leak is api-brain on a free-tier model.)"""
    if env.get("BRAIN", "") != "api":
        return None
    model = (env.get("BRAIN_MODEL", "") + " " + env.get("MODEL", "")).lower()
    hit = next((m for m in _free_tier(env) if m in model), None)
    return ("crit", f"BRAIN=api is paying per-token for a model in the declared free tier ('{hit}' via "
                    f"COST_FREE_TIER) — switch to BRAIN=claude (free at margin).") if hit else None


def cfg_permission(env):
    """A pod that DECLARES it needs autonomous network/exec (AGENT_NEEDS) but runs an approval-gated
    permission mode DEADLOCKS — nothing approves the prompt in an unattended tick (the acceptEdits
    incident). Derived from the declaration, not a blanket 'acceptEdits is always wrong'."""
    needs = {s.strip().lower() for s in env.get("AGENT_NEEDS", "").split(",") if s.strip()}
    perm = env.get("PERMISSION", "acceptEdits").lower()
    gated = perm in ("acceptedits", "allowlist", "default")   # modes that can block a tool on approval
    if needs & {"network", "exec", "bash"} and gated:
        return ("error", f"PERMISSION={env.get('PERMISSION')} is approval-gated but the agent declares "
                         f"needs={sorted(needs)} — autonomous network/exec will hang on an approval "
                         f"prompt. Use PERMISSION=dangerous (the guard hook stays the real safety layer).")
    return None


def cfg_persistence(env):
    """Building into ephemeral storage is lost on restart (the /work wipe). The persistent build dir is
    DECLARED as WORK_PERSIST and must live under the pod's persistent home ($AGENT_DIR — always a mount).
    WARN only when it's explicitly set OUTSIDE that home (no path denylist; unset → runtime may set it,
    so stay silent rather than false-warn)."""
    wp = env.get("WORK_PERSIST", "")
    home = env.get("AGENT_DIR", "/agent").rstrip("/")
    if wp and not (wp == home or wp.startswith(home + "/")):
        return ("warn", f"WORK_PERSIST={wp} is outside the persistent home ({home}) — a restart may wipe "
                        f"the build. Point it under the persistent home.")
    return None


def cfg_warm_session(env):
    """A continuous (daemon) pod that resumes a warm session each tick bloats context + carries stale
    cross-tick beliefs (the acceptEdits-carryover). The proven default for continuous agents is
    WARM_SESSION=0; a daemon without it is a WARN (not error — some pods legitimately want warm)."""
    if env.get("RUNTIME_MODE", "").lower() != "daemon":
        return None
    if env.get("WARM_SESSION", "1") != "0":
        return ("warn", "continuous (daemon) pod without WARM_SESSION=0 — warm resume bloats context and "
                        "carries stale cross-tick state. Set WARM_SESSION=0 (durable files carry continuity).")
    return None


def cfg_observability(env):
    """Is this pod still REPORTING what it does? Nothing else checked, and it went dark unnoticed.

    On 2026-07-22 an audit of the live fleet found event capture had been dead on THREE of five pods
    for 27.5 hours — all three froze within 18 seconds of each other, and none of them noticed,
    escalated, or degraded visibly. The framework ships hooks/event_log.py into every pod but the
    default settings.json generator never wired it, so whether a pod emits events depended on which
    template it happened to be created from. Meanwhile the pods kept ticking and the dashboard kept
    showing them green: an agent that cannot report is indistinguishable from one that is fine.

    Three cheap disk reads, in the order they fail:
      1. the event source is present but NOT WIRED  -> it will never emit, by construction;
      2. it is wired but events.jsonl is far older than the last tick -> it emitted and then stopped;
      3. no tick-scorecard.jsonl while ticks are happening -> product is unmeasurable for this pod.
    WARN only, like every config check — a monitoring gap must never stop the agent working."""
    home = pathlib.Path(env.get("AGENT_DIR", "/agent"))
    st = home / "state"
    hooks_dir, setjson = home / ".claude" / "hooks", home / ".claude" / "settings.json"
    try:
        wired = "event_log" in setjson.read_text()
    except Exception:
        wired = False
    if (hooks_dir / "event_log.py").exists() and not wired:
        return ("warn", "event_log.py is shipped but NOT WIRED in .claude/settings.json — this pod "
                        "emits no tool events, so nothing can report what it actually did. Add it as "
                        "a PostToolUse hook.")
    ev, sc = st / "events.jsonl", st / "tick-scorecard.jsonl"
    try:
        last_tick = sc.stat().st_mtime
    except OSError:
        last_tick = 0
    if wired and last_tick and ev.exists():
        gap = last_tick - ev.stat().st_mtime
        if gap > 6 * 3600:
            return ("warn", f"events.jsonl is {gap/3600:.1f}h older than the last scored tick — the "
                            f"event stream went dark while this pod kept working. Restart the pod "
                            f"(a hook change does not reach an already-running loop).")
    if last_tick == 0 and (st / ".heartbeat").exists() and not (st / "paused").exists():
        return ("warn", "no state/tick-scorecard.jsonl on a live pod — product output is unmeasurable, "
                        "so this agent reads as idle no matter what it produces.")
    return None


CONFIG_CHECKS = {"cost": cfg_cost, "permission": cfg_permission,
                 "persistence": cfg_persistence, "warm_session": cfg_warm_session,
                 "observability": cfg_observability}


def _run_config_checks(env, st):
    """Run the config-correctness checks + escalate (drain-on-clear) and return the findings. Called
    EVERY boot BEFORE the capability cache-skip — config can change between ticks and the checks are
    cheap, so they must never be short-circuited by a fresh capabilities.json (that dormancy bug let the
    checks sit un-run for 18h). Advisory only: never gates, never changes the exit code."""
    cfg_findings = {}
    for name, chk in CONFIG_CHECKS.items():
        try:
            res = chk(env)
        except Exception:
            res = None
        if res:
            level, msg = res
            cfg_findings[name] = {"level": level, "msg": msg}
            print(f"  [CFG:{level.upper()}] {name}: {msg}")
    cfg_stamp = st / ".preflight-config"
    prev_cfg = {}
    if cfg_stamp.exists():
        try: prev_cfg = json.loads(cfg_stamp.read_text())
        except Exception: prev_cfg = {}
    ts = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    with (st / "escalations.log").open("a") as f:
        for name, fnd in cfg_findings.items():
            if name not in prev_cfg:
                f.write(f"{ts} ESCALATE :: [preflight:config] {fnd['level'].upper()} {name} — {fnd['msg']}\n")
        for name in prev_cfg:
            if name not in cfg_findings:
                f.write(f"{ts} NOTE :: RESOLVED [preflight:config] {name} finding cleared.\n")
    cfg_stamp.write_text(json.dumps(cfg_findings))
    return cfg_findings


def _selftest():
    """Proof-of-firing for the config checks (design rule: no check lands without a test that trips it)."""
    ok = True
    def ck(name, cond):
        nonlocal ok; ok = ok and cond
        print(f"  {'PASS' if cond else 'FAIL'} {name}")
    # cost: api-brain on a declared-free model → CRIT
    ck("cost fires: api + free-tier model", cfg_cost({"BRAIN":"api","MODEL":"claude-opus-4-8","COST_FREE_TIER":"claude"}) and cfg_cost({"BRAIN":"api","MODEL":"claude-opus-4-8","COST_FREE_TIER":"claude"})[0]=="crit")
    # cost: metered team (no declared free tier) → silent, no false alarm
    ck("cost silent: metered team (empty free_tier)", cfg_cost({"BRAIN":"api","MODEL":"claude-opus-4-8","COST_FREE_TIER":""}) is None)
    # cost: subscription brain → not applicable
    ck("cost silent: BRAIN=claude", cfg_cost({"BRAIN":"claude","MODEL":"claude-opus-4-8","COST_FREE_TIER":"claude"}) is None)
    # perm: declared network need under approval-gated mode → ERROR
    ck("perm fires: needs network + acceptEdits", cfg_permission({"PERMISSION":"acceptEdits","AGENT_NEEDS":"network"}) and cfg_permission({"PERMISSION":"acceptEdits","AGENT_NEEDS":"network"})[0]=="error")
    # perm: dangerous mode → silent
    ck("perm silent: dangerous", cfg_permission({"PERMISSION":"dangerous","AGENT_NEEDS":"network,exec"}) is None)
    # perm: nothing declared → can't judge, stays silent (no false positive)
    ck("perm silent: no needs declared", cfg_permission({"PERMISSION":"acceptEdits"}) is None)
    # persistence: WORK_PERSIST outside the home → WARN; under home → silent; unset → silent
    ck("persist fires: WORK_PERSIST outside home", cfg_persistence({"WORK_PERSIST":"/work/x","AGENT_DIR":"/agent"}) and cfg_persistence({"WORK_PERSIST":"/work/x","AGENT_DIR":"/agent"})[0]=="warn")
    ck("persist silent: under home", cfg_persistence({"WORK_PERSIST":"/agent/work","AGENT_DIR":"/agent"}) is None)
    ck("persist silent: unset (runtime may set it)", cfg_persistence({"AGENT_DIR":"/agent"}) is None)
    # warm: daemon without WARM_SESSION=0 → WARN; with =0 → silent; non-daemon → silent
    ck("warm fires: daemon + warm", cfg_warm_session({"RUNTIME_MODE":"daemon","WARM_SESSION":"1"}) and cfg_warm_session({"RUNTIME_MODE":"daemon","WARM_SESSION":"1"})[0]=="warn")
    ck("warm silent: daemon + WARM_SESSION=0", cfg_warm_session({"RUNTIME_MODE":"daemon","WARM_SESSION":"0"}) is None)
    ck("warm silent: non-daemon", cfg_warm_session({"RUNTIME_MODE":"oneshot","WARM_SESSION":"1"}) is None)
    print("SELFTEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=os.environ.get("AGENT_STATE", "/agent/state"))
    ap.add_argument("--requires", default=os.environ.get("REQUIRES", ""))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()

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

    env = dict(os.environ); env.setdefault("AGENT_DIR", str(st.parent))

    # Config-correctness checks run EVERY boot (cheap; config drifts between ticks) — BEFORE the
    # capability cache-skip, which otherwise left them un-run for 18h behind a fresh capabilities.json.
    cfg_findings = _run_config_checks(env, st)

    # cached: skip the (expensive) capability PROBES if the last run covered the same reqs, all required
    # were ok, and it is <24h old. Config findings above always refresh; only the probes are cached.
    if capfile.exists() and not a.force:
        try:
            prev = json.loads(capfile.read_text())
            fresh = (time.time() - float(prev.get("_ts", 0))) < 86400
            if fresh and prev.get("_reqs") == sorted(reqs) and all(prev.get(r, {}).get("ok") for r in reqs):
                prev["_config"] = cfg_findings                 # refresh config findings in the cached file
                prev["_config_ts"] = int(time.time())
                capfile.write_text(json.dumps(prev, indent=2))
                print("preflight: capability probes fresh — skip (config checks refreshed)")
                return 0
        except Exception:
            pass

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

    results["_config"] = cfg_findings   # already run + escalated above (before the cache-skip)
    capfile.write_text(json.dumps(results, indent=2))

    stamp = st / ".preflight-alerted"
    if broken:
        with (st / "escalations.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} ESCALATE :: [preflight] required capability BROKEN "
                    f"→ {broken}. See state/capabilities.json. FIX the tool/env (or the probe if it's a false "
                    f"negative — e.g. server needs to be up); do NOT abandon it as 'blocked' and build unverifiable work.\n")
        stamp.write_text(",".join(broken))
        print(f"preflight: BROKEN required capabilities: {broken} → escalated")
        return 3
    # Alarm lifecycle (T2, 2026-07-20): the probe knows the moment the capability is back — write
    # the resolution so the console's alarm drains instead of pinning forever.
    if stamp.exists():
        with (st / "escalations.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} NOTE :: RESOLVED [preflight] "
                    f"previously-broken capability(ies) [{stamp.read_text().strip()}] now probe OK.\n")
        stamp.unlink(missing_ok=True)
    print("preflight: all required capabilities OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
