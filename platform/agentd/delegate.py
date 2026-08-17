#!/usr/bin/env python3
"""
delegate.py — the manager's delegation primitive: hand ONE subtask to an isolated LOCAL worker.

"Claude manages, local does the labor." The manager (BRAIN=claude) plans a tick, then calls this to
delegate the actual implementation to a cheap/local model. The worker is local_agent.py run in
WORKER_MODE (its own guarded ReAct loop, restricted tools, no escalation/recursion). It does the work
in the repo and we return ONLY a JSON summary to the manager — the worker's intermediate steps go to
disk, never into the manager's context (token-frugal: the 136M-burn lesson). A verify command gates
quality: on failure the worker is re-invoked with the failure, bounded by --verify-retries.

Distilled (not ported) from Hermes delegate_tool/verification_stop + NemoClaw model-pinning. See
docs/DELEGATION.md.

Usage:
  python3 delegate.py --task "<subtask + acceptance>" [--kind code|write|analyze|classify]
      [--cwd <dir>] [--context-files a,b] [--verify "<shell cmd>"] [--verify-retries 2]
      [--max-steps 20] [--timeout 600] [--agent-dir /agent]

stdout = a single JSON object (the summary). Full worker trace → <agent-dir>/state/delegations/<id>.log
"""
import sys, os, re, json, time, argparse, subprocess, pathlib, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
LOCAL_AGENT = HERE / "local_agent.py"

# Worker models are CONFIGURATION, never framework constants.
#
# This table used to name vendor models inline, with a comment instructing whoever edited it to
# "keep this table in sync with policy.json" — a manual sync with nothing enforcing it. It went
# stale exactly as you would expect: all three entries pointed at qwen/qwen3-next-80b-a3b-instruct,
# NVIDIA retired it on 2026-07-27 (HTTP 410 Gone), and every delegation on two live pods failed for
# a WEEK — 54 calls, 0 successes — each logged as one innocuous $0 `brain_error` tick, so the fleet
# read as idle-and-cheap while the Claude manager silently absorbed all the delegated labour.
#
# A framework that hardcodes one vendor's catalogue entry breaks on that vendor's next retirement,
# and enclave is a product other people run. So there is no model name in this file. Resolution:
#
#   1. DELEGATE_MODEL_<KIND>            — explicit per-kind override, always wins
#   2. $DELEGATE_POLICY                 — path to a policy.json
#   3. $TOOLS_ROOT/llm/policy.json      — the SAME file route.mjs reads, so the duplication that
#                                         caused the stale table cannot recur
#   4. nothing                          — raise, naming exactly what to set. NEVER guess a model:
#                                         guessing is what produced a week of silent failure.
DELEGATE_POOL = os.environ.get("DELEGATE_POOL", "nvidia")

# The delegation kinds a manager may ask for. OUR vocabulary, deliberately separate from the
# capability names policy.json uses ("default"/"fast"/"coder") — _KIND_ALIASES maps between them.
# It replaces the deleted KIND_MODEL table, which `--kind`'s argparse `choices` still referenced
# after the de-hardcoding commit: every invocation died with `NameError: KIND_MODEL` before it
# parsed a single argument. `test_delegate.py` now runs `--help` for exactly this reason — the
# commit verified _model_for() against the live policy and never ran the CLI it had broken.
DELEGATE_KINDS = ("code", "write", "analyze", "classify")
_KIND_ALIASES = {"analyze": ("analyze", "analysis"), "classify": ("classify", "fast")}


def _policy_path():
    p = os.environ.get("DELEGATE_POLICY")
    if p:
        return pathlib.Path(p)
    # Same resolution as local_agent._policy(): the compose files mount workspace tools at
    # $TOOLS_ROOT/tools, so the file lives at <root>/tools/llm/policy.json. The old "<root>/llm/"
    # path existed nowhere, which left _model_for() raising on every pod — broken-loud, but broken.
    root = pathlib.Path(os.environ.get("TOOLS_ROOT", "/workspace"))
    for cand in (root / "tools" / "llm" / "policy.json", root / "llm" / "policy.json"):
        if cand.exists():
            return cand
    return root / "tools" / "llm" / "policy.json"


def _policy_models():
    """models.<DELEGATE_POOL> from policy.json, or {} if unreadable. Never raises."""
    try:
        return (json.loads(_policy_path().read_text()).get("models") or {}).get(DELEGATE_POOL) or {}
    except Exception:
        return {}


