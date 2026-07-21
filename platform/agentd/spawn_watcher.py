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
import os, sys, re, json, time, pathlib, subprocess, shutil

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


# A guardrail is the ABSENCE of a capability, not a text match (operator rule 2026-06-27). The
# operator owns money + legal authority (CLAUDE.md), so a payment-rail or legal-identity credential
# must NEVER be mounted into an autonomous pod's scoped /workspace/.secrets. If no card/payment token
# exists in the container, the agent structurally cannot spend — regardless of what it types, and
# without any (evadable, false-positive-prone) runtime command-text filtering. This is the chokepoint:
# refuse to STAGE such a credential, quarantine it, escalate to the operator, and start the pod without
# it. The operator can still place one out-of-band if they genuinely intend a pod to transact.
_PAYMENT_LEGAL_RE = re.compile(
    r"stripe|gumroad|paypal|braintree|adyen|paddle|lemonsqueez|razorpay|payoneer|venmo|cashapp|"
    r"coinbase|binance|\bwise[_-]?api|"                            # processors / wallets
    r"card[_-]?number|credit[_-]?card|\bcvv\b|\bcvc\b|\biban\b|"   # card / bank
    r"routing[_-]?number|account[_-]?number|sort[_-]?code|"
    r"\bkdp\b|amazon[_-]?kdp|seller[_-]?central",                  # legal-identity publishing / selling
    re.I)


def _is_payment_or_legal_cred(fname, content):
    """True if a staged secret file looks like a payment rail or legal-identity credential — the kind
    that routes through the operator and must never be mounted into an autonomous pod (the capability,
    not a runtime string, is the guardrail). Matches on filename OR the env body."""
    return bool(_PAYMENT_LEGAL_RE.search(fname or "") or _PAYMENT_LEGAL_RE.search(content or ""))


MANIFEST = pathlib.Path(os.environ.get("ENCLAVE_MANIFEST",
                        str(pathlib.Path.home() / ".config" / "enclave" / "fleet.json")))


def _ledger():
    """DECLARED credential-ownership ledger (manifest identities.shared + identities.owners). Empty if
    absent — then the guard is inert (no false refusals), consistent with declare-then-diff."""
    try:
        idn = json.loads(MANIFEST.read_text()).get("identities", {})
        return set(idn.get("shared", [])), dict(idn.get("owners", {}))
    except Exception:
        return set(), {}


def _record_handover(fname, new_owner):
    """Transfer ownership in the ledger (deliberate handover: e.g. labpod proved a channel → hands
    the account to the owning agent). Best-effort; never raises into the staging loop."""
    try:
        d = json.loads(MANIFEST.read_text())
        d.setdefault("identities", {}).setdefault("owners", {})[fname] = new_owner
        MANIFEST.write_text(json.dumps(d, indent=2) + "\n")
    except Exception:
        pass


def _escalate(target, name, msg):
    """Append a human-decision escalation to the pod's escalations.log (the dashboard HITL inbox reads
    it). Same plain-text format the monitor/supervisor use: '<iso> ESCALATE :: <msg>'."""
    try:
        f = target / "home" / "state" / "escalations.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with f.open("a") as h:
            h.write(f"{ts} ESCALATE :: [spawn:payment-cred-refused] {name} — {msg}\n")
    except Exception:
        pass


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


