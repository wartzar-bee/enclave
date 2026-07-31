#!/usr/bin/env python3
"""tokencount-tool — reference enclave plugin (type: tool).

Estimates the token cost of a text file with the ~4-chars-per-token heuristic, so an agent can
budget-check a prompt before spending it. Pure stdlib: reads only the path handed to it, opens no
network connection, touches no secret store, runs no subprocess — so it matches its plugin.yaml
security declarations exactly and the validator passes it clean.
"""
from __future__ import annotations

import sys

CHARS_PER_TOKEN = 4  # coarse heuristic; good enough for a pre-spend budget check


def estimate(text: str) -> dict:
    chars = len(text)
    return {
        "chars": chars,
        "words": len(text.split()),
        "est_tokens": (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: tool.py <text-file>", file=sys.stderr)
        return 2
    with open(argv[0], encoding="utf-8", errors="replace") as fh:
        report = estimate(fh.read())
    import json
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
