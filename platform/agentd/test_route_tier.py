#!/usr/bin/env python3
"""Hermetic tests for route_tier.py — the model-tier picker.

Covers choose_tier()'s decision matrix AND pending_directives()'s done-detection
(the fix for the forgepod cost leak: a done-but-unflipped '- [ ]' directive must
NOT count as pending, else a stale '[tier:top]' tag pins every tick to the top model).
Run: python3 test_route_tier.py
"""
import tempfile, pathlib
import route_tier as rt


def _pending(text):
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "inbox.md"
        p.write_text(text)
        return rt.pending_directives(p)


def test_pending_skips_done_annotated():
    # A '- [ ]' with an indented 'done:' child is COMPLETED → not pending.
    inbox = (
        "- [ ] 2026 — [tier:top] big board directive\n"
        "  - done: 2026 — shipped it, committed abc123\n"
        "- [ ] 2026 — genuinely open task\n"
    )
    pend = _pending(inbox)
    assert len(pend) == 1, pend
    assert "genuinely open" in pend[0]


def test_pending_handles_multiline_body_before_done():
    # A directive whose body wraps onto indented continuation lines, with the done child last.
    inbox = (
        "- [ ] 2026 — [tier:top] mandate\n"
        "  PHASE 1 — research\n"
        "\n"
        "  PHASE 2 — build\n"
        "  - done: 2026 — phases complete\n"
        "- [ ] 2026 — still open\n"
    )
    pend = _pending(inbox)
    assert len(pend) == 1 and "still open" in pend[0], pend


def test_pending_keeps_open_items():
    inbox = "- [ ] open A\n- [ ] open B\n- [x] already checked\n"
    assert len(_pending(inbox)) == 2


def test_done_but_unflipped_tier_top_no_longer_pins_top():
    # The exact regression: stale completed [tier:top] item must not force top.
    inbox = (
        "- [ ] 2026 — [tier:top] BOARD DECISION ...\n"
        "  - done: 2026 — acted on, pushed\n"
    )
    pend = _pending(inbox)
    m, why = rt.choose_tier("heartbeat", pend, "opus", "sonnet")
    assert m == "sonnet", (m, why)


def test_fresh_tier_top_still_pins_top():
    pend = ["[tier:top] new judgment directive just arrived"]
    m, why = rt.choose_tier("inbox", pend, "opus", "sonnet")
    assert m == "opus" and why == "tag:top", (m, why)


def test_routine_heartbeat_is_cheap():
    m, why = rt.choose_tier("heartbeat", [], "opus", "sonnet")
    assert m == "sonnet", (m, why)


def test_forced_overrides():
    assert rt.choose_tier("heartbeat", ["[tier:top] x"], "opus", "sonnet", forced="cheap")[0] == "sonnet"
    assert rt.choose_tier("heartbeat", [], "opus", "sonnet", forced="top")[0] == "opus"


def test_judgment_and_mechanical_keywords():
    assert rt.choose_tier("inbox", ["please review the design"], "opus", "sonnet")[0] == "opus"
    assert rt.choose_tier("inbox", ["post the update"], "opus", "sonnet")[0] == "sonnet"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"ok  {name}")
    print(f"\n{n}/{n} passed")
