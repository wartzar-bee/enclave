#!/usr/bin/env python3
"""
test_secrets.py — the framework's single credential definition, incl. shell PORTABILITY.

The portability half is not hypothetical. The studio's pre-commit hook wrote its quote class as
`["\x27]`. GNU grep reads that as ["'] — BSD grep (the macOS default, and what a git hook actually
runs) reads it as the literal characters \ x 2 7. On macOS the hook therefore MISSED every
single-quoted secret (`password='hunter2secretvalue'`) while false-blocking legitimate secrets
*reads* like `grep -oP 'TOKEN=\K.*' .secrets/x.env`. It sat like that for weeks and nobody could
tell, because a scanner that silently matches nothing looks exactly like a clean repo.

So: bash_pattern() is asserted to behave IDENTICALLY under every grep implementation present, and
to agree with the Python scanner on the same inputs.
"""
import os, shutil, subprocess, sys, tempfile, pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secrets as S

fails = []


def ck(name, cond):
    if not cond:
        fails.append(name)


def fx(*parts):
    """Assemble fixtures — never a literal credential in a file that gets scanned."""
    return "".join(parts)


# ── one definition, no drift ─────────────────────────────────────────────────────────────────
ck("formats non-empty", len(S.FORMATS) >= 10)
ck("bash pattern built from FORMATS",
   all(p in S.bash_pattern() for p, _ in S.FORMATS))

# ── PORTABILITY: the emitted ERE must mean the same thing to every grep ──────────────────────
GREPS = [g for g in ("/usr/bin/grep", shutil.which("ggrep"), shutil.which("grep"))
         if g and os.path.exists(g)]
ck("at least one grep found", bool(GREPS))

POSITIVE = [fx("ghp", "_") + "b" * 36,
            fx("sk-ant-", "api03-") + "x" * 40,
            fx("AKIA", "IOSFODNN7EXAMPLE"),
            fx("xoxb", "-123456789012-abcdef"),
            fx("-----BEGIN ", "OPENSSH PRIVATE KEY-----")]
NEGATIVE = ["rotate the password before the next tick",
            "api_key = os.environ['NVIDIA_API_KEY']",
            "imap_code{secret:google-logancross.env}",
            "see businesses/enclave/platform/agentd/secrets.py"]

pat = S.bash_pattern()
tmp = pathlib.Path(tempfile.mkdtemp(prefix="sectest-"))
try:
    results = {}
    for g in GREPS:
        hits = []
        for i, s in enumerate(POSITIVE + NEGATIVE):
            f = tmp / f"c{i}.txt"
            f.write_text(s + "\n")
            r = subprocess.run([g, "-EnI", pat, str(f)], capture_output=True)
            hits.append(r.returncode == 0)
        results[g] = hits
    # every grep agrees with every other grep
    baseline = results[GREPS[0]]
    for g, hits in results.items():
        ck(f"portable across {os.path.basename(g)}", hits == baseline)
    # ...and agrees with the truth: all positives match, no negative matches
    ck("shell matches every credential format", all(baseline[:len(POSITIVE)]))
    ck("shell matches no ordinary line", not any(baseline[len(POSITIVE):]))
    # the specific historical bug: a class written \x27 is NOT portable. Assert we emit no \x, \s, \d.
    for bad in ("\\x", "\\s", "\\d", "\\w", "(?"):
        ck(f"no non-portable escape {bad!r}", bad not in pat)

    # ── the regression that started it: single-quoted secrets must be caught by the PYTHON
    # scanner (the shell pattern is format-only by design, which is why the label families live
    # in Python where is_reference() can run).
    ck("single-quoted secret detected", S.scan_text("password='" + fx("hunter2", "secretvalue") + "'") is not None)
    ck("double-quoted secret detected", S.scan_text('api_key="' + fx("abcdefgh", "12345678") + '"') is not None)
    ck("secrets READ not flagged", S.scan_text("grep -oP 'TOKEN=\\K.*' /workspace/.secrets/x.env") is None)
    # a regex read froze channel-lab's vault backup: the captured "value" was `.*\|TOKEN=.*`
    ck("regex read not flagged",
       S.scan_text("TOKEN=$(grep -o 'X_TOKEN=.*" + chr(92) + "|TOKEN=.*' /workspace/.secrets/x.env)") is None)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── detection agrees with redaction: anything scan_text finds, redact must remove ─────────────
for s in POSITIVE + ["password='" + fx("hunter2", "secretvalue") + "'",
                     fx("RR_PASSWORD=", "Sw0rdfish!longenough")]:
    if S.scan_text(s):
        ck(f"redact removes what scan finds: {s[:18]}", S.scan_text(S.redact(s)) is None)

# ── redaction must not destroy ordinary text ─────────────────────────────────────────────────
for s in NEGATIVE:
    ck(f"redact keeps prose: {s[:20]}", S.redact(s) == s)

if fails:
    print(f"FAIL ({len(fails)}): " + ", ".join(fails))
    sys.exit(1)
print(f"secrets: one definition + shell portability OK (greps checked: "
      f"{', '.join(os.path.basename(g) for g in GREPS)})")
