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

Fails OPEN (any error → exit 0, never wedge the tick). Fast + deterministic (no model call).
  (configured automatically as a Stop hook in /agent/.claude/settings.json — not run by hand)
"""
import sys, os, json, datetime, pathlib

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
    sys.exit(0)


if __name__ == "__main__":
    main()
