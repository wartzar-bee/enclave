#!/usr/bin/env python3
"""tick_feeder.py — stream-json stdin feeder + graduated budget INJECTOR + hard cutoff.

Runs as the stdin writer for `claude -p --input-format stream-json`. It:
  1. Delivers the tick prompt as the first user turn.
  2. Watches live spend (state/.ctx-budget.json, written per assistant turn by usage_capture.py) and
     INJECTS graduated user messages as the agent approaches its $ budget. An injected USER message is
     a first-class operator turn the model OBEYS (empirically proven — unlike an ignorable stderr hook):
       • warn1  (cost ≥ soft)            → "wrap up the current sub-task, refresh handoff, no new sub-task"
       • warn2  (cost ≥ soft+0.6·(hard-soft)) → "finalize handoff NOW — you're about to be cut off"
       • STOP   (cost ≥ hard)            → "write handoff + tick-status{session:clear} + finish NOW"
  3. Hard backstop: if the agent hasn't finished within GRACE after STOP, touches state/.cost-cutoff and
     kills claude (the cap holds even if the agent ignores the message).
  4. Closes stdin (EOF → claude exits cleanly) when usage_capture signals the tick result
     (state/.tick-result).

Budget plan: state/budget.json {package, soft_usd, hard_usd} (agent-authored); clamped to the env floors
(--soft-floor / --hard-floor). Fail-safe: the prompt is always delivered; the kill backstop guarantees
the cap regardless of what the agent does.
"""
import os, sys, json, time, argparse, subprocess, pathlib

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
         "(1) finish writing state/handoff.md; (2) write state/tick-status.json "
         "{{\"status\":\"continue\",\"session\":\"clear\"}}; (3) finish this turn. You will be "
         "hard-stopped in ~{grace}s.")
# Turn-cap wrap-up (2026-07-04 fix #4/#9-adjacent): MAX_TURNS used to GUILLOTINE mid-work
# (57 ticks / $111 on forgepod died error_max_turns, the truncated work re-derived next tick).
# Inject a wrap-up order near the cap so the agent banks state and exits cleanly instead.
TURNWRAP = ("⏳ TURN CAP — you are at turn {turn} of a {max_turns}-turn tick cap. WRAP UP NOW: "
            "bank the current chunk (commit if applicable), write state/handoff.md with the EXACT "
            "next step, write state/tick-status.json {{\"status\":\"continue\"}}, and finish this "
            "turn cleanly. Do NOT start anything new — hitting the cap wipes this tick's unsaved "
            "work and the next tick pays to re-derive it.")


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


def read_json(p, d):
    try:
        return json.loads(pathlib.Path(p).read_text())
    except Exception:
        return d


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

    # Open the FIFO for write — blocks until claude opens the read end (rendezvous), then deliver the task.
    fh = open(a.fifo, "w")
    fh.write(umsg(prompt) + "\n")
    fh.flush()

    sent = {"w1": False, "w2": False, "stop": False, "turnwrap": False}
    stop_ts = None
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
        agent_dir = str(st.resolve().parent)
        pat = f"claude .*{_re.escape(agent_dir)}"
        subprocess.run(["pkill", "-TERM", "-f", pat], check=False)
        time.sleep(2)
        subprocess.run(["pkill", "-KILL", "-f", pat], check=False)
        try:
            fh.close()
        except Exception:
            pass

    while True:
        time.sleep(POLL)
        # tick produced a result → close stdin so claude exits cleanly
        if sentinel.exists():
            try:
                fh.close()
            except Exception:
                pass
            return

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
                try:
                    fh.close()
                except Exception:
                    pass
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
