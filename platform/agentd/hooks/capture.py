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
Convention is not capture: scribepod had no decisions.jsonl at all. This hook has the transcript
at tick end, so the record no longer depends on the agent remembering to write it.

Fails OPEN (any error → exit 0, never wedge the tick). Fast + deterministic (no model call).
  (configured automatically as a Stop hook in /agent/.claude/settings.json — not run by hand)
"""
import sys, os, json, re, datetime, pathlib

# Everything this hook writes lands in git-committed, qmd-recalled memory — run it ALL through the
# framework's one redactor (same definition the secret_scan gate + vault scan use). Without this,
# a token inlined in a Bash command is transcribed verbatim into memory/activity/ (channel-lab
# leaked TELEGRAPH_TOKEN exactly this way, 2026-07-28) and the vault gate then blocks all backups.
# Load the framework's secrets.py BY FILE PATH — its module name (`secrets`) collides with Python's
# stdlib, and a plain `import secrets` on a bad/missing path silently binds the stdlib module (no
# .redact), which then crashes at first use. Explicit file-path load avoids the name collision entirely.
# It sits one dir up from this hook (platform/agentd/secrets.py); env/legacy paths are fallbacks.
_sec = None
for _dir in (str(pathlib.Path(__file__).resolve().parent.parent),
             os.environ.get("ENCLAVE_AGENTD"), "/workspace/platform/agentd"):
    if not _dir:
        continue
    _f = pathlib.Path(_dir) / "secrets.py"
    if not _f.exists():
        continue
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("enclave_secrets", str(_f))
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        if hasattr(_mod, "redact"):
            _sec = _mod
            break
    except Exception:                                      # pragma: no cover
        pass
if _sec is None:
    sys.stderr.write("[capture] DEGRADED: shared secrets module unreachable; "
                     "activity summaries are NOT being redacted\n")


def _redact(text):
    return _sec.redact(text) if (_sec and text) else text

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


def extract(transcript_path, all_text=False):
    """Pull (meaningful tool actions, final assistant text) from a Claude Code session JSONL.

    all_text=True returns EVERY assistant text block joined instead of just the last one. Decision
    capture needs that: an agent commonly writes its decision block and THEN makes a final tool call
    with a one-line sign-off, and keeping only the last block silently discarded the decision (every
    record came back "implicit" while the agents were in fact being asked to write one).
    """
    actions, final, chunks = [], "", []
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
                        actions.append(_redact(summarize_tool(nm, b.get("input"))))
                elif b.get("type") == "text" and b.get("text", "").strip():
                    final = _redact(b["text"].strip())
                    chunks.append(final)
    except OSError:
        pass
    return actions, ("\n".join(chunks) if all_text else final)


# An agent that writes "DECISION: x / WHY: y" gets that captured verbatim; one that writes nothing
# structured still gets a record built from its own conclusion. Both beat an empty log.
# The agent decides the formatting, so the parser has to accept the shapes a model actually emits.
# Measured against the live variants on 2026-08-04: a markdown HEADING ("## DECISION: x") was missed
# entirely and fell through to an implicit record, and "**DECISION:** x" captured a literal "**"
# into the stored value. `_LEAD` covers list bullets and headings; `_strip_md` cleans the capture.
_LEAD = r"\s*(?:[-*]\s*|\#{1,6}\s*)?"
_DEC = re.compile(r"^" + _LEAD + r"(?:\*\*)?(DECISION|DECIDED|CHOSE|CHOICE)(?:\*\*)?\s*[:\-]\s*(.+)$", re.I)
_WHY = re.compile(r"^" + _LEAD + r"(?:\*\*)?(WHY|BECAUSE|RATIONALE|REASON)(?:\*\*)?\s*[:\-]\s*(.+)$", re.I)
_EVID = re.compile(r"^" + _LEAD + r"(?:\*\*)?(EVIDENCE|BASIS)(?:\*\*)?\s*[:\-]\s*(.+)$", re.I)
_CONF = re.compile(r"^" + _LEAD + r"(?:\*\*)?CONFIDENCE(?:\*\*)?\s*[:\-]\s*(high|medium|low)\b", re.I)


def _strip_md(value):
    """Drop markdown emphasis left over from `**DECISION:** x` style headers."""
    return value.strip().lstrip("*").strip().rstrip("*").strip()


#: A logged decision often begins "<ISO ts> — ...". When such a line is read back as the source of
#: the NEXT implicit decision, the prefixes chain and each entry embeds its predecessor.
_TS_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?\s*[—-]\s*")


def _headline(rollup, final):
    """The best one-line statement of what THIS tick concluded.

    `final` is read BEFORE `rollup`, and the order is the whole point. The rollup's newest line is
    the PREVIOUS tick's logged conclusion, so preferring it made every implicit decision a copy of
    the one before, stamped with the current time — measured on financial-advisor 2026-08-04, 13 of
    the last 20 entries were the prior tick's text and the chain accreted until the 500-char cap.
    A log that attributes old decisions to new ticks is worse than a sparse one: it reads as
    activity and it corrupts the unevidenced-rate signal decisions_report.py exists to produce.

    A rollup can also be a placeholder ("(no ticks yet)", "—") that says nothing, so it stays a
    fallback rather than being dropped entirely.
    """
    # `final` is EVERY assistant text block of the tick joined (see extract(all_text=True)), so its
    # first line is the agent's OPENING sentence and its last is its conclusion. Scanning forwards
    # recorded openings as decisions — live examples on 2026-08-04 were "I'll start by checking the
    # preflight capabilities..." and "Now writing a skill to...", i.e. the plan and the middle of
    # the work, logged as what the tick decided. The rollup is a curated one-liner, so it is read
    # forwards.
    for cand, from_end in (((final or "").strip(), True), ((rollup or "").strip(), False)):
        lines = cand.splitlines()
        for line in (reversed(lines) if from_end else lines):
            line = line.strip().lstrip("#-* ").strip()
            if len(line) < 8:
                continue
            if line.startswith("(") and line.endswith(")"):      # "(no ticks yet)"
                continue
            # Strip any chain of leading timestamps, so a headline that did come from a rollup
            # cannot re-accrete. Bounded loop: a malformed line must not spin here.
            for _ in range(5):
                stripped = _TS_PREFIX.sub("", line)
                if stripped == line:
                    break
                line = stripped.strip()
            if len(line) < 8:
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
            cur = {"decision": _strip_md(m.group(2))[:500], "why": "", "evidence": "", "confidence": ""}
            continue
        if cur is None:
            continue
        for rx, key in ((_WHY, "why"), (_EVID, "evidence")):
            m = rx.match(line)
            if m:
                cur[key] = _strip_md(m.group(2))[:800]
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
    every_text = extract(data.get("transcript_path", ""), all_text=True)[1]
    rollup = ""
    try:
        body = [l for l in (d / "state" / "rollup.md").read_text().splitlines()
                if l.strip() and not l.strip().startswith("#")]
        rollup = _redact(body[0]) if body else ""
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
        recs = extract_decisions(every_text or final, actions, rollup, ts,
                                 os.environ.get("AGENT_ID", d.name))
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
