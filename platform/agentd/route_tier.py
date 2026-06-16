#!/usr/bin/env python3
"""
route_tier.py — pick the MODEL TIER for one tick (P2: cap-discipline for a persistent fleet).

A persistent agent that reaches for the top model (opus) on EVERY 3h tick burns the
subscription cap fast — and at fleet scale that's the binding constraint (D-071). This
router downgrades the ticks that don't need judgment — routine maintenance heartbeats and
purely mechanical directives (post / measure / narrate / schedule) — to a cheaper model
(sonnet), while RESERVING the top model for judgment (decide / adjudicate / strategy /
review / creative). It is SAFE-BY-DEFAULT: anything uncertain, or any error upstream, falls
back to the top model — the router only downgrades when it's confident the work is cheap.

Inspired by ruflo's router (bypass the expensive model for simple work) but kept to a
zero-cost, deterministic heuristic (no extra LLM call in the hot path); an LLM/learned
classifier is a later upgrade. Forcing: `agentctl msg --tier top|cheap` injects a
`[tier:...]` tag the agent's drained directive carries, which overrides the heuristic.

  python3 route_tier.py <agent-dir> --reason <startup|heartbeat|inbox|comms> \
                        --model <top> --routine <cheap> [--forced top|cheap]
prints the chosen model to stdout; a one-line rationale to stderr (logged by runtime.sh).
"""
import sys, argparse, pathlib

# Markers are matched against the lowercased text of the tick's PENDING directives.
JUDGMENT = ("decide", "choose", "adjudicat", "strateg", "evaluate", "assess", "review",
            "critique", "prioriti", "which ", "should we", "judge", "analy", "design ",
            "plan the", "rewrite", "revise", "draft chapter", "write chapter", "canon", "verdict")
MECHANICAL = ("post", "upload", "schedule", "measure", "snapshot", "append", "format",
              "reblog", "repost", "publish", "collect", "fetch", "narrate", "render",
              "reel", "commit", "ack", "ping")


def pending_directives(inbox):
    """Unchecked '- [ ]' items in inbox.md — the directives this tick still has to act on."""
    try:
        lines = inbox.read_text().splitlines()
    except OSError:
        return []
    return [ln.strip()[5:].strip() for ln in lines if ln.strip().startswith("- [ ]")]


def choose_tier(reason, pending, model, routine, forced=None):
    """PURE decision (unit-tested). Returns (model_name, rationale). Bias: downgrade only when
    confidently cheap; everything ambiguous resolves to the top `model`."""
    if forced == "top":
        return model, "forced:top"
    if forced in ("cheap", "routine"):
        return routine, "forced:cheap"
    blob = " ".join(pending).lower()
    if "[tier:top]" in blob:
        return model, "tag:top"
    if "[tier:cheap]" in blob:
        return routine, "tag:cheap"
    if pending:
        if any(k in blob for k in JUDGMENT):
            return model, "directive:judgment→top"
        if any(k in blob for k in MECHANICAL):
            return routine, "directive:mechanical→routine"
        return model, "directive:uncertain→top"      # safe default: don't under-power
    if reason == "heartbeat":
        return routine, "routine-heartbeat→routine"   # idle maintenance pass — the frequent cheap win
    return model, f"{reason}→top"                      # startup / a directive trigger with nothing parsed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir")
    ap.add_argument("--reason", default="heartbeat")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--routine", default="sonnet")
    ap.add_argument("--forced", default=None)
    a = ap.parse_args()
    pend = pending_directives(pathlib.Path(a.agent_dir) / "inbox.md")
    m, why = choose_tier(a.reason, pend, a.model, a.routine, a.forced)
    sys.stderr.write(f"route_tier: {m} ({why}; {len(pend)} pending, reason={a.reason})\n")
    print(m)


if __name__ == "__main__":
    main()
