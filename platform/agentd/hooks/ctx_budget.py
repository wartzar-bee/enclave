#!/usr/bin/env python3
"""ctx_budget.py — cost-budget warning (PostToolUse) + hard-block (PreToolUse).

The agent plans a $ budget per work-PACKAGE (state/budget.json {package, soft_usd, hard_usd}); the parser
writes live spend (state/.ctx-budget.json {cost_est, ...}). Three escalating layers (the runtime adds the
4th — a watchdog cutoff that kills the tick at hard):
  • soft   (PostToolUse, cost ≥ soft)        → warn: "wrap up, refresh handoff, no big reads"
  • BLOCK  (PreToolUse, cost ≥ block≈0.75·hard) → block the WORK tools (Bash/Edit/Task/Web…) so the ONLY
            thing the agent can do is finalize state/handoff.md + finish. A block can't be ignored (a
            warning can — proven). This FORCES the agent to prep its own handoff before the cutoff.
  • (hard cutoff = the runtime watchdog kills the tick at cost ≥ hard.)
The agent's plan budget is PRIMARY; CTX_COST_HARD_USD is the global $ floor. Fail-OPEN — never wedge a tick.

Register BOTH: PreToolUse (matcher Bash|Edit|MultiEdit|Task|WebFetch|WebSearch) + PostToolUse (matcher *).
exit 0 = allow / nothing; exit 2 + stderr = (Pre) block the tool / (Post) surface the steer.
"""
import os, sys, json, pathlib

COST_SOFT = float(os.environ.get("CTX_COST_SOFT_USD", "2.0"))   # min/default soft cap (clamp plan UP to this)
COST_HARD = float(os.environ.get("CTX_COST_HARD_USD", "3.5"))   # min/default hard cap (clamp plan UP to this)
COST_HARD_MAX = float(os.environ.get("CTX_COST_HARD_MAX", "6.0"))  # absolute runaway ceiling (clamp plan DOWN)
BLOCK_FRAC = float(os.environ.get("CTX_COST_BLOCK_FRAC", "0.75"))   # block work tools at this fraction of hard
OCC_HARD = int(os.environ.get("CTX_OCC_HARD_TOKENS", "400000"))     # window-safety occupancy net
HANDOFF = "state/handoff.md"
WORK_TOOLS = {"Bash", "Edit", "MultiEdit", "Task", "WebFetch", "WebSearch"}  # blocked once near budget


def _agent_dir():
    d = os.environ.get("AGENT_DIR")
    if d and pathlib.Path(d, "state").is_dir():
        return pathlib.Path(d)
    for p in pathlib.Path(__file__).resolve().parents:
        if (p / "state").is_dir() and (p / ".claude").is_dir():
            return p
    return pathlib.Path("/agent")


def _read_json(p, default):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return default


def main():
    try:
        ev = json.load(sys.stdin)
    except Exception:
        ev = {}
    try:
        st = _agent_dir() / "state"
        b = _read_json(st / ".ctx-budget.json", None)
        if not b:
            sys.exit(0)
        cost = float(b.get("cost_est", 0) or 0)
        occ = int(b.get("ctx_tokens", 0) or 0)
        plan = _read_json(st / "budget.json", {})
        # Budget = a runaway CAP: clamp the plan UP to the floor (a too-tight self-budget thrashes — a
        # warm-resume tick spends ~$1+ on turn-1 cache rewarm) and DOWN to the absolute max.
        hard = min(max(float(plan.get("hard_usd") or COST_HARD), COST_HARD), COST_HARD_MAX)
        soft = min(max(float(plan.get("soft_usd") or COST_SOFT), COST_SOFT), hard)
        pkg = (str(plan.get("package") or ""))[:60]
        pkgs = f" (package: {pkg})" if pkg else ""
        is_post = "tool_response" in ev          # PostToolUse carries the result; PreToolUse does not
        tool = ev.get("tool_name", "")

        # ---- PreToolUse: HARD-BLOCK the work tools once spend nears the budget ----
        if not is_post:
            block_at = min(BLOCK_FRAC * hard, hard)
            near = (cost >= block_at) or (occ >= OCC_HARD)
            if near and tool in WORK_TOOLS:
                sys.stderr.write(
                    f"[ctx_budget] \U0001f6d1 spend ${cost:.2f} ≥ ${block_at:.2f} (near your ${hard:.2f} "
                    f"budget){pkgs}. WORK TOOLS ARE BLOCKED. The ONLY thing to do now: (1) finalize "
                    f"{HANDOFF} (objective · now-doing · EXACT next step · key files path:line · decisions · "
                    f"blockers) with the Write tool; (2) write state/tick-status.json "
                    f"{{\"status\":\"continue\",\"session\":\"clear\"}}; (3) finish. (Write/Read/Grep stay "
                    f"open so you can compose the handoff.)\n")
                sys.exit(2)
            sys.exit(0)

        # ---- PostToolUse: graduated WARNING (dedup once per level per tick) ----
        order = {"none": 0, "soft": 1, "hard": 2}
        level = "hard" if (cost >= hard or occ >= OCC_HARD) else ("soft" if cost >= soft else "none")
        warned = _read_json(st / ".ctx-warned", {}).get("level", "none")
        if order[level] <= order.get(warned, 0):
            sys.exit(0)
        (st / ".ctx-warned").write_text(json.dumps({"level": level}))
        if level == "hard":
            why = (f"context occupancy {occ//1000}k near the window limit" if occ >= OCC_HARD
                   else f"spend ${cost:.2f} ≥ hard budget ${hard:.2f}")
            sys.stderr.write(
                f"[ctx_budget] \U0001f6d1 {why}{pkgs}. STOP NOW: finalize {HANDOFF}, write "
                f"state/tick-status.json {{\"status\":\"continue\",\"session\":\"clear\"}}, then finish. "
                f"Work tools are now blocked — only the handoff + finish remain.\n")
            sys.exit(2)
        if level == "soft":
            sys.stderr.write(
                f"[ctx_budget] \U0001f4ca spend ${cost:.2f} ≥ soft budget ${soft:.2f}{pkgs}. Wrap up the "
                f"current sub-task soon: refresh {HANDOFF}, no big new reads, don't start a new sub-task — "
                f"you're approaching this package's budget (work tools get blocked at "
                f"${BLOCK_FRAC*hard:.2f}).\n")
            sys.exit(2)
    except SystemExit:
        raise
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
