#!/usr/bin/env python3
"""publish_audit.py — enforce the ".publish-audit-allow" contract.

WHY THIS EXISTS: `.publish-audit-allow` shipped with no enforcer. Grep across the whole tree found
exactly one reference to it — itself. It was an allowlist for a scanner that did not exist, and an
external security reviewer read it as evidence of a working control and reasoned from it. A control
that only LOOKS like a control is worse than none: it buys confidence exactly where someone checks.

WHAT IT CHECKS: enclave is public; the studio that develops it is not. Studio-SPECIFICS (a pod name,
an operator's checkout path, a sibling venture's internals) must never land here. The allow file
lists the terms that are this product's own DOMAIN VOCABULARY and must not be flagged.

Usage:  python3 tools/publish_audit.py [--json]      exit 0 clean · 1 violations · 2 bad config
"""
import json, pathlib, re, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ALLOW = ROOT / ".publish-audit-allow"

# GENERIC leak classes only. The private terms deliberately do NOT live here: this file is PUBLIC,
# so a hardcoded list of private pod names, fleet roots and company names would publish exactly what
# it is meant to protect — the scanner would be the leak. (It was, on first write. Caught by running
# the scanner on itself.)
#
# Private terms come from `.publish-audit-deny` (gitignored, operator-supplied): one regex per line,
# `#` comments. Absent = generic checks still run and the scanner says the private list is off, so a
# fresh clone is honest about what it did and did not check rather than silently passing.
PATTERNS = {
    # `/Users/you|me|user/` and `/Users/<...>` are documentation placeholders, not a real checkout.
    "operator-path": r"/Users/(?!you/|me/|user/|<)[a-z0-9._-]+/|/home/(?!agent/|user/)[a-z0-9._-]+/Dev/",
}
DENY_FILE = ROOT / ".publish-audit-deny"


def load_deny():
    """Operator's private terms. Returns (patterns, present)."""
    if not DENY_FILE.exists():
        return {}, False
    out = {}
    for i, line in enumerate(DENY_FILE.read_text(errors="replace").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            re.compile(line)
        except re.error:
            continue
        out[f"private:{i}"] = line
    return out, True


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "dist", "build"}
# This scanner's OWN test must contain the strings it detects in order to test that it detects them.
# Narrow, explicit, and by exact path — NOT a wildcard for "tests", because a real leak in a test file
# is still a published leak. (The same self-reference bit .gitleaksignore: documenting a false positive
# by quoting it made the documentation the next finding.)
SKIP_EXACT = {"platform/agentd/test_publish_audit.py"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".pdf", ".zip", ".woff", ".woff2"}


def load_allow():
    if not ALLOW.exists():
        return None
    pats = []
    for line in ALLOW.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            pats.append(re.compile(line, re.I))
        except re.error as e:
            print(f"publish-audit: bad regex in .publish-audit-allow: {line!r} ({e})", file=sys.stderr)
            return None
    return pats


def tracked_files():
    """Only git-tracked files — an untracked local scratch file is not published."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                         capture_output=True, text=True, check=True).stdout
    for rel in out.split("\0"):
        if not rel:
            continue
        p = ROOT / rel
        if (p.suffix.lower() in SKIP_SUFFIX or rel in SKIP_EXACT
                or set(pathlib.PurePath(rel).parts) & SKIP_DIRS):
            continue
        yield rel, p


def audit():
    allow = load_allow()
    if allow is None:
        return None, [], False
    deny, deny_present = load_deny()
    rx = {k: re.compile(v, re.I) for k, v in {**PATTERNS, **deny}.items()}
    violations = []
    for rel, p in tracked_files():
        try:
            text = p.read_text(errors="replace")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            # The allowlist is evaluated on the LINE: a line that is domain vocabulary is exempt even
            # if a pattern also matches it. That is the documented contract of the allow file.
            if any(a.search(line) for a in allow):
                continue
            for kind, r in rx.items():
                m = r.search(line)
                if m:
                    violations.append({"file": rel, "line": i, "kind": kind,
                                       "match": m.group(0)[:60], "text": line.strip()[:120]})
                    break
    return allow, violations, deny_present


def main():
    allow, violations, deny_present = audit()
    if allow is None:
        print("publish-audit: .publish-audit-allow missing or unparseable — FAILING CLOSED", file=sys.stderr)
        return 2
    if "--json" in sys.argv:
        print(json.dumps(violations, indent=1))
    elif violations:
        print(f"publish-audit: {len(violations)} studio-specific leak(s) in the public tree:")
        for v in violations[:50]:
            print(f"  {v['file']}:{v['line']}  [{v['kind']}] {v['match']}")
            print(f"      {v['text']}")
        if len(violations) > 50:
            print(f"  … and {len(violations) - 50} more")
        print("\nFix them — do NOT add them to .publish-audit-allow. That file is for this product's "
              "own vocabulary, not for excusing a real leak (see its header).")
    else:
        note = "" if deny_present else "  [no .publish-audit-deny — private-term check OFF]"
        print(f"publish-audit: clean ({len(allow)} allow-rules applied){note}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
