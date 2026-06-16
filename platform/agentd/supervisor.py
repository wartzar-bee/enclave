#!/usr/bin/env python3
"""
supervisor.py — the OFF-OPUS orchestrator for a local-brain worker pod.

The whole point of a local agent is to run off the Claude cap. A frontier model (Opus) supervising it
defeats that — every check-in re-reads a growing context (the real token sink). So supervision runs
OFF-OPUS — a cheap EXTERNAL planner + DETERMINISTIC checks, holding all the state it wants for free.
Opus is touched NEVER in the loop — only a human (or a rare Opus pass) reads escalations.

It steers the worker PURELY through shared agent-dir files (work.json / phase-goal.txt /
escalations.log) and has no host-only dependency, so it runs as a sibling process IN the pod
container — `agentloop.py` spawns + watchdogs it for BRAIN=local pods (SUPERVISE=auto|on|off), 2026-06-13.
(It can still be run standalone — `python3 supervisor.py <agent-dir>` — for a host-side / dev loop.)

Roles (none are Opus):
  • PLAN (per phase, infrequent) — operator writes state/phase-goal.txt (or a rare Opus /plan pass).
  • BREAK DOWN — an EXTERNAL cheap planner (OpenRouter, e.g. google/gemini-2.5-flash) decomposes the goal
    into bounded tasks, each with a DETERMINISTIC `verify` shell command. (Bake-off winner; r1 rejected.)
  • EXECUTE — the local 80B worker pod (its own agentloop ticks).
  • VERIFY — deterministic code (memory.py work-done gate runs each item's verify). Not a model.
  • SUPERVISE — this loop: keep the queue full, detect stuck/repeated-fail, escalate to a FILE.
  • ESCALATE — append to state/escalations.log; a human/Opus reads it out-of-band. Never calls Opus.

  python3 supervisor.py <agent-dir> [--once]
Env: SUP_POLL(300) SUP_MIN_OPEN(2) SUP_PLANNER(google/gemini-2.5-flash) SUP_STUCK_FAILS(3)
"""
import os, sys, json, time, subprocess, pathlib, urllib.request

HERE = pathlib.Path(__file__).resolve().parent
WORKSPACE = pathlib.Path(os.environ.get("TOOLS_ROOT", "/workspace"))


def _secret(name, key):
    for base in (WORKSPACE / ".secrets", HERE.parents[1] / ".secrets"):
        try:
            for ln in (base / name).read_text().splitlines():
                if ln.strip().startswith(key + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            continue
    return ""


PLANNER_SYS = (
    "You are the PLANNER for an autonomous local-LLM build agent. Decompose the GOAL into 4-8 BOUNDED, "
    "independently-verifiable tasks. Output ONLY a JSON array; each item {\"text\":\"...\",\"verify\":\"<cmd>\"}. "
    "RULES for verify: a deterministic shell command run from /agent that exits 0 ONLY if the task is genuinely "
    "done. Make it ROBUST, not brittle: prefer case-insensitive/multi-pattern greps (grep -qiE 'a|b') or a "
    "FUNCTIONAL check (curl the served page, a headless render, node --check), NEVER an exact-string grep of a "
    "name you invented, and NEVER a trivially-passing command (true/ls/echo). One file path per task where possible."
)


def plan(goal, model, key):
    """Call the external cheap planner → list of {text, verify}. [] on any failure (supervisor retries)."""
    body = json.dumps({"model": model, "temperature": 0.3, "max_tokens": 1600,
                       "messages": [{"role": "system", "content": PLANNER_SYS},
                                    {"role": "user", "content": f"GOAL: {goal}"}]}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json"); req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            txt = json.load(r)["choices"][0]["message"]["content"]
    except Exception as e:
        log(f"planner call failed: {e}"); return []
    # tolerate ```json fences
    s = txt.strip()
    if "```" in s:
        s = s.split("```")[1].lstrip("json").strip() if len(s.split("```")) > 1 else s
    try:
        items = json.loads(s)
        return [{"text": i["text"], "verify": i.get("verify", "")} for i in items
                if isinstance(i, dict) and i.get("text")]
    except Exception as e:
        log(f"planner output unparseable: {e}"); return []


def log(m):
    print(f"{time.strftime('%FT%TZ', time.gmtime())} — [supervisor] {m}", flush=True)


def memory(agent_dir, *args):
    mem = pathlib.Path(agent_dir) / "bin" / "memory.py"
    p = subprocess.run(["python3", str(mem), "--base", str(agent_dir), *args],
                       capture_output=True, text=True)
    return p.stdout.strip()


def open_items(agent_dir):
    try:
        w = json.loads((pathlib.Path(agent_dir) / "work.json").read_text())
        return [i for i in w if i.get("status") in ("todo", "doing")]
    except Exception:
        return []


def escalate(agent_dir, msg):
    f = pathlib.Path(agent_dir) / "state" / "escalations.log"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as h:
        h.write(f"{time.strftime('%FT%TZ', time.gmtime())} ESCALATE :: {msg}\n")
    log(f"ESCALATED: {msg}")


def tick(agent_dir, planner, key, min_open, stuck_fails):
    ad = pathlib.Path(agent_dir)
    goal_f = ad / "state" / "phase-goal.txt"
    goal = goal_f.read_text().strip() if goal_f.exists() else ""
    openi = open_items(agent_dir)
    # 1) refill: if the queue is low and there's a phase goal, plan more bounded tasks (external, cheap).
    if goal and len(openi) <= min_open:
        # GROUND the planner in the REAL file tree so it doesn't hallucinate paths (its verify cmds must
        # target files that actually exist; otherwise the gate rejects them and we burn cycles).
        try:
            tree = subprocess.run(["bash", "-lc", "cd " + str(ad) + " && find work -maxdepth 3 -type f 2>/dev/null | head -40"],
                                  capture_output=True, text=True, timeout=15).stdout.strip()
        except Exception:
            tree = ""
        grounded = goal + ("\n\nThe project's ACTUAL files (verify commands MUST reference these real paths, "
                           "relative to /agent):\n" + tree if tree else "")
        new = plan(grounded, planner, key)
        if new:
            for it in new:
                memory(agent_dir, "work", "add", it["text"], "--verify", it["verify"])
            log(f"planned {len(new)} task(s) for goal: {goal[:60]}")
        else:
            escalate(agent_dir, f"planner returned no usable tasks for goal: {goal[:80]} — needs a human/Opus plan")
    # 2) stuck detection: an item with many doing-cycles / repeated verify fails → escalate, don't spin.
    for it in openi:
        if it.get("fails", 0) >= stuck_fails:
            escalate(agent_dir, f"work #{it['id']} stuck after {it['fails']} verify-fails: {it['text'][:80]}")
    # (worker pod executes + the memory.py verify-gate enforces 'done' — no model verification here.)


def main():
    ad = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent")
    once = "--once" in sys.argv
    poll = int(os.environ.get("SUP_POLL", "300"))
    planner = os.environ.get("SUP_PLANNER", "google/gemini-2.5-flash")
    key = _secret("openrouter.env", "OPENROUTER_API_KEY")
    min_open = int(os.environ.get("SUP_MIN_OPEN", "2"))
    stuck = int(os.environ.get("SUP_STUCK_FAILS", "3"))
    log(f"start dir={ad} planner={planner} poll={poll}s (OPUS NOT in this loop)")
    while True:
        try:
            tick(ad, planner, key, min_open, stuck)
        except Exception as e:
            log(f"tick error: {e}")
        if once:
            break
        time.sleep(poll)


if __name__ == "__main__":
    main()
