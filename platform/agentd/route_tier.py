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
import sys, argparse, json, pathlib

# Markers are matched against the lowercased text of the tick's PENDING directives.
JUDGMENT = ("decide", "choose", "adjudicat", "strateg", "evaluate", "assess", "review",
            "critique", "prioriti", "which ", "should we", "judge", "analy", "design ",
            "plan the", "rewrite", "revise", "draft chapter", "write chapter", "canon", "verdict")
MECHANICAL = ("post", "upload", "schedule", "measure", "snapshot", "append", "format",
              "reblog", "repost", "publish", "collect", "fetch", "narrate", "render",
              "reel", "commit", "ack", "ping")


def pending_directives(inbox):
    """The directives this tick still has to act on.

    PREFERRED SOURCE: the compiled directive state (state/directives.json — see directives.py).
    Its ACTIVE texts carry no stale '[tier:top]' tags, so a long-done inbox item can no longer
    pin every tick (heartbeats included) to the top model — the router's judgment/mechanical
    heuristic decides instead. Falls back to scanning inbox.md '- [ ]' items when no compiled
    state exists.

    An item is treated as DONE (and skipped) when it carries a child completion line — an
    INDENTED '... done:' sub-bullet beneath it. Agents reliably record completion that way
    but don't always flip the parent '- [ ]' checkbox to '- [x]'; a stale done-but-unflipped
    directive (esp. one tagged '[tier:top]') would otherwise pin the tier router to the top
    model on EVERY tick — heartbeats included — forever (a real subscription-cap leak seen on
    stoneforge: 19 'pending' items, all completed, kept every tick on Opus). Hygiene-tolerant
    by design: the router shouldn't depend on perfect checkbox discipline."""
    dj = inbox.parent / "state" / "directives.json"
    if dj.exists():
        try:
            items = json.loads(dj.read_text()).get("directives", [])
            acts = sorted([x for x in items if isinstance(x, dict) and x.get("status") == "active"
                           and str(x.get("text", "")).strip()],
                          key=lambda x: (x.get("priority", 50), x.get("id", "")))
            if acts:
                return [x["text"] for x in acts]
        except Exception:
            pass                                   # unparseable compiled state → inbox fallback
    try:
        lines = inbox.read_text().splitlines()
    except OSError:
        return []
    pending, i, n = [], 0, len(lines)
    while i < n:
        ln = lines[i]
        if ln.strip().startswith("- [ ]"):
            text, done, j = ln.strip()[5:].strip(), False, i + 1
            # Scan this item's child lines (indented continuations + sub-bullets) until the
            # next top-level line. A 'done:' among them marks the directive completed.
            while j < n:
                child = lines[j]
                if not child.strip():
                    j += 1
                    continue
                if child[:1] not in (" ", "\t"):   # next top-level line → end of this item
                    break
                if "done:" in child.lower():
                    done = True
                j += 1
            if not done:
                pending.append(text)
            i = j
        else:
            i += 1
    return pending


def trace_hint(state_dir, routine, k=3):
    """Trace-informed routing v1 (N4, 2026-07-20 — idea from OpenJarvis's trace-driven policies,
    kept deterministic + explainable). Reads the pod's OWN recent outcomes and answers one question:
    is the cheap tier currently FAILING on this pod? If the last `k` consecutive ticks that ran on
    the routine tier ended badly — wandered to max_steps, or logged decisions whose cited evidence
    no tool event witnessed (fabrication tripwire) — the next tick escalates to the top model.
    Evidence source: state/tick-scorecard.jsonl (subtype + decisions_unwitnessed per tick).
    Returns "escalate" or None. Never raises; no signal → None (heuristic decides)."""
    try:
        rows = []
        with (pathlib.Path(state_dir) / "tick-scorecard.jsonl").open(errors="replace") as fh:
            for ln in fh:
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    continue
        rows = rows[-20:]
        streak = 0
        for r in reversed(rows):
            if r.get("reason") == "chat":
                continue
            bad = r.get("subtype") == "max_steps" or (r.get("decisions_unwitnessed") or 0) > 0
            if not bad:
                return None                     # most recent real tick was fine → no escalation
            streak += 1
            if streak >= k:
                return "escalate"
        return None
    except Exception:
        return None


def choose_tier(reason, pending, model, routine, forced=None, state_dir=None):
    """PURE decision (unit-tested). Returns (model_name, rationale). Bias: downgrade only when
    confidently cheap; everything ambiguous resolves to the top `model`. Precedence:
    forced > inbox tag > trace escalation > directive heuristic > reason default."""
    if forced == "top":
        return model, "forced:top"
    if forced in ("cheap", "routine"):
        return routine, "forced:cheap"
    blob = " ".join(pending).lower()
    if "[tier:top]" in blob:
        return model, "tag:top"
    if "[tier:cheap]" in blob:
        return routine, "tag:cheap"
    # Trace check BEFORE the downgrade paths: a cheap tier that keeps wandering or emitting
    # unwitnessed evidence must not keep getting the work just because it looks mechanical.
    if state_dir and trace_hint(state_dir, routine):
        return model, "trace:cheap-tier-failing→top"
    if pending:
        if any(k in blob for k in JUDGMENT):
            return model, "directive:judgment→top"
        if any(k in blob for k in MECHANICAL):
            return routine, "directive:mechanical→routine"
        return model, "directive:uncertain→top"      # safe default: don't under-power
    if reason in ("heartbeat", "continue"):
        # Routine maintenance pass / continuous backlog re-fire (no parsed directive) — the frequent
        # cheap win. A self-driving agent's back-to-back grind MUST stay off the top model; Opus is for
        # judgment directives + escalation only (see docs/CONTEXT-AND-TICKS.md).
        return routine, f"routine-{reason}→routine"
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
    m, why = choose_tier(a.reason, pend, a.model, a.routine, a.forced,
                         state_dir=pathlib.Path(a.agent_dir) / "state")
    sys.stderr.write(f"route_tier: {m} ({why}; {len(pend)} pending, reason={a.reason})\n")
    print(m)


if __name__ == "__main__":
    main()
