#!/usr/bin/env python3
"""secrets.py — the framework's ONE definition of "this looks like a credential".

Before this module the same knowledge was copy-pasted into five places that had already drifted:
`vault_snapshot.py` (the vault gate), its embedded bash pre-commit hook, `hooks/secret_scan.py` (the
PreToolUse write-blocker), `hooks/event_log.py` and `local_agent.py` (redaction) — with `ghp_` written
as `{16,}` in two of them and `{30,}` in another, the bash copy missing whole families, and only one
of them able to tell a credential from a *reference* to one. A scanner that disagrees with itself is
worse than one scanner: each caller gets a different answer and nobody knows which is authoritative.

Three jobs, one place:
  scan_text(text)  -> snippet | None   DETECT a credential (fail-closed gates: vault, write-blocker)
  redact(text)     -> text             STRIP credentials from anything rendered/logged/reported
  bash_pattern()   -> ERE string       the SAME families for shell hooks, generated not hand-copied

PORTABILITY (this bit is not theoretical): the orchestrator's own pre-commit hook wrote its quote class as
`["\\x27]`. GNU grep reads that as ["'], BSD grep (macOS default) reads it as the literal characters
\\ x 2 7 — so on macOS it MISSED every single-quoted secret (`password='hunter2secretvalue'`) while
false-blocking legitimate secrets *reads*. bash_pattern() therefore emits only POSIX-portable ERE:
bracket expressions with real characters and [[:space:]] classes, never \\x, \\s, \\d or backrefs.
test_secrets.py asserts the emitted pattern behaves identically under every grep on the box.
"""
import re

# ── high-confidence credential FORMATS ───────────────────────────────────────────────────────
# A string in one of these shapes is a credential in ANY context — never exempted, never redacted
# away by a "looks like a reference" rule.
FORMATS = (
    (r"ghp_[A-Za-z0-9]{30,}", "github-pat"),
    (r"github_pat_[A-Za-z0-9_]{30,}", "github-fine-grained"),
    (r"gho_[A-Za-z0-9]{30,}", "github-oauth"),
    (r"glpat-[A-Za-z0-9_-]{20,}", "gitlab"),
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "anthropic"),
    (r"sk-or-v1-[A-Za-z0-9]{20,}", "openrouter"),
    (r"sk-[A-Za-z0-9]{32,}", "openai-like"),
    (r"nvapi-[A-Za-z0-9_-]{20,}", "nvidia"),
    (r"AKIA[0-9A-Z]{16}", "aws-access-key"),
    (r"AIza[0-9A-Za-z_-]{35}", "google-api-key"),
    (r"xox[baprs]-[A-Za-z0-9-]{10,}", "slack"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private-key"),
)
_FORMAT_RE = [(re.compile(p), name) for p, name in FORMATS]

# ── label = value assignments (format-free, so they need the reference filter below) ──────────
LABEL_VALUE = (
    # `\b` cannot fire after an underscore, so a plain \b(password) never matched RR_PASSWORD= —
    # the exact leak class the write-blocker was built for.
    r"(?i)[A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret|bearer)"
    r"\s*[:=]\s*['\"]?(?P<val>[A-Za-z0-9_\-./+=]{12,})",
    # env-assignment: an uppercase credential name takes any non-space value (passwords carry punctuation)
    r"[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|CREDENTIALS?)"
    r"\s*[:=]\s*['\"]?(?P<val>\S{8,})",
    # Authorization header / curl -H form (opaque token, no prefix, no key name)
    r"(?i)authorization\s*[:=]\s*['\"]?(?:bearer|basic|token)\s+(?P<val>\S{12,})",
    # Google app-password 4x4 — ONLY behind a password label; the bare shape is ordinary English
    # ("when they were done") and would freeze every prose-heavy vault.
    r"(?i)(?:app[- ]?password|password)\s*[:=]\s*['\"]?(?P<val>[a-z0-9]{4}(?:\s+[a-z0-9]{4}){3})",
)
_LABEL_RE = [re.compile(p) for p in LABEL_VALUE]

# ── reference-vs-literal: a credential is a LITERAL ──────────────────────────────────────────
_SECRETS_FILE = re.compile(r"(?i)^[\w\-./]*\.(?:env|json|ya?ml|pem|key|txt|ini|toml|cfg|conf)$")
_ENV_REF = re.compile(r"^\$\{?[A-Za-z_]\w*\}?$")
_DOTTED_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
# A CALL produces a credential, it is not one: `password = _gen_password()`,
# `token = os.getenv("X")`, `key = load_key(path)`. This blocked one deployment's vault for hours —
# a fail-closed gate that fires on a false positive stops the backup just as dead as a real leak,
# so precision here is a availability property, not just tidiness. looks_random() still overrides.
_CALL = re.compile(r"^[A-Za-z_][\w.]*\s*\(.*\)$")
_PLACEHOLDER = re.compile(r"(?i)^(?:redacted|placeholder|example|changeme|your[-_]?\w*|dummy|fake"
                          r"|none|null|todo|tbd|x{3,}|\*{3,}|\.{3,}|_{3,}|-{3,})")


