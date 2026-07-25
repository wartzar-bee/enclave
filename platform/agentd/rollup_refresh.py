#!/usr/bin/env python3
"""Deterministic rollup headline refresh (runtime-owned, off-agent).

The fleet dashboard's per-agent headline (fleet.py `_headline`) reads the NEWEST *dated* line of
`state/rollup.md`. Writing that file is an agent convention, and agents drift: they stop appending a
rollup line while still ticking, so the dashboard headline goes stale (e.g. "[rollup 4d old]") even
though the pod is live and producing. This derives ONE dated line from the newest
`state/decisions.jsonl` entry — the record the agent writes every tick — and prepends it to
`state/rollup.md`, keeping the headline fresh without depending on the agent remembering to.

Runtime-owned, fully isolated, best-effort: any error is swallowed so it never affects the loop.
Usage: python3 rollup_refresh.py <AGENT_DIR>
"""
import sys
import os
import json

SEED = {"(no ticks yet)", "(no rollup yet)"}
MAX_HISTORY = 40


def _newest_decision(path):
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except Exception:
                    continue
    except Exception:
        return None
    return last


def main():
    agent_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    dec = os.path.join(agent_dir, "state", "decisions.jsonl")
    roll = os.path.join(agent_dir, "state", "rollup.md")
    if not os.path.exists(dec):
        return

    last = _newest_decision(dec)
    if not last:
        return
    ts = (last.get("ts") or "")[:19]
    txt = last.get("decision") or last.get("summary") or last.get("headline") or ""
    txt = " ".join(str(txt).split())[:160]
    if not ts or not txt:
        return
    newline = f"{ts} — {txt}"

    header = None
    body = []
    try:
        with open(roll, encoding="utf-8", errors="replace") as f:
            for l in f.read().splitlines():
                s = l.strip()
                if s.startswith("#"):
                    if header is None:
                        header = l
                    continue
                if not s or s in SEED:
                    continue
                body.append(l)
    except FileNotFoundError:
        pass
    if header is None:
        header = "# status"

    # Newest line on top; drop any prior identical copy; cap the history.
    body = [newline] + [b for b in body if b.strip() != newline]
    body = body[:MAX_HISTORY]

    tmp = roll + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header + "\n\n" + "\n".join(body) + "\n")
    os.replace(tmp, roll)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
