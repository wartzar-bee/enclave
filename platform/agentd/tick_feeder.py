#!/usr/bin/env python3
"""tick_feeder.py — stream-json stdin feeder: epoch continuation + graduated budget INJECTOR + cutoff.

Runs as the stdin writer for `claude -p --input-format stream-json`. It:
  1. Delivers the tick prompt as the first user turn.
  2. CONTEXT EPOCHS (2026-07-26, adaptive ticks): when the agent finishes an increment (a `result`
     event → usage_capture touches state/.tick-result), the feeder decides — deterministically,
     off-model — whether to inject the NEXT increment into the SAME cache-hot session or end the
     epoch. Continue while context occupancy is lean, spend is under the soft budget, the 5h/7d
     subscription windows have headroom, and open work remains; otherwise wrap up (handoff) + close.
     Measured why: with one-increment ticks every boot re-pays ~90-110k cache-write + the re-derive
     turns (~2/3 of tick cost was context carriage); in one held-open session the second increment's
     cache-write drops to a few hundred tokens (probe-verified 2026-07-26). The pause/resume boundary
     becomes the CONTEXT WINDOW, not the clock. EPOCH_TICKS=0 restores one-increment ticks.
     This is NOT the forbidden warm-timer loop: one bounded process, no gaps, no --resume; the span
     is capped by occupancy + $ + wall + increments, all enforced here (off-Opus python).
  3. Watches live spend (state/.ctx-budget.json, written per assistant turn by usage_capture.py) and
     INJECTS graduated user messages as the agent approaches its $ budget. An injected USER message is
     a first-class operator turn the model OBEYS (empirically proven — unlike an ignorable stderr hook):
       • warn1  (cost ≥ soft)            → "wrap up the current sub-task, refresh handoff, no new sub-task"
       • warn2  (cost ≥ soft+0.6·(hard-soft)) → "finalize handoff NOW — you're about to be cut off"
       • STOP   (cost ≥ hard)            → "write handoff + tick-status{session:clear} + finish NOW"
  4. Hard backstop: if the agent hasn't finished within GRACE after STOP, touches state/.cost-cutoff and
     kills claude (the cap holds even if the agent ignores the message).

Budget plan: state/budget.json {package, soft_usd, hard_usd} (agent-authored); clamped to the env floors
(--soft-floor / --hard-floor). Fail-safe: the prompt is always delivered; the kill backstop guarantees
the cap regardless of what the agent does.
Epoch env: EPOCH_TICKS(1) CTX_EPOCH_TOKENS(140000) EPOCH_MAX_INCREMENTS(8) EPOCH_WALL_SEC(5400).
"""
import os, sys, json, time, argparse, subprocess, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import statefile

POLL = float(os.environ.get("FEED_POLL_SEC", "2"))

WARN1 = ("\U0001F4CA BUDGET — you've spent ${cost:.2f} of your ${hard:.2f} package budget (soft "
         "${soft:.2f}). BANK THIS CHUNK, don't abandon the task: this is a per-TICK cap, not a reason to "
         "avoid a hard/multi-file task. If the top task is big, it's a MULTI-TICK task — finish the current "
         "tick-sized CHUNK, write state/handoff.md with the EXACT next chunk, and stop (the NEXT fresh tick "
         "continues it cheaply). Do NOT start an unrelated sub-task and do NOT downgrade to easy filler to "
         "dodge the hard one — the hard task stays the priority next tick.")
WARN2 = ("⚠️ BUDGET — ${cost:.2f} of ${hard:.2f}. You are about to be cut off. FINALIZE "
         "state/handoff.md now (objective · now-doing · EXACT next step · key files "
         "path:line · decisions · blockers) and stop opening new work.")
STOP  = ("\U0001F6D1 STOP NOW — ${cost:.2f} ≥ your ${hard:.2f} budget. Do ONLY this, immediately: "
         "(1) finish writing state/handoff.md; (2) write /agent/state/tick-status.json "
         "{{\"status\":\"continue\",\"session\":\"clear\"}}; (3) finish this turn. You will be "
         "hard-stopped in ~{grace}s.")
# Turn-cap wrap-up (2026-07-04 fix #4/#9-adjacent): MAX_TURNS used to GUILLOTINE mid-work
# (57 ticks / $111 on forgepod died error_max_turns, the truncated work re-derived next tick).
# Inject a wrap-up order near the cap so the agent banks state and exits cleanly instead.
TURNWRAP = ("⏳ TURN CAP — you are at turn {turn} of a {max_turns}-turn tick cap. WRAP UP NOW: "
            "bank the current chunk (commit if applicable), write state/handoff.md with the EXACT "
            "next step, write /agent/state/tick-status.json {{\"status\":\"continue\"}}, and finish this "
            "turn cleanly. Do NOT start anything new — hitting the cap wipes this tick's unsaved "
            "work and the next tick pays to re-derive it.")
