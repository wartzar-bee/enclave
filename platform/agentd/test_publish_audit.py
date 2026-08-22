#!/usr/bin/env python3
"""Tests for tools/publish_audit.py — the enforcer .publish-audit-allow never had.

The file shipped with no scanner reading it; an external security reviewer cited it as a working
control and reasoned from it. So the scanner needs tests that assert the properties that make it a
control rather than decoration: it FAILS CLOSED, it catches the leak classes, it does not flag the
repo's own address, and the allowlist exempts by LINE (its documented contract).

Run: python3 test_publish_audit.py
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location("pa", ROOT / "tools" / "publish_audit.py")
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
    print(("ok  " if cond else "FAIL ") + name)


def hits(text, kinds=None):
    """Run the raw patterns over one line, the way audit() does."""
    import re
    out = []
    for kind, pat in pa.PATTERNS.items():
        if re.search(pat, text, re.I):
            out.append(kind)
    return out


# --- the generic class it ships with ------------------------------------------------------------
check("catches an operator checkout path", "operator-path" in hits("--fleet-root /Users/alice/Dev/x"))

# --- THE PROPERTY THAT MATTERS: the public file must not name the private things ----------------
# On first write the pattern list hardcoded the pod names, fleet roots and company name. In a PUBLIC
# repo that IS the leak — the scanner published what it protects. Caught by running it on itself.
src = (ROOT / "tools" / "publish_audit.py").read_text().lower()
for term in ("stoneforge", "logan-cross", "financial-advisor", "peterandsons", "agent-pas-ops"):
    check(f"the public scanner does not name {term!r}", term not in src)
check("private terms are loaded from a file, not hardcoded", hasattr(pa, "load_deny"))

# --- operator-supplied private terms are honoured when present ---------------------------------
deny, present = pa.load_deny()
check("reports whether the private list is present", isinstance(present, bool))

# --- what it must NOT flag ---------------------------------------------------------------------
check("does NOT flag the org that owns this repo",
      hits("git clone https://github.com/wartzar-bee/enclave.git") == [])
check("does NOT flag documentation placeholders",
      hits("e.g. /Users/you/Dev/myproject") == [] and hits("/Users/me/Dev/x") == [])
check("does NOT flag the container's own /agent path", hits("mounts home at /agent/state") == [])

# --- fail closed: the property that separates a control from decoration ------------------------
real_allow = pa.ALLOW
try:
    pa.ALLOW = ROOT / ".publish-audit-allow.does-not-exist"
    allow, violations, _ = pa.audit()
    check("FAILS CLOSED when the allow file is missing (audit returns None)", allow is None)
    check("main() exits 2 on a missing allow file", pa.main() == 2)
finally:
    pa.ALLOW = real_allow

# --- the allowlist contract: exempt by LINE ----------------------------------------------------
allow = pa.load_allow()
check("allow file parses to compiled rules", allow is not None and len(allow) > 0)
check("an allowlisted line is exempt even though a pattern matches it",
      any(a.search("marketing/ cites tokenscope") for a in (allow or [])))

# --- the live tree must be clean ---------------------------------------------------------------
allow, violations, _ = pa.audit()
check(f"the published tree is clean (found {len(violations)})", not violations)

print(f"\n{'OK' if not fails else 'FAIL'} {len(fails)} failed")
raise SystemExit(1 if fails else 0)