def _load_spec(spec_path):
    """Full spec dict (YAML or JSON), or None when unparseable."""
    try:
        text = spec_path.read_text()
        if spec_path.suffix in (".yaml", ".yml"):
            import yaml
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _governance_check(spec):
    """A graduated pod must be BORN governed (2026-07-19 evaluation §3.5): the spawn spec is the
    only surface guaranteed to exist at birth, and until now NO spec format carried a term sheet —
    every pod's governance was retrofitted prose, and one term sheet was written to a JSON file
    without ever reaching the pod (L-304). Venture-class specs (template/class == 'venture')
    REQUIRE term_sheet: {kpi, kill_line[, budget_usd_weekly]}. Internal/ops agents may carry one
    voluntarily; when present it is materialized either way."""
    if not isinstance(spec, dict):
        return True, ""                       # unparseable specs fail later in `enclave new`
    venture = "venture" in {str(spec.get("class") or "").strip(), str(spec.get("template") or "").strip()}
    ts = spec.get("term_sheet")
    if venture:
        if not isinstance(ts, dict):
            return False, ("venture-class spec has no term_sheet — a pod is a Series A, not an "
                           "experiment (vc-os). Required: term_sheet: {kpi: <one measurable signal>, "
                           "kill_line: YYYY-MM-DD, budget_usd_weekly: N}")
        missing = [k for k in ("kpi", "kill_line") if not str(ts.get(k) or "").strip()]
        if missing:
            return False, f"term_sheet is missing required field(s): {', '.join(missing)}"
        # The KPI must be MEASURABLE FROM OUTSIDE on day one (kpi_probe.py reads these): a venture
        # whose KPI only its own pod can report is born unfalsifiable — the 61-followers class.
        srcs = ts.get("kpi_sources")
        if not (isinstance(srcs, list) and srcs and all(
                isinstance(s, dict) and str(s.get("label", "")).strip() and str(s.get("url", "")).strip()
                and str(s.get("pattern", "")).strip() for s in srcs)):
            return False, ("term_sheet has no usable kpi_sources — the external KPI probe needs "
                           "[{label, url, pattern}] so the orchestrator can read the KPI from the "
                           "third-party surface. A pod may not score itself (standing rule).")
        # Analytics-plan P0: PRODUCT must be machine-checkable from birth — without kpi_artifacts
        # globs the scorecard runs blind (product=null) and "done at the product level" is prose
        # again. Same born-governed rule as the term sheet.
        ka = spec.get("kpi_artifacts")
        if not (isinstance(ka, list) and any(str(g).strip() for g in ka)):
            return False, ("venture-class spec has no kpi_artifacts — the work-product scorecard "
                           "cannot classify PRODUCT without them. Add kpi_artifacts: [<glob>, ...] "
                           "naming the reader/buyer-facing artifacts this venture ships.")
    return True, ""