# Epoch continuation: the increment landed and context is still lean — keep working in THIS session
# instead of paying a fresh boot next tick. Handoff discipline is adaptive: mid-epoch the session
# itself carries continuity, so a full handoff rewrite per increment is waste — it's demanded once,
# at epoch end (EPOCHEND below or the WARN/STOP ladder).
CONT = ("✅ Increment #{inc} banked — your context is still lean ({ctx}k tokens, ${cost:.2f} spent), "
        "so CONTINUE IN THIS SESSION: take the NEXT single highest-leverage increment toward your KPI, "
        "same rules (do it to a verifiable done, log the decision, update work.json, write "
        "/agent/state/tick-status.json when the increment completes). Everything you read is still in "
        "context — do NOT re-read inbox/plans/capabilities unless they changed. Skip the full "
        "state/handoff.md rewrite mid-session (you'll be told when to wrap up); keep it to a one-line "
        "pointer if you must. If nothing actionable remains, write /agent/state/tick-status.json "
        "{{\"status\":\"idle\"}} (or \"blocked\" + waiting_on) and finish.")
EPOCHEND = ("\U0001F3C1 EPOCH END ({reason}). Do ONLY this now: (1) finalize state/handoff.md "
            "(objective · now-doing · EXACT next step · key files path:line · decisions · blockers) "
            "so the next fresh session continues cheaply; (2) write /agent/state/tick-status.json — "
            "{{\"status\":\"continue\"}} if actionable work remains, else \"idle\"/\"blocked\" with "
            "waiting_on; (3) finish this turn. Do NOT start new work.")


def umsg(text):
    return json.dumps({"type": "user", "message": {"role": "user",
                       "content": [{"type": "text", "text": text}]}})


def next_injection(cost, turn, soft, hard, max_turns, sent):
    """PURE decision: which injection (if any) fires now. Order matters — STOP wins, then the
    turn-cap wrap-up (independent of $), then the graduated $ warnings. `sent` dedups. Unit-tested."""
    if sent.get("stop"):
        return None                       # STOP already delivered — nothing may follow it
    if cost >= hard:
        return "stop"
    if max_turns and turn >= max(3, int(max_turns * 0.8)) and not sent.get("turnwrap"):
        return "turnwrap"
    if cost >= soft + (hard - soft) * 0.6 and not sent.get("w2"):
        return "w2"
    if cost >= soft and not sent.get("w1"):
        return "w1"
    return None


def epoch_decision(*, subtype_ok, ctx_tokens, cost, soft, epoch_tokens, increments, max_increments,
                   elapsed, wall_sec, status, has_work, cap_ok, stop_sent, handoff_fresh):
    """PURE epoch decision at a result boundary (unit-tested). Returns (action, reason):
      continue — inject the next increment into the same cache-hot session
      finalize — one last cycle to write the handoff, then close
      close    — end the epoch now (handoff already fresh, or the agent already wrapped/parked)
    Order matters: agent declarations (clear/idle/blocked) and errors close unconditionally; the
    stop conditions (occupancy/$/increments/wall/cap) are checked BEFORE "work remains" so a full
    context can never buy another increment; work-remains is what separates continue from close."""
    st = str((status or {}).get("status") or "").strip().lower()
    sess = str((status or {}).get("session") or "").strip().lower()
    if stop_sent:
        return "close", "budget STOP already delivered"
    if not subtype_ok:
        return "close", "increment errored"
    if sess in ("clear", "fresh", "reset", "new"):
        return "close", "agent signalled session clear"
    if st in ("idle", "blocked"):
        return "close", f"agent declared {st}"
    wrap = "close" if handoff_fresh else "finalize"
    if ctx_tokens >= epoch_tokens:
        return wrap, f"context {ctx_tokens // 1000}k >= epoch cap {epoch_tokens // 1000}k"
    if cost >= soft:
        return wrap, f"spend ${cost:.2f} >= soft ${soft:.2f}"
    if increments >= max_increments:
        return wrap, f"increment cap {max_increments} reached"
    if wall_sec and elapsed >= wall_sec:
        return wrap, f"epoch wall {int(elapsed)}s >= {wall_sec}s"
    if not cap_ok:
        return wrap, "subscription window near its floor"
    if st == "continue" or has_work:
        return "continue", f"work remains, context lean ({ctx_tokens // 1000}k)"
    return "close", "no open work"


def read_json(p, d):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return d


