#!/usr/bin/env python3
"""
test_capture.py — the Stop hook's decision capture.

Decision logging used to live in local_agent.py's `finish` contract, which only runs on
BRAIN=api/local. When the fleet moved to BRAIN=claude on the subscription, every pod silently stopped
recording WHY it acted — scribepod had no decisions.jsonl at all, and the others' last entries were
hours stale. These tests pin the replacement: structural capture that does not depend on the agent
remembering, and that stays honest about which records are the agent's own words.
"""
import json, os, pathlib, sys, tempfile, shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture as C

fails = []


def ck(name, cond):
    if not cond:
        fails.append(name)


TS, AGENT = "2026-07-21T13:00:00Z", "scribepod"

# ── explicit markers are captured as the agent wrote them ────────────────────────────────────
final = """Looked at the RR swap engine.
DECISION: ship the proxy-verified swap to production
WHY: the flat-KPI run proves the old path never fetched a live page
EVIDENCE: 40 proxy fetches, 0 blocks, KPI moved 0.04 -> 0.11
CONFIDENCE: medium
Next tick I will re-measure."""
r = C.extract_decisions(final, ["Bash: curl rr"], "rollup line", TS, AGENT)
ck("explicit: one record", len(r) == 1)
ck("explicit: decision", r[0]["decision"] == "ship the proxy-verified swap to production")
ck("explicit: why", r[0]["why"].startswith("the flat-KPI run proves"))
ck("explicit: evidence", "40 proxy fetches" in r[0]["evidence"])
ck("explicit: confidence", r[0]["confidence"] == "medium")
ck("explicit: not implicit", r[0]["implicit"] is False)
ck("explicit: provenance", r[0]["_by"] == "capture-hook" and r[0]["agent"] == AGENT and r[0]["ts"] == TS)

# ── several decisions in one tick, markdown-styled, all captured ─────────────────────────────
r = C.extract_decisions("- **Decided:** drop channel A\n  **Why:** zero reach after 6 posts\n"
                        "* DECISION - keep channel B\n  WHY - it converts",
                        [], "", TS, AGENT)
ck("multi: two records", len(r) == 2)
ck("multi: second decision", r[1]["decision"] == "keep channel B")
ck("multi: second why", r[1]["why"] == "it converts")

# ── no markers: still a record, honestly marked implicit, evidence from what it RAN ──────────
r = C.extract_decisions("I refreshed the token and re-ran the check.", ["Bash: curl x", "Write: a.md"],
                        "refreshed the Bluesky token", TS, AGENT)
ck("implicit: one record", len(r) == 1)
ck("implicit: uses rollup", r[0]["decision"] == "refreshed the Bluesky token")
ck("implicit: flagged", r[0]["implicit"] is True)
ck("implicit: evidence from actions", r[0]["evidence"].startswith("2 tool action(s):"))
ck("implicit: confidence unstated", r[0]["confidence"] == "unstated")

# ── the four fields on ONE line (agents write it this way as often as not) ────────────────────
r = C.extract_decisions(
    'DECISION: skip Reddit this tick / WHY: no warmed account / EVIDENCE: 403 on submit / CONFIDENCE: high',
    ["Bash: curl reddit"], "", TS, AGENT)
ck("inline: one record", len(r) == 1)
ck("inline: decision only", r[0]["decision"] == "skip Reddit this tick")
ck("inline: why", r[0]["why"] == "no warmed account")
ck("inline: evidence", r[0]["evidence"] == "403 on submit")
ck("inline: confidence", r[0]["confidence"] == "high")
ck("inline: not implicit", r[0]["implicit"] is False)
# a slash inside prose must NOT split (only a slash before a known field name does)
r = C.extract_decisions("DECISION: use the a/b split for the CTA", [], "", TS, AGENT)
ck("inline: prose slash kept", r[0]["decision"] == "use the a/b split for the CTA")

# ── a placeholder rollup is NOT a decision; fall through to the agent's own words ─────────────
r = C.extract_decisions("Checked npm and found the version already published.", ["Bash: curl npm"],
                        "(no ticks yet)", TS, AGENT)
ck("placeholder rollup skipped", r[0]["decision"].startswith("Checked npm"))
r = C.extract_decisions("", ["Bash: x"], "(no ticks yet)", TS, AGENT)
ck("placeholder + no text -> no record", r == [])
ck("headline is capped", len(C.extract_decisions("", [], "x" * 900, TS, AGENT)[0]["decision"]) <= 300)

# ── a tick with nothing to say logs nothing (no synthetic noise) ─────────────────────────────
ck("empty tick -> no record", C.extract_decisions("", [], "", TS, AGENT) == [])

# ── explicit decision with no stated reason keeps why empty, so the unevidenced rate is real ──
r = C.extract_decisions("DECISION: paused outreach", ["Bash: ls"], "", TS, AGENT)
ck("no-why stays empty", r[0]["why"] == "")
ck("evidence falls back to actions", r[0]["evidence"].startswith("1 tool action(s):"))

# ── records are JSON-serialisable, one line each (decisions.jsonl is line-delimited) ─────────
r = C.extract_decisions(final, ["Bash: x"], "", TS, AGENT)
line = json.dumps(r[0])
ck("serialisable", "\n" not in line and json.loads(line)["decision"])

# ── a decision block followed by a tool call + sign-off must still be captured ────────────────
tmp2 = pathlib.Path(tempfile.mkdtemp(prefix="captest2-"))
try:
    tr2 = tmp2 / "t.jsonl"
    tr2.write_text("\n".join(json.dumps(x) for x in [
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "DECISION: stand up lobste.rs\nWHY: unmapped\nCONFIDENCE: low"}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "/agent/state/tick-status.json"}}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Now set tick-status to continue."}]}},
    ]))
    last = C.extract(str(tr2))[1]
    every = C.extract(str(tr2), all_text=True)[1]
    ck("last-block-only loses the decision", "DECISION" not in last)
    ck("all_text keeps the decision", "DECISION: stand up lobste.rs" in every)
    r = C.extract_decisions(every, ["Write: x"], "", TS, AGENT)
    ck("captured despite trailing sign-off", r[0]["decision"] == "stand up lobste.rs")
    ck("captured: not implicit", r[0]["implicit"] is False)
    ck("captured: confidence", r[0]["confidence"] == "low")
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

# ── end-to-end: main() appends to state/decisions.jsonl from a real transcript ───────────────
tmp = pathlib.Path(tempfile.mkdtemp(prefix="captest-"))
try:
    home = tmp / "agent"
    (home / "state").mkdir(parents=True)
    tr = tmp / "t.jsonl"
    tr.write_text("\n".join(json.dumps(x) for x in [
        {"message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "curl example.com"}}]}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "DECISION: ship it\nWHY: the probe passed"}]}},
    ]))
    actions, fin = C.extract(str(tr))
    recs = C.extract_decisions(fin, actions, "", TS, "x")
    with (home / "state" / "decisions.jsonl").open("a") as f:
        for rec in recs:
            f.write(json.dumps(rec) + "\n")
    got = [json.loads(l) for l in (home / "state" / "decisions.jsonl").read_text().splitlines()]
    ck("e2e: transcript -> decision", got and got[0]["decision"] == "ship it")
    ck("e2e: why carried", got[0]["why"] == "the probe passed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

if fails:
    print(f"FAIL ({len(fails)}): " + ", ".join(fails))
    sys.exit(1)
print("capture decision-log OK (36/36)")