def looks_random(v):
    """A 16+ char run mixing upper, lower AND digits — token entropy no identifier or path has.
    Overrides every exemption below: `Bearer eyJhbGciOiJIUzI1NiJ9.abc` is dotted like an attribute
    access, but it is a token."""
    for run in re.findall(r"[A-Za-z0-9]{16,}", v or ""):
        if any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run):
            return True
    return False


def is_reference(val):
    """True when a captured value POINTS AT a credential rather than being one.

    This filter is why a fail-closed gate is usable: a false positive silently freezes an agent's
    whole brain backup (it did — three pods had never backed up). Every rule is anchored or
    structural, so a bare literal like `3r2s-e726-ct5d-y4ee` still blocks."""
    raw = (val or "").strip()
    v = (val or "").strip("'\"[](){}<>`,;")
    if not v or len(v) < 8:
        return True
    if looks_random(v):
        return False
    # Checked against RAW, before the strip above: stripping "()" off `_gen_password()` leaves a bare
    # identifier, so the `"(" in v` test below never fired for a no-argument call. That single gap
    # froze a deployment's brain backup for hours.
    if _CALL.match(raw):
        return True
    # An INTERPOLATION placeholder, not a value: f"ECHOJS_APISECRET={apisecret}\n", ${VAR}, {{tmpl}}.
    # The strip above removes the braces, leaving a bare identifier that looks like a literal — the
    # same shape that made a no-arg call slip through. Code that WRITES a credential file is the
    # normal case for an account-provisioning pod; flagging it froze its brain backup.
    # Matched with search(), not match(): the capture runs to the next whitespace, so the real value
    # is `{apisecret}\n")` — trailing source, not a clean placeholder. Anchoring on the START of the
    # capture is what matters; a literal credential never begins with `{name}`.
    if re.match(r"^\{[A-Za-z_]\w*\}", raw) or re.match(r"^\{\{[A-Za-z_]", raw):
        return True
    # $VAR, $(cmd), ${A:-$B}, re.compile(...), os.environ[...]
    if v[0] == "$" or v.lstrip("-:").startswith("$") or "(" in v or "[" in v:
        return True
    if ".secrets" in v or v.startswith("secrets/"):
        return True
    # A REGEX/GLOB fragment, not a literal: `TOKEN=$(grep -o 'X_TOKEN=.*\|TOKEN=.*' .secrets/x.env)`
    # is a command that READS a credential. `.*` and `\|` are regex constructs, not password
    # characters, and blocking the read froze a pod's brain backup (2026-07-21).
    if ".*" in v or "\\|" in v or v.startswith("*"):
        return True
    # A sed/awk SCRIPT that extracts a credential, e.g.
    #   T=$(sed -n 's/^X_TOKEN=//p;s/^BRIDGE_TOKEN=//p' /workspace/.secrets/x.env)
    # The captured value is `//p;s/^BRIDGE_TOKEN=//p'` — a substitution program, not a secret. No
    # credential contains `s/`, `//p` or an unbalanced quote, and reading a secret is the ONE thing
    # a pod must be able to do without freezing its own brain backup (this blocked one twice).
    if "//p" in v or v.startswith(("s/", "n;", "p;")) or ";s/" in v:
        return True
    return bool(_SECRETS_FILE.match(v) or _ENV_REF.match(v)
                or _DOTTED_IDENT.match(v) or _PLACEHOLDER.match(v))


def scan_text(text, with_kind=False):
    """Return a matched credential snippet (or (snippet, kind)), else None."""
    if not text:
        return None
    for rx, kind in _FORMAT_RE:
        m = rx.search(text)
        if m:
            snip = m.group(0)[:24] + "…"
            return (snip, kind) if with_kind else snip
    for rx in _LABEL_RE:
        for m in rx.finditer(text):
            if is_reference(m.group("val")):
                continue
            # The val group stops at the first non-word char, so a CALL arrives here as a bare
            # identifier: `password = _gen_password()` captures `_gen_password`. Look at what
            # follows — an opening paren means the value is PRODUCED, not written down. Missing
            # this froze a pod's brain backup for 3.7h on a line that generates a password.
            if text[m.end("val"):m.end("val") + 1] == "(":
                continue
            snip = m.group(0)[:24] + "…"
            return (snip, "label-value") if with_kind else snip
    return None


# ── redaction (for anything rendered, logged, or reported) ───────────────────────────────────
# Both quote characters, on purpose. Written with only `"` this missed `password='...'` — the very
# same single-quote blind spot that made the orchestrator's shell hook useless on macOS.
_RED_KV = re.compile(
    r"\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY)[A-Z0-9_]*)"
    r"(\\?['\"]?\s*[=:]\s*)(?P<val>\\?['\"]?[^\s\"']{6,}\\?['\"]?)", re.I)
_RED_AUTH = re.compile(r"((?:authorization|proxy-authorization)\s*:\s*)(bearer|basic|token)(\s+)"
                       r"([^\s\"'\\]{8,})", re.I)