def _read_status(state_dir):
    """The agent's tick-status.json, checked in its home state/ AND the cwd-relative spots agents
    actually write to (same alternates agentloop recovers from). Read-only — the caller decides
    whether to consume it."""
    alts = [pathlib.Path(state_dir) / "tick-status.json"]
    for base in (os.environ.get("WORK_DIR"), "/workspace", "/work"):
        if base:
            p = pathlib.Path(base) / "state" / "tick-status.json"
            if p not in alts:
                alts.append(p)
    for f in alts:
        d = read_json(f, None)
        if isinstance(d, dict):
            return d, f
    return None, None


def _has_open_work(agent_dir):
    # statefile.open_work is the ONE work.json normaliser (shared with agentloop + memory). This
    # reader used to iterate the file directly and treat a {"items":[...]} dict as empty — so a
    # dict-shaped backlog closed the epoch as "no open work" while agentloop's wake-gate saw the
    # backlog. Reading through the shared normaliser removes that disagreement.
    return statefile.has_open_work(pathlib.Path(agent_dir) / "work.json")


def _cap_ok():
    """Mid-epoch subscription-window check: the runtime's boot-time guard decision goes stale over a
    long epoch, so re-ask claude_usage.py (cached, cheap) at each boundary. Fail-OPEN — only an
    explicit 75 (defer) ends the epoch; any error/missing helper means headroom."""
    helper = pathlib.Path(__file__).resolve().parent / "claude_usage.py"
    if not helper.exists():
        return True
    try:
        rc = subprocess.run(
            [sys.executable, str(helper), "guard",
             "--session-floor", os.environ.get("STUDIO_SESSION_LIMIT_PCT_FLOOR", "90"),
             "--weekly-floor", os.environ.get("STUDIO_WEEKLY_LIMIT_PCT_FLOOR", "90"),
             "--session-warn", os.environ.get("STUDIO_SESSION_WARN_PCT", "70"),
             "--weekly-warn", os.environ.get("STUDIO_WEEKLY_WARN_PCT", "85")],
            capture_output=True, timeout=30).returncode
        return rc != 75
    except Exception:
        return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fifo", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--state", required=True)              # the agent's state/ dir
    ap.add_argument("--soft-floor", type=float, default=float(os.environ.get("CTX_COST_SOFT_USD", "2.0")))
    ap.add_argument("--hard-floor", type=float, default=float(os.environ.get("CTX_COST_HARD_USD", "3.5")))
    ap.add_argument("--hard-max", type=float, default=float(os.environ.get("CTX_COST_HARD_MAX", "6.0")))
    ap.add_argument("--grace", type=float, default=float(os.environ.get("CTX_STOP_GRACE_SEC", "60")))
    a = ap.parse_args()

    st = pathlib.Path(a.state)
    agent_dir = st.resolve().parent
    bud = st / ".ctx-budget.json"
    sentinel = st / ".tick-result"
    cutoff = st / ".cost-cutoff"
    for f in (sentinel, cutoff):
        try:
            f.unlink()
        except Exception:
            pass

    try:
        prompt = pathlib.Path(a.prompt_file).read_text()
    except Exception as e:
        sys.stderr.write(f"[feeder] cannot read prompt: {e}\n")
        sys.exit(1)

    # Epoch config (see module docstring). EPOCH_TICKS=0 → legacy single-increment behavior.
    epochs_on = os.environ.get("EPOCH_TICKS", "1") != "0"
    epoch_tokens = int(os.environ.get("CTX_EPOCH_TOKENS", "140000") or 140000)
    max_incs = int(os.environ.get("EPOCH_MAX_INCREMENTS", "8") or 8)
    wall_sec = int(os.environ.get("EPOCH_WALL_SEC", "5400") or 5400)
    started = time.time()
    handoff = agent_dir / "state" / "handoff.md"

    # Open the FIFO for write — blocks until claude opens the read end (rendezvous), then deliver the task.
    fh = open(a.fifo, "w")
    fh.write(umsg(prompt) + "\n")
    fh.flush()

    sent = {"w1": False, "w2": False, "stop": False, "turnwrap": False}
    stop_ts = None
    finalize_ts = None                                     # EPOCHEND injected; next result closes
    increments = 0
    max_turns = int(os.environ.get("MAX_TURNS", "0") or 0)   # 0/unset = no turn-cap wrap-up

    def kill_now():
        try:
            cutoff.write_text(str(int(time.time())))
        except Exception:
            pass
        # Scoped kill (2026-07-04 fix #9): this agent's claude carries --add-dir <agent_dir> on its
        # cmdline — match THAT, not every 'claude -p' on the machine (which, host-run multi-agent,
        # killed every other agent's in-flight tick too).
        import re as _re
        pat = f"claude .*{_re.escape(str(agent_dir))}"
        subprocess.run(["pkill", "-TERM", "-f", pat], check=False)
        time.sleep(2)
        subprocess.run(["pkill", "-KILL", "-f", pat], check=False)
        try:
            fh.close()
        except Exception:
            pass

    def close_epoch():
        try:
            fh.close()
        except Exception:
            pass

    while True:
        time.sleep(POLL)

        # ── result boundary: an increment finished — continue the epoch, finalize, or close ──
        if sentinel.exists():
            try:
                sentinel.unlink()
            except Exception:
                pass
            increments += 1
            if not epochs_on or finalize_ts is not None:
                close_epoch()              # legacy mode, or the post-EPOCHEND wrap cycle just landed
                return
            b = read_json(bud, {}) or {}
            cost = float(b.get("cost_est", 0) or 0)
            ctx = int(b.get("ctx_tokens", 0) or 0)
            plan = read_json(st / "budget.json", {})
            hard = min(max(float(plan.get("hard_usd") or a.hard_floor), a.hard_floor), a.hard_max)
            soft = min(max(float(plan.get("soft_usd") or a.soft_floor), a.soft_floor), hard)
            status, status_path = _read_status(st)
            try:
                hf = handoff.stat().st_mtime >= started - 1
            except OSError:
                hf = False
            action, reason = epoch_decision(
                subtype_ok=True, ctx_tokens=ctx, cost=cost, soft=soft, epoch_tokens=epoch_tokens,
                increments=increments, max_increments=max_incs, elapsed=time.time() - started,
                wall_sec=wall_sec, status=status, has_work=_has_open_work(agent_dir),
                cap_ok=_cap_ok(), stop_sent=sent.get("stop", False), handoff_fresh=hf)
            sys.stderr.write(f"[feeder] increment #{increments} done → {action} ({reason})\n")
            try:
                if action == "continue":
                    if status_path is not None:            # consumed: each increment re-declares
                        try:
                            status_path.unlink()
                        except OSError:
                            pass
                    fh.write(umsg(CONT.format(inc=increments, ctx=ctx // 1000, cost=cost)) + "\n")
                    fh.flush()
                    continue
                if action == "finalize":
                    fh.write(umsg(EPOCHEND.format(reason=reason)) + "\n")
                    fh.flush()
                    finalize_ts = time.time()
                    continue
            except BrokenPipeError:
                return                                      # claude already exited
            close_epoch()                                   # action == "close"
            return

        # finalize wrap cycle overran its grace → close anyway (the wall timeout is the backstop)
        if finalize_ts and (time.time() - finalize_ts) > max(a.grace * 3, 180):
            close_epoch()
            return

        # ── between boundaries: graduated budget ladder (unchanged) ──
        b = read_json(bud, None)
        if b:
            cost = float(b.get("cost_est", 0) or 0)
            turn = int(b.get("turn", 0) or 0)
            plan = read_json(st / "budget.json", {})
            # The budget is a runaway CAP, not a target — clamp the agent's plan UP to the floor (so a
            # too-tight self-budget can't thrash: a warm-resume tick spends ~$1+ on turn-1 cache rewarm
            # before any work) and DOWN to the absolute max (so it can't blow past the runaway ceiling).
            hard = min(max(float(plan.get("hard_usd") or a.hard_floor), a.hard_floor), a.hard_max)
            soft = min(max(float(plan.get("soft_usd") or a.soft_floor), a.soft_floor), hard)
            which = next_injection(cost, turn, soft, hard, max_turns, sent)
            try:
                if which == "stop":
                    fh.write(umsg(STOP.format(cost=cost, hard=hard, grace=int(a.grace))) + "\n"); fh.flush()
                    sent["stop"] = True
                    stop_ts = time.time()
                elif which == "turnwrap":
                    fh.write(umsg(TURNWRAP.format(turn=turn, max_turns=max_turns)) + "\n"); fh.flush()
                    sent["turnwrap"] = True
                elif which == "w2":
                    fh.write(umsg(WARN2.format(cost=cost, hard=hard)) + "\n"); fh.flush()
                    sent["w2"] = True
                elif which == "w1":
                    fh.write(umsg(WARN1.format(cost=cost, hard=hard, soft=soft)) + "\n"); fh.flush()
                    sent["w1"] = True
            except BrokenPipeError:
                return  # claude already exited

        # hard backstop: grace elapsed after STOP and the agent still hasn't yielded a result
        if stop_ts and (time.time() - stop_ts) > a.grace:
            if sentinel.exists():
                close_epoch()
                return
            kill_now()
            return


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as e:
        sys.stderr.write(f"[feeder] {e}\n")
        sys.exit(1)
