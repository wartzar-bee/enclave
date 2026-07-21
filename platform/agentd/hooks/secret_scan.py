#!/usr/bin/env python3
"""
secret_scan.py — PreToolUse hook: block a write that leaks a credential VALUE into a durable file.

Wired on PreToolUse (Write|Edit|MultiEdit), where exit 2 actually PREVENTS the write; as a
PostToolUse hook it could only complain after the credential was already on disk.

Gap 5: guard.py blocks READS of foreign secrets and egress, but nothing stopped a pod writing a
password/token INTO its own memory/work/rollup (logan-cross leaked RR + Google app-passwords into
memory/activity + work.json; the vault backup silently blocked). Secrets belong only in .secrets/,
referenced by filename. This scans Write/Edit content to memory/**, work/**, state/rollup*,
board-report*, skills/**, and blocks (exit 2) when it looks like a real credential value.

Deterministic, no deps. exit 0 = allow, exit 2 + stderr = block (fires under --dangerously-skip-perms).
"""
import json, re, sys

# durable, git-committed or recall-fed files a leaked secret would poison
_TARGET = re.compile(r"(memory/|/work/|work\.json|rollup|board-report|skills/|handoff|learnings|activity)", re.I)
# credential VALUE shapes (not the word "password:", which is fine as a label without a value)
_SECRET = [
    re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*[^\s\"'\[\]]{6,}"),   # password: <value>
    re.compile(r"(?i)(RR_PASSWORD|API_KEY|SECRET|TOKEN|APP_PASSWORD)\s*[:=]\s*[^\s\"'\[\]]{6,}"),
    re.compile(r"\b(sk-[a-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|sk-or-v1-[a-z0-9]{20,}|xox[bap]-[A-Za-z0-9-]{10,})"),
    re.compile(r"(?i)\b(?:[a-z]{4}\s+){3}[a-z]{4}\b"),                    # google app-password (4x4 words)
]
# never a false-positive on a redaction/placeholder
_SAFE = re.compile(r"REDACTED|\.secrets|<value>|placeholder|example|xxxx|\*\*\*", re.I)


def _payload():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def scan(path, content):
    if not path or not _TARGET.search(path):
        return None
    for line in content.splitlines():
        if _SAFE.search(line):
            continue
        for rx in _SECRET:
            if rx.search(line):
                return f"{path}: line looks like a leaked credential VALUE — secrets live ONLY in " \
                       f".secrets/ (reference by filename). Redact to a placeholder or .secrets ref."
    return None


def main():
    d = _payload()
    tool = d.get("tool_name") or d.get("tool") or ""
    if tool not in ("Write", "Edit", "MultiEdit", "write", "edit"):
        return 0
    inp = d.get("tool_input") or d.get("input") or {}
    path = inp.get("file_path") or inp.get("path") or ""
    content = inp.get("content") or inp.get("new_string") or inp.get("new_str") or ""
    hit = scan(str(path), str(content))
    if hit:
        sys.stderr.write("[secret_scan BLOCKED] " + hit + "\n")
        return 2
    return 0


def _fx(*parts):
    """Assemble a test fixture. Fixtures are NEVER written as literals: this hook file is copied into
    every agent home, which is a scan-gated git vault — a literal credential-shaped fixture makes the
    vault gate block, and that froze wartzar-bee's brain backups entirely (2026-07-21)."""
    return "".join(parts)


def _selftest():
    fails = []
    def ck(n, c):
        if not c: fails.append(n)
    # the real logan leaks → blocked
    ck("rr-password", scan("memory/activity/2026-07-19.md", _fx("RR_PASSWORD=hunter", "2xyz")) is not None)
    ck("password-label-value", scan("work.json", _fx("password: s3cret", "Value1")) is not None)
    ck("google-app-pw", scan("skills/x.md", _fx("app password: abcd ", "efgh ijkl mnop")) is not None)
    ck("openrouter-key", scan("memory/n.md", _fx("key sk-or-", "v1-abcdef0123456789abcdef")) is not None)
    # non-target path → allowed even with a secret (guard.py + .secrets perms cover real secret files)
    ck("non-target", scan("README.md", _fx("password: whatever", "123")) is None)
    # redacted / .secrets reference → allowed
    ck("redacted-ok", scan("memory/a.md", "password: [REDACTED->.secrets]") is None)
    ck("ref-ok", scan("work.json", "reads RR_PASSWORD from .secrets/royalroad.env") is None)
    # a bare label with no value → allowed
    ck("bare-label", scan("rollup.md", "the login uses a password") is None)
    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK (8/8)")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    sys.exit(main())
