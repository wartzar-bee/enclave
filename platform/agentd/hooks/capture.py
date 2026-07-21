#!/usr/bin/env python3
"""
capture.py — Stop hook: AUTO-CAPTURE each tick into the agent's committed markdown memory (P3.5).

This is claude-mem's auto-capture pattern, rebuilt on OUR assets instead of its stack: no Chroma,
no Bun worker, no SQLite, no LLM-compression call. At tick END (the `Stop` lifecycle hook, which
fires even under `claude -p --dangerously-skip-permissions`), it deterministically parses the
tick's session transcript, extracts the meaningful actions + the agent's own rollup line, and
appends a compact dated entry to `/agent/memory/activity/<date>.md`. That file is git-durable
(memory/ is committed) and qmd-indexed (the agent recalls it semantically next tick) — so the
record of what the agent DID never depends on it remembering to write it.

Division of labour: this hook captures the FACTUAL record automatically; the brief still asks the
agent to distil genuine LESSONS (judgement) into memory/. memory.py recall SKIPS memory/activity/
so these logs stay qmd-searchable + auditable without crowding the lesson digest.

It ALSO writes state/decisions.jsonl (2026-07-21). Decision capture used to be part of the `finish`
contract in local_agent.py, which only runs on BRAIN=api/local — so when the whole fleet moved to
BRAIN=claude on the subscription, every pod silently stopped recording WHY it did anything, and
effective_config still reported the claude path as "convention only (no structural capture yet)".
Convention is not capture: logan-cross had no decisions.jsonl at all. This hook has the transcript
at tick end, so the record no longer depends on the agent remembering to write it.

Fails OPEN (any error → exit 0, never wedge the tick). Fast + deterministic (no model call).
  (configured automatically as a Stop hook in /agent/.claude/settings.json — not run by hand)
"""
import sys, os, json, re, datetime, pathlib

# Tools worth recording (the real actions); read-only noise (Read/Grep/Glob/LS) is skipped.
MEANINGFUL = ("Bash", "Write", "Edit", "NotebookEdit")


def _agent_dir(data):
    return pathlib.Path(os.environ.get("AGENT_DIR") or data.get("cwd") or "/agent")


def summarize_tool(name, inp):
    inp = inp or {}
    if name == "Bash":
        return "Bash: " + (inp.get("command") or "").strip().replace("\n", " ")[:140]
    for k in ("file_path", "path", "notebook_path"):
        if inp.get(k):
            return f"{name}: {inp[k]}"
    if name.startswith("mcp__"):
        return f"{name}: {json.dumps(inp)[:90]}"
    return name


def extract(transcript_path):
    """Pull (meaningful tool actions, final assistant text) from a Claude Code session JSONL."""
    actions, final = [], ""
    try:
        for line in pathlib.Path(transcript_path).read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") or rec
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    nm = b.get("name", "")
                    if nm in MEANINGFUL or nm.startswith("mcp__"):
                        actions.append(summarize_tool(nm, b.get("input")))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    final = b["text"].strip()
    except OSError:
        pass
    return actions, final


# An agent that writes "DECISION: x / WHY: y" gets that captured verbatim; one that writes nothing
# structured still gets a record built from its own conclusion. Both beat an empty log.
_DEC = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(DECISION|DECIDED|CHOSE|CHOICE)(?:\*\*)?\s*[:\-]\s*(.+)$", re.I)
_WHY = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(WHY|BECAUSE|RATIONALE|REASON)(?:\*\*)?\s*[:\-]\s*(.+)$", re.I)
_EVID = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?(EVIDENCE|BASIS)(?:\*\*)?\s*[:\-]\s*(.+)$", re.I)
_CONF = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?CONFIDENCE(?:\*\*)?\s*[:\-]\s*(high|medium|low)\b", re.I)


def _headline(rollup, final):
    """The best one-line statement of what this tick concluded.

    A rollup can be a placeholder ("(no ticks yet)", "—") that says nothing; recording it as a
    decision is worse than useless, because it inflates the log and hides the pods that genuinely
    are not reasoning. Fall through to the agent's own closing text instead.
    """
    for cand in ((rollup or "").strip(), (final or "").strip()):
        for line in cand.splitlines():
            line = line.strip().lstrip("#-* ").strip()
            if len(line) < 8:
                continue
            if line.startswith("(") and line.endswith(")"):      # "(no ticks yet)"
                continue
            return line[:300]
    return ""