def _write_governance(target, name, spec):
    """Materialize governance at birth: state/term-sheet.json (read by the monitor's kill_line
    playbook) + state/directives.json (the compiled directive state memory.py/route_tier read —
    see directives.py). Best-effort: a failure here is logged, never blocks the spawn (the
    governance CHECK above already gated a venture spec)."""
    try:
        ts = spec.get("term_sheet") if isinstance(spec, dict) else None
        state = target / "home" / "state"
        state.mkdir(parents=True, exist_ok=True)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if isinstance(ts, dict):
            sheet = dict(ts)
            sheet.setdefault("action_on_pass", "pause pod + escalate")
            sheet.setdefault("set_by", f"spawn spec {now}")
            (state / "term-sheet.json").write_text(json.dumps(sheet, indent=2) + "\n")
        directives = []
        mission = str((spec or {}).get("mission") or "").strip()
        kpi = str((ts or {}).get("kpi") or (spec or {}).get("kpi") or "").strip()
        if isinstance(ts, dict):
            directives.append({
                "id": f"{name}-term-sheet", "status": "active", "priority": 1,
                "date": now[:10],
                "text": (f"TERM SHEET. ONE KPI: {kpi or '(see term-sheet.json)'}. "
                         f"KILL-LINE: {ts.get('kill_line')} — mechanical (the fleet monitor reads "
                         "state/term-sheet.json and pauses you when the line passes; renewal is a "
                         "board decision). Work that does not serve the KPI does not get ticks.")})
        if mission:
            directives.append({"id": f"{name}-mission", "status": "active",
                               "priority": 10, "date": now[:10], "text": mission[:1200]})
        if directives:
            (state / "directives.json").write_text(json.dumps(
                {"version": 1, "updated": now, "compiled_by": "spawn_watcher",
                 "source": "spawn spec", "directives": directives}, indent=2) + "\n")
        # Scorecard classification config (analytics plan P0) — product globs from the spec.
        ka = [str(g) for g in ((spec or {}).get("kpi_artifacts") or []) if str(g).strip()]
        if ka:
            (state / "scorecard-config.json").write_text(json.dumps(
                {"kpi_artifacts": ka,
                 "tooling_paths": (spec or {}).get("tooling_paths") or ["bin/**", "work/**/*.py", "work/**/*.sh"],
                 "set_by": f"spawn spec {now}"}, indent=2) + "\n")
    except Exception as e:
        print(f"  ⚠ {name}: governance files not fully written ({e}) — write them by hand")


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
    spec_data = _load_spec(spec_path)
    ok, why = _governance_check(spec_data)
    if not ok:
        return _fail(why)

    print(f"  → graduating {name} → {target}")
    new = subprocess.run([sys.executable, str(ENCLAVE), "new", name, "--dir", str(target),
                          "--image-only", "--spec", str(spec_path), "--yes"],
                         capture_output=True, text=True)
    if new.returncode != 0:
        return _fail("enclave new failed:\n" + (new.stderr or new.stdout))
    _write_governance(target, name, spec_data or {})
    # Apply any secrets staged alongside the spec (real env files written by whoever queued the spawn,
    # e.g. the console: existing creds copied from the library + new name/value pairs). Overwrites the
    # placeholder files `enclave new` made. Staging is consumed + removed so values don't linger.
    staged = queue / "secrets-staging" / name
    if staged.is_dir():
        dst = target / "secrets"
        dst.mkdir(parents=True, exist_ok=True)
        # Explicit ownership HANDOVER: a `.handover` file in the staging dir lists filenames whose
        # ownership is deliberately transferred to THIS pod (the real workflow — an experimenter proves
        # a channel, then hands the account over). Anything not listed stays owned by its current owner.
        handover = set()
        hf = staged / ".handover"
        if hf.exists():
            handover = {ln.strip() for ln in hf.read_text(errors="ignore").splitlines() if ln.strip()}
        shared, owners = _ledger()
        n = refused = 0
        for f in staged.glob("*.env"):
            try:
                content = f.read_text(errors="ignore")
            except OSError:
                content = ""
            # STRUCTURAL guardrail: never mount a payment/legal-identity credential into a pod.
            if _is_payment_or_legal_cred(f.name, content):
                q = queue / "secrets-refused" / name        # quarantine OUTSIDE the pod (not mounted)
                q.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(q / f.name))
                try:
                    os.chmod(q / f.name, 0o600)
                except OSError:
                    pass
                _escalate(target, name, f"refused to mount '{f.name}' — it looks like a payment/"
                          f"legal-identity credential. The operator owns money + legal authority; a pod "
                          f"cannot be given a way to spend. Quarantined at _queue/secrets-refused/{name}/"
                          f"{f.name}. If you truly intend this pod to transact, place it out-of-band.")
                _audit("spawn-secrets", name, "refused",
                       f"{f.name}: payment/legal credential — not mounted")
                print(f"  ⛔ {name}: refused to mount '{f.name}' (payment/legal cred → operator); quarantined")
                refused += 1
                continue
            # OWNERSHIP guardrail: a credential another agent owns must not land in this pod (the sprawl
            # that let a fiction identity into the experimenter pod). Refuse + quarantine + escalate,
            # UNLESS this staging explicitly declares a handover (then transfer ownership in the ledger).
            owner = owners.get(f.name)
            if owner and owner != name and f.name not in shared:
                if f.name in handover:
                    _record_handover(f.name, name)
                    _audit("spawn-secrets", name, "handover", f"{f.name}: ownership {owner} → {name}")
                    print(f"  ⇄ {name}: ownership handover of '{f.name}' from {owner} (declared)")
                else:
                    q = queue / "secrets-refused" / name
                    q.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(q / f.name))
                    try: os.chmod(q / f.name, 0o600)
                    except OSError: pass
                    _escalate(target, name, f"refused to mount '{f.name}' — it is OWNED BY {owner} (a live "
                              f"identity another agent operates). Mounting it into {name} risks that agent's "
                              f"account (pacing/spam-flag). Quarantined at _queue/secrets-refused/{name}/"
                              f"{f.name}. If this is a deliberate transfer, add '{f.name}' to a `.handover` "
                              f"file in the staging dir.")
                    _audit("spawn-secrets", name, "refused", f"{f.name}: owned by {owner}, no handover")
                    print(f"  ⛔ {name}: refused '{f.name}' (owned by {owner}); quarantined")
                    refused += 1
                    continue
            shutil.copy2(f, dst / f.name)
            try:
                os.chmod(dst / f.name, 0o600)
            except OSError:
                pass
            n += 1
        shutil.rmtree(staged, ignore_errors=True)
        _audit("spawn-secrets", name, "applied", f"{n} file(s); {refused} refused")
        print(f"  · applied {n} staged secret file(s) to {name}"
              + (f" — ⛔ {refused} payment/legal cred(s) refused (see escalations)" if refused else ""))
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