def _worker_base():
    """Endpoint for the worker pool. Config, not a constant — same reasoning as the models."""
    for env in ("DELEGATE_BASE", "NVIDIA_API_BASE"):
        if os.environ.get(env):
            return os.environ[env]
    try:
        pools = json.loads(_policy_path().read_text()).get("pools") or {}
        pool = pools.get(DELEGATE_POOL) or {}
        base = os.environ.get(pool.get("base_url_env", "")) or pool.get("base_url_default")
        if base:
            return base
    except Exception:
        pass
    raise RuntimeError(
        f"no endpoint for delegation pool {DELEGATE_POOL!r}: set DELEGATE_BASE, or provide a "
        f"policy.json at {_policy_path()} defining pools.{DELEGATE_POOL}.base_url_default"
    )




def _worker_key():
    """Resolve the worker API key (NVIDIA free) from env or the scoped secret mount."""
    k = os.environ.get("DELEGATE_KEY") or os.environ.get("NVIDIA_API_KEY")
    if k:
        return k
    for r in (os.environ.get("AGENT_DIR", "/agent"), os.environ.get("TOOLS_ROOT", "/workspace")):
        f = pathlib.Path(r) / ".secrets" / "nvidia.env"
        try:
            for ln in f.read_text().splitlines():
                if ln.startswith("NVIDIA_API_KEY="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def _model_for(kind):
    """Resolve the worker model for `kind`. Raises rather than guessing — see the note above."""
    explicit = os.environ.get(f"DELEGATE_MODEL_{kind.upper()}")
    if explicit:
        return explicit

    models = _policy_models()
    # policy.json names models by capability ("default"/"fast"/"coder"), not by our kind vocabulary.
    for key in _KIND_ALIASES.get(kind, (kind,)) + ("default",):
        if models.get(key):
            return models[key]

    raise RuntimeError(
        f"no worker model for kind={kind!r}: set DELEGATE_MODEL_{kind.upper()}, or provide a "
        f"policy.json at {_policy_path()} defining models.{DELEGATE_POOL}.default. "
        f"Refusing to guess — a stale hardcoded default is what silently broke 54 delegations "
        f"over a week when the vendor retired the model it named."
    )


def _prewarm(model, key, timeout=120):
    """Tiny call to validate the endpoint/model (and warm a local model if that's the base)."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 4}).encode()
    hdrs = {"Content-Type": "application/json"}
    if key:
        hdrs["Authorization"] = "Bearer " + key
    req = urllib.request.Request(_worker_base().rstrip("/") + "/chat/completions", data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception:
        return False


def _git_porcelain(cwd):
    try:
        out = subprocess.run(["git", "-C", cwd, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=10)
        return set(l[3:].strip() for l in out.stdout.splitlines() if l.strip())
    except Exception:
        return None


def _revert_unlisted(cwd, allow_files, before):
    """Bound the worker's blast radius: a weak local model strays off-task (overwrites/edits files it
    wasn't asked to). Revert every change the WORKER introduced that isn't in --allow-files. Only the
    worker's deltas (after - before) are considered, so the manager's pre-existing uncommitted work is
    untouched. tracked → `git checkout --`; untracked → `git clean -fdq`."""
    after = _git_porcelain(cwd)
    if before is None or after is None:
        return [], []
    allow = set(f.strip() for f in allow_files if f.strip())
    kept, reverted = [], []
    for line in sorted(after - before):
        f = line.split(" -> ")[-1].strip()
        if f in allow:
            kept.append(f); continue
        subprocess.run(["git", "-C", cwd, "checkout", "--", f], capture_output=True)
        subprocess.run(["git", "-C", cwd, "clean", "-fdq", "--", f], capture_output=True)
        reverted.append(f)
    return kept, reverted


def _build_task(args):
    parts = [args.task.strip()]
    if args.cwd:
        parts.append(f"\nWORK IN: {args.cwd} (use absolute paths under it). Do NOT touch anything outside it.")
    if args.context_files:
        parts.append("FIRST read these for context: " + ", ".join(args.context_files.split(",")))
    if args.verify:
        parts.append(f"ACCEPTANCE: your work must make this command exit 0 — `{args.verify}`. "
                     f"Run it yourself before calling finish; if it fails, fix and re-run.")
    parts.append("When done: VERIFY (read the file back; if code, run it), then call `finish` with a "
                 "2-5 line summary of WHAT you did and WHICH files you changed.")
    return "\n".join(parts)


def _run_worker(task, model, args, trace_path, extra=""):
    """Run local_agent.py in WORKER_MODE; capture its stdout to trace_path; return (rc, trace_text)."""
    env = dict(os.environ)
    env.update({
        "WORKER_MODE": "1",
        "DELEGATE_TASK": task + (("\n\n" + extra) if extra else ""),
        "LOCAL_BRAIN_MODEL": model,
        "LOCAL_BRAIN_BASE": _worker_base(),
        "LOCAL_BRAIN_KEY": _worker_key(),
        "LOCAL_MAX_STEPS": str(args.max_steps),
        "LOCAL_REQ_TIMEOUT": str(max(120, args.timeout // 2)),   # NVIDIA free is fast; generous floor
        "GUARD_HOOK": os.environ.get("GUARD_HOOK", "guard.py"),
        "AGENT_DIR": args.cwd or env.get("AGENT_DIR", "/agent"),
        "DELEGATION_ENFORCE": "off",          # the worker IS the laborer — never gate it
    })
    cwd = args.cwd or env.get("AGENT_DIR", "/agent")
    try:
        proc = subprocess.run(["python3", str(LOCAL_AGENT), cwd], env=env,
                              capture_output=True, text=True, timeout=args.timeout)
        trace = proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = e.stdout                                   # text=True can still hand back bytes on timeout
        if isinstance(out, (bytes, bytearray)):
            out = out.decode(errors="ignore")
        trace = (out or "") + f"\n[delegate] worker TIMED OUT after {args.timeout}s"
        rc = 124
    except Exception as e:                               # never let the worker invocation crash delegate.py
        trace = f"[delegate] worker invocation error: {type(e).__name__}: {e}"
        rc = 1
    try:
        with open(trace_path, "a") as f:
            f.write((trace or "") + "\n")
    except Exception:
        pass
    return rc, (trace or "")


def _summary_from_trace(trace):
    m = re.findall(r"\[local_agent\] finish:\s*(.+)", trace)
    if m:
        return m[-1].strip()
    # no finish → last few meaningful step lines
    steps = [l for l in trace.splitlines() if "[local_agent] step" in l]
    return (steps[-1].strip() if steps else "(worker produced no finish summary)")[:400]


def _run_verify(cmd, cwd, timeout):
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        tail = (p.stdout + p.stderr).strip().splitlines()[-20:]
        return p.returncode == 0, "\n".join(tail)
    except subprocess.TimeoutExpired:
        return False, f"verify timed out after {timeout}s"
    except Exception as e:
        return False, f"verify error: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--kind", default="code", choices=list(DELEGATE_KINDS))
    ap.add_argument("--cwd", default=os.environ.get("AGENT_DIR", "/agent"))
    ap.add_argument("--context-files", default="")
    ap.add_argument("--allow-files", default="",
                    help="comma list of files the worker may change; any OTHER file it touches is reverted (blast-radius guard)")
    ap.add_argument("--verify", default="")
    ap.add_argument("--verify-retries", type=int, default=2)
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--agent-dir", default=os.environ.get("AGENT_DIR", "/agent"))
    args = ap.parse_args()

    t0 = time.time()
    did = str(int(t0))
    state = pathlib.Path(args.agent_dir) / "state"
    (state / "delegations").mkdir(parents=True, exist_ok=True)
    trace_path = state / "delegations" / f"{did}.log"

    model = _model_for(args.kind)
    _prewarm(model, _worker_key())
    task = _build_task(args)
    before = _git_porcelain(args.cwd)

    status, verify_passed, verify_tail = "incomplete", None, ""
    rc, trace = _run_worker(task, model, args, trace_path)

    if args.verify:
        for attempt in range(args.verify_retries + 1):
            verify_passed, verify_tail = _run_verify(args.verify, args.cwd, min(args.timeout, 600))
            if verify_passed:
                status = "ok"
                break
            status = "verify_failed"
            if attempt < args.verify_retries:
                rc, trace = _run_worker(
                    task, model, args, trace_path,
                    extra=f"PREVIOUS ATTEMPT FAILED ACCEPTANCE: `{args.verify}` did not exit 0.\n"
                          f"Output tail:\n{verify_tail}\nDiagnose and FIX it, then re-verify.")
    else:
        status = "ok" if rc == 0 else "incomplete"

    reverted = []
    if args.allow_files:
        _, reverted = _revert_unlisted(args.cwd, args.allow_files.split(","), before)

    after = _git_porcelain(args.cwd)
    files = sorted(after - before) if (before is not None and after is not None) else []
    files = [f.split(" -> ")[-1] for f in files]

    summary = {
        "status": status,
        "summary": _summary_from_trace(trace),
        "files_changed": files,
        "reverted_off_task": reverted,
        "verify": {"cmd": args.verify, "passed": verify_passed, "tail": verify_tail} if args.verify else None,
        "model": model, "kind": args.kind,
        "elapsed_s": round(time.time() - t0, 1),
        "worker_log": str(trace_path),
    }
    # one-line ledger (read by delegation_guard's fallback check + tick logging)
    try:
        with open(state / "delegations.log", "a") as f:
            f.write(json.dumps({"id": did, "ts": time.strftime("%FT%TZ", time.gmtime()),
                                "kind": args.kind, "model": model, "status": status,
                                "files": files, "verify_passed": verify_passed}) + "\n")
    except Exception:
        pass

    print(json.dumps(summary, indent=2))
    sys.exit(0 if status == "ok" else 0)   # always 0: the manager reads status from JSON, decides next


if __name__ == "__main__":
    main()
