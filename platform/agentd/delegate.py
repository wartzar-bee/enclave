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

# kind → local worker model (env override DELEGATE_MODEL_<KIND>). Defaults = models we verified emit
# real content as an instruct/coder worker (NOT a reason-only model like Nemotron).
KIND_MODEL = {
    "code":     "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
    "write":    "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
    "analyze":  "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit",
    "classify": "mlx-community/Qwen3-8B-4bit",
}
LOCAL_BASE = os.environ.get("LOCAL_BRAIN_BASE", "http://host.docker.internal:8081/v1")


def _model_for(kind):
    return os.environ.get(f"DELEGATE_MODEL_{kind.upper()}") or KIND_MODEL.get(kind, KIND_MODEL["code"])


def _prewarm(model, timeout=200):
    """One tiny call so a cold model-load doesn't eat the worker's step budget / time out step 1."""
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "hi"}],
                       "max_tokens": 4}).encode()
    req = urllib.request.Request(LOCAL_BASE.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
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
        "LOCAL_BRAIN_BASE": LOCAL_BASE,
        "LOCAL_MAX_STEPS": str(args.max_steps),
        "LOCAL_REQ_TIMEOUT": str(max(300, args.timeout // 2)),   # slow local 30B: generous per-call
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
    ap.add_argument("--kind", default="code", choices=list(KIND_MODEL))
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
    _prewarm(model)
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