_RED_LABELED = re.compile(r"\b(token|password|passwd|secret|api[_-]?key|cookie)(\s+)"
                          r"(?P<val>[A-Za-z0-9_\-]{20,})", re.I)
_RED_SHORTVAR = re.compile(r"\b(TOK|PW|PASS|AUTH|CRED|SESSION|BEARER)(\s*=\s*)"
                           r"(?P<val>\\?['\"]?[A-Za-z0-9._\-]{24,}\\?['\"]?)", re.I)
_RED_URLCREDS = re.compile(r"\b[A-Za-z0-9._%+-]+:[^\s@/]{6,}@")
_ENTROPY = re.compile(r"(?<![A-Za-z0-9_\-./])[A-Za-z0-9]{20,64}(?![A-Za-z0-9_\-./])")


def _entropy_sub(m):
    s = m.group(0)
    if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
        return "[REDACTED]"
    return s


def redact(text):
    """Strip credential-shaped substrings, keeping the KEY NAME so the line still says what was set.

    Every shape here is one that actually reached a git-tracked file in this studio: JSON bodies
    (a quote sits between key and colon), opaque `Authorization: Bearer`, abbreviated vars (`TOK=`),
    prose (`token XMzC…`), and bare unlabelled tokens (`echo 'GNdKTz…'`)."""
    if not text:
        return text
    for rx, name in _FORMAT_RE:
        text = rx.sub("[REDACTED]", text)
    text = _RED_URLCREDS.sub("[REDACTED]@", text)
    text = _RED_AUTH.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[REDACTED]", text)
    # The label families run through the SAME reference filter as detection, so redact() and
    # scan_text() cannot disagree: `os.environ['NVIDIA_API_KEY']` and `secret:google.env` are
    # pointers, and mangling them makes a log less readable without making it safer.
    def _keep_refs(m):
        if is_reference(m.group("val")):
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}[REDACTED]"
    text = _RED_LABELED.sub(_keep_refs, text)
    text = _RED_SHORTVAR.sub(_keep_refs, text)
    text = _RED_KV.sub(_keep_refs, text)
    return _ENTROPY.sub(_entropy_sub, text)


# ── shell hooks get the SAME families, generated ─────────────────────────────────────────────
def bash_pattern():
    """POSIX-portable ERE of the credential FORMATS, for `grep -E` in a shell hook.

    Generated from FORMATS so a bash hook can never drift from the Python gate. Deliberately
    format-only: the label=value families need is_reference() to stay usable, and a shell hook has
    no way to run it — a bash hook that blocked `password = args.password` would be turned off by
    the first person it annoyed.

    PORTABLE means: no \\x escapes, no \\s/\\d, no backreferences, no non-greedy — only literal
    characters, bracket expressions and [[:space:]]-style classes, which BSD and GNU ERE agree on.
    """
    return "|".join(p for p, _ in FORMATS)


def selftest():
    fails = []

    def ck(n, c):
        if not c:
            fails.append(n)

    fx = lambda *p: "".join(p)                      # assemble; never a literal credential in source
    # detect
    ck("ghp", scan_text("token " + fx("ghp", "_") + "a" * 36) is not None)
    ck("openrouter", scan_text("key " + fx("sk-or-", "v1-") + "0" * 32) is not None)
    ck("rr-password", scan_text(fx("RR_PASSWORD=", "Sw0rdfish!longenough")) is not None)
    ck("auth-header", scan_text(fx("Authorization: Bearer ", "eyJhbGciOiJIUzI1NiJ9", ".abcdefghij")) is not None)
    ck("bsky-app-pw", scan_text("App password: " + fx("3r2s-e726", "-ct5d-y4ee")) is not None)
    # reference, not credential
    ck("recipe-ref", scan_text("imap_code{secret:google-logancross.env,x:1}") is None)
    ck("env-ref", scan_text("password: $RR_PASSWORD") is None)
    ck("shell-default", scan_text('TOKEN="${BROWSER_BRIDGE_TOKEN:-$X_TOKEN}"') is None)
    ck("code-attr", scan_text("password = args.password") is None)
    ck("secrets-path", scan_text("api_key: .secrets/nvidia.env") is None)
    ck("prose", scan_text("rotate the password before the next tick") is None)
    # redact
    ck("redact-json", fx("3r2s-e726", "-ct5d-y4ee") not in redact('{"password":"' + fx("3r2s-e726", "-ct5d-y4ee") + '"}'))
    ck("redact-bare", fx("GNdKTzxr3", "PBkLh8MdDGiR5GE") not in redact("echo '" + fx("GNdKTzxr3", "PBkLh8MdDGiR5GE") + "'"))
    ck("redact-keeps-name", "RR_PASSWORD" in redact(fx("RR_PASSWORD=", "hunter2hunter2")))
    ck("redact-keeps-prose", redact("the token expired, ask the operator") == "the token expired, ask the operator")
    print(("secrets selftest FAIL: " + ", ".join(fails)) if fails
          else f"secrets selftest OK ({15 - len(fails)}/15)")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(selftest())