def extract_decisions(final, actions, rollup, ts, agent):
    """PURE — build decision records for one tick (unit-tested).

    Explicit `DECISION:`/`WHY:` lines are captured as written. If the agent wrote none, we still emit
    ONE record from its own tick conclusion, marked `implicit` and with evidence derived from what it
    actually ran — an honest 'it did this, and stated no reason' beats a silent gap, and the
    unevidenced rate in decisions_report.py is then a real signal instead of an artefact of the log.
    """
    recs, cur = [], None
    # Agents write the four fields on one line as often as on four ("DECISION: x / WHY: y / ...").
    # Without this split the whole line lands in `decision` and why/evidence read as empty — a log
    # that looks populated while carrying no reasoning, which is worse than an obvious gap.
    text = re.sub(r"\s+/\s+(?=(?:WHY|BECAUSE|RATIONALE|REASON|EVIDENCE|BASIS|CONFIDENCE)\s*[:\-])",
                  "\n", final or "", flags=re.I)
    for line in text.splitlines():
        m = _DEC.match(line)
        if m:
            if cur:
                recs.append(cur)
            cur = {"decision": m.group(2).strip()[:500], "why": "", "evidence": "", "confidence": ""}
            continue
        if cur is None:
            continue
        for rx, key in ((_WHY, "why"), (_EVID, "evidence")):
            m = rx.match(line)
            if m:
                cur[key] = m.group(2).strip()[:800]
        m = _CONF.match(line)
        if m:
            cur["confidence"] = m.group(1).lower()
    if cur:
        recs.append(cur)

    ev_auto = ""
    if actions:
        ev_auto = f"{len(actions)} tool action(s): " + "; ".join(actions[:5])
    if not recs:
        headline = _headline(rollup, final)
        if not headline:
            return []
        recs = [{"decision": headline[:500], "why": "", "evidence": "", "confidence": "", "implicit": True}]
    for r in recs:
        r.setdefault("implicit", False)
        r["evidence"] = r["evidence"] or ev_auto
        r["confidence"] = r["confidence"] or "unstated"
        r["ts"], r["agent"], r["_by"], r["_actions"] = ts, agent, "capture-hook", len(actions)
    return recs


def summarize_tick(actions, final, rollup_line, ts):
    """PURE — render one dated tick entry (unit-tested)."""
    lines = [f"### tick {ts}"]
    if rollup_line:
        lines.append(f"- rollup: {rollup_line[:300]}")
    if actions:
        lines.append(f"- actions ({len(actions)}): " + "; ".join(actions[:20]))
    if final:
        lines.append(f"- result: {final.splitlines()[0][:300]}")
    return "\n".join(lines) + "\n\n"


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    d = _agent_dir(data)
    actions, final = extract(data.get("transcript_path", ""))
    rollup = ""
    try:
        body = [l for l in (d / "state" / "rollup.md").read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")]
        rollup = body[0] if body else ""
    except OSError:
        pass
    if not actions and not rollup and not final:
        sys.exit(0)                                          # nothing to capture
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = d / "memory" / "activity" / (now.strftime("%Y-%m-%d") + ".md")
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if not out.exists():
            out.write_text(f"# Activity — {now.strftime('%Y-%m-%d')} (auto-captured each tick)\n\n")
        with out.open("a") as f:
            f.write(summarize_tick(actions, final, rollup, ts))
    except OSError:
        pass
    # state/ is vault-gitignored, so a credential quoted in an agent's reasoning stays local; the
    # RENDER step (decisions_report.py) is where redaction belongs, and is where it now happens.
    try:
        recs = extract_decisions(final, actions, rollup, ts, os.environ.get("AGENT_ID", d.name))
        if recs:
            sd = d / "state"
            sd.mkdir(parents=True, exist_ok=True)
            with (sd / "decisions.jsonl").open("a") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
    except Exception:
        pass                                                 # never wedge a tick over a log line
    sys.exit(0)


if __name__ == "__main__":
    main()
