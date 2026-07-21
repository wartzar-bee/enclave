#!/usr/bin/env python3
"""
vault_snapshot.py — durable, secret-safe persistence for an agent's memory vault.

The vault (the agent home: knowledge/ + memory/ + skills/ + work.json + CLAUDE.md) is the brain.
This makes it survive a machine wipe WITHOUT leaking secrets — one source of truth for the secret
patterns, used by both `bin/enclave` (host) and runtime.sh (post-tick auto-snapshot in-container).

  init      home/ becomes its OWN git repo; .gitignore excludes secrets/state/logs; a fail-closed
            pre-commit hook blocks any credential. (Independent of the product repo.)
  snapshot  stage -> SCAN tracked files for credential patterns -> commit ONLY if clean (fail-closed).
            git history is forever, so a token pasted into a lesson must never reach it.
  encrypt   strong at-rest option: an encrypted archive of the brain (openssl AES-256 + PBKDF2;
            key in .vault/key, never committed) so an off-machine copy is CIPHERTEXT, not
            plaintext — covers a scanner miss. (age/git-crypt are drop-in upgrades when installed.)
  decrypt   restore the brain from an encrypted archive.

The agent itself can't git (guard-blocked); the runtime/operator snapshots. Stdlib only.

CLI:
  vault_snapshot.py init     <home>
  vault_snapshot.py snapshot <home> [--msg "..."]      exit 3 = BLOCKED by the secret gate
  vault_snapshot.py encrypt  <home> [--out blob]
  vault_snapshot.py decrypt  <home> --in blob
"""
import sys, os, re, io, time, tarfile, shutil, argparse, subprocess, pathlib

_GIT_ENV = {"GIT_AUTHOR_NAME": "enclave", "GIT_AUTHOR_EMAIL": "enclave@local",
            "GIT_COMMITTER_NAME": "enclave", "GIT_COMMITTER_EMAIL": "enclave@local"}

# High-confidence credential FORMATS. A string in one of these shapes is a credential in ANY context,
# so these are never exempted. Legitimate prose ("rotate the password") doesn't trip them.
SECRET_PATTERNS = (
    r"ghp_[A-Za-z0-9]{30,}", r"github_pat_[A-Za-z0-9_]{30,}",
    r"sk-ant-[A-Za-z0-9_-]{20,}", r"sk-or-v1-[A-Za-z0-9]{20,}", r"sk-[A-Za-z0-9]{32,}",
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"xox[baprs]-[A-Za-z0-9-]{10,}", r"AIza[0-9A-Za-z_-]{35}", r"glpat-[A-Za-z0-9_-]{20,}",
)
_SECRET_RE = [re.compile(p) for p in SECRET_PATTERNS]

# Generic `label = value` assignments. Format-free, so these run through the reference filter below:
# an agent skill legitimately writes `imap_code{secret:google-logancross.env,...}` — a REFERENCE to a
# secrets FILE, not a credential. Blocking those silently froze logan-cross's vault backups for days.
# The leading `[A-Za-z0-9_]*` matters: a plain `\b` cannot fire after an underscore, so the original
# pattern never matched `RR_PASSWORD=…` — the exact leak the gate was built for.
LABEL_VALUE_PATTERNS = (
    r"(?i)[A-Za-z0-9_]*(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret|bearer)"
    r"\s*[:=]\s*['\"]?(?P<val>[A-Za-z0-9_\-./+=]{12,})",
    # env-assignment form: an uppercase credential name takes any non-space value (passwords carry
    # punctuation, which the conservative class above stops at).
    r"[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|CREDENTIALS?)"
    r"\s*[:=]\s*['\"]?(?P<val>\S{8,})",
    # Authorization header / curl -H form.
    r"(?i)authorization\s*[:=]\s*['\"]?(?:bearer|basic|token)\s+(?P<val>\S{12,})",
    # Google app-password (4x4 groups). Only ever matched behind a password LABEL — the bare 4x4 shape
    # is ordinary English ("when they were done") and would freeze every prose-heavy vault.
    r"(?i)(?:app[- ]?password|password)\s*[:=]\s*['\"]?(?P<val>[a-z0-9]{4}(?:\s+[a-z0-9]{4}){3})",
)
_LABEL_RES = [re.compile(p) for p in LABEL_VALUE_PATTERNS]

# ── reference-vs-literal: a credential is a LITERAL. These say "points at a credential" ──────────
_SECRETS_FILE = re.compile(r"(?i)^[\w\-./]*\.(?:env|json|ya?ml|pem|key|txt|ini|toml|cfg|conf)$")
_ENV_REF = re.compile(r"^\$\{?[A-Za-z_]\w*\}?$")
_DOTTED_IDENT = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")          # args.password, re.compile
_PLACEHOLDER = re.compile(r"(?i)^(?:redacted|placeholder|example|changeme|your[-_]?\w*|dummy|fake"
                          r"|none|null|todo|tbd|x{3,}|\*{3,}|\.{3,}|_{3,}|-{3,})")


def _looks_random(v):
    """A 16+ char run mixing upper, lower AND digits — JWT/base64/API-token entropy that no code
    identifier or file path has. Wins over every exemption below: `Bearer eyJhbGciOiJIUzI1NiJ9.abc`
    is dotted like an attribute access, but it is a token."""
    for run in re.findall(r"[A-Za-z0-9]{16,}", v):
        if any(c.isupper() for c in run) and any(c.islower() for c in run) and any(c.isdigit() for c in run):
            return True
    return False


def _is_reference(val):
    """True when the captured value REFERENCES a credential rather than being one. Wrappers are
    stripped first, so `[REDACTED->.secrets/x.env]` and `"$TOKEN"` classify correctly.

    This filter is why the gate is usable: it is fail-closed, so a false positive silently freezes a
    pod's whole brain backup. Every rule here is anchored or structural — a bare literal like
    `3r2s-e726-ct5d-y4ee` matches none of them and still blocks."""
    v = (val or "").strip("'\"[](){}<>`,;")
    if not v or len(v) < 8:
        return True
    if _looks_random(v):
        return False
    # $VAR, $(cmd), re.compile(...), os.environ[...], and shell defaults `${A:-$B}` whose captured
    # value arrives as `-$B` once the braces are stripped.
    if v[0] == "$" or v.lstrip("-:").startswith("$") or "(" in v or "[" in v:
        return True
    if ".secrets" in v or v.startswith("secrets/"):    # a path into the vault of record
        return True
    return bool(_SECRETS_FILE.match(v) or _ENV_REF.match(v)
                or _DOTTED_IDENT.match(v) or _PLACEHOLDER.match(v))

VAULT_GITIGNORE = (
    "# Enclave deployment vault — durable memory is TRACKED; secrets + runtime state are NOT.\n"
    "secrets/\n"        # defensive: never commit a stray cred dir
    ".vault/\n"         # the vault encryption key lives here — NEVER commit it
    "state/\n"          # transient per-tick runtime state
    "logs/\n"
    "uploads/\n"
    "*.log\n"
    "*.enc\n"           # encrypted vault archives live outside git
    ".DS_Store\n"
)

# Pre-commit hook so even a MANUAL `git commit` is scan-gated (self-contained — same families).
_PRECOMMIT = r'''#!/usr/bin/env bash
set -euo pipefail
pat='ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-or-v1-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35}|glpat-[A-Za-z0-9_-]{20,}'
bad=0
while IFS= read -r f; do
  [ -f "$f" ] || continue
  if grep -EnI "$pat" "$f" >/dev/null 2>&1; then echo "BLOCKED: credential pattern in $f" >&2; bad=1; fi
done < <(git diff --cached --name-only)
[ "$bad" -eq 0 ] || { echo "Vault commit blocked — move the secret to secrets/ (gitignored)." >&2; exit 1; }
'''

# The durable brain: what encrypt/backup includes (NEVER secrets/state/logs).
BRAIN_DIRS = ("knowledge", "memory", "skills")
BRAIN_FILES = ("work.json", "CLAUDE.md", "agent.env", "inbox.md")


def _git(home, *a, **kw):
    return subprocess.run(["git", *a], cwd=home, capture_output=True, text=True,
                          env={**os.environ, **_GIT_ENV}, **kw)


def scan_text(text):
    """Return a matched credential snippet, or None. A credential FORMAT is absolute; a generic
    `label = value` hit is skipped only when the value is a reference (secrets file, $VAR,
    placeholder) rather than a credential."""
    for pat in _SECRET_RE:
        m = pat.search(text)
        if m:
            return m.group(0)[:24] + "…"
    for pat in _LABEL_RES:
        for m in pat.finditer(text):
            if _is_reference(m.group("val")):
                continue
            return m.group(0)[:24] + "…"
    return None


def scan_staged(home):
    """Return [(file, snippet)] for any STAGED file matching a credential pattern (the gate)."""
    r = _git(home, "diff", "--cached", "--name-only", "-z")
    hits = []
    for rel in (r.stdout.split("\0") if r.stdout else []):
        if not rel:
            continue
        try:
            text = (home / rel).read_text(errors="ignore")
        except OSError:
            continue
        snip = scan_text(text)
        if snip:
            hits.append((rel, snip))
    return hits


def ensure_repo(home):
    """Idempotently make home/ a scan-gated git vault. Returns 'created'|'exists'|None(no git)."""
    gi = home / ".gitignore"
    if not gi.exists():
        gi.write_text(VAULT_GITIGNORE)
    if shutil.which("git") is None:
        return None
    if (home / ".git").exists():
        # keep the hook fresh even on an existing repo
        hook = home / ".git" / "hooks" / "pre-commit"
        if not hook.exists():
            hook.write_text(_PRECOMMIT); os.chmod(hook, 0o755)
        return "exists"
    _git(home, "init", "-q")
    hook = home / ".git" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(_PRECOMMIT); os.chmod(hook, 0o755)
    snapshot(home, "init vault (durable memory: knowledge + memory + skills)")
    return "created"


def snapshot(home, msg=None):
    """Stage + SCAN + commit. Returns ('ok'|'nochange'|'blocked'|None, hits)."""
    if shutil.which("git") is None:
        return None, []
    if not (home / ".git").exists():
        ensure_repo(home)
    _git(home, "add", "-A")
    hits = scan_staged(home)
    if hits:
        _git(home, "reset", "-q")          # unstage; a secret never reaches a commit
        return "blocked", hits
    msg = msg or f"snapshot {time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}"
    r = _git(home, "commit", "-q", "-m", msg)
    return ("ok" if r.returncode == 0 else "nochange"), []


# ── encryption-at-rest (strong option: an off-machine copy is CIPHERTEXT) ────────────────────
def _vault_key(home):
    """A 256-bit key in secrets/ (gitignored, 600). Generated once; back it up SEPARATELY — lose it
    and the encrypted archive is unrecoverable."""
    keyf = home / ".vault" / "key"
    keyf.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not keyf.exists():
        keyf.write_text(os.urandom(32).hex() + "\n"); os.chmod(keyf, 0o600)
    return keyf


def _age_ok():
    """age is the preferred encryptor (modern, audited, no PBKDF2-tuning footguns). We only USE it if the
    operator has installed it — Enclave never auto-installs an external dep. Falls back to openssl."""
    return shutil.which("age") is not None and shutil.which("age-keygen") is not None


def _age_identity(home):
    """An X25519 identity in .vault/age-identity.txt (gitignored, 600). Generated once; back it up
    SEPARATELY — lose it and the archive is unrecoverable."""
    idf = home / ".vault" / "age-identity.txt"
    idf.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not idf.exists():
        p = subprocess.run(["age-keygen", "-o", str(idf)], capture_output=True)
        if p.returncode != 0:
            raise SystemExit(f"age-keygen failed: {p.stderr.decode(errors='ignore')[:200]}")
        os.chmod(idf, 0o600)
    return idf


def _age_recipient(idf):
    p = subprocess.run(["age-keygen", "-y", str(idf)], capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit(f"age-keygen -y failed: {(p.stderr or '')[:200]}")
    return p.stdout.strip()


def _tar_brain(home):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for sub in BRAIN_DIRS:
            if (home / sub).is_dir():
                tar.add(home / sub, arcname=sub)
        for f in BRAIN_FILES:
            if (home / f).exists():
                tar.add(home / f, arcname=f)
    return buf.getvalue()


def encrypt(home, out=None):
    """Encrypt the brain (NOT secrets/) to a ciphertext archive. Prefers `age` (X25519) when installed,
    else openssl AES-256+PBKDF2. Auto-detected on decrypt. Returns the blob path."""
    out = pathlib.Path(out) if out else home.parent / f"{home.resolve().name}-vault.enc"
    data = _tar_brain(home)
    if _age_ok():
        idf = _age_identity(home)
        p = subprocess.run(["age", "-r", _age_recipient(idf), "-o", str(out)],
                           input=data, capture_output=True)
        if p.returncode != 0:
            raise SystemExit(f"age encrypt failed: {p.stderr.decode(errors='ignore')[:200]}")
        return out
    if shutil.which("openssl") is None:
        raise SystemExit("no encryptor found — install `age` (recommended) or `openssl` for vault encryption.")
    keyf = _vault_key(home)
    p = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-salt", "-pass", f"file:{keyf}"],
                       input=data, capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"openssl encrypt failed: {p.stderr.decode(errors='ignore')[:200]}")
    out.write_bytes(p.stdout)
    return out


def decrypt(home, blob):
    """Restore the brain into home/ (overwrites the brain dirs). Auto-detects age vs openssl from the
    archive header, so it works regardless of which encryptor produced it."""
    raw = pathlib.Path(blob).read_bytes()
    if raw[:18].startswith(b"age-encryption.org"):          # age armored/binary both carry this banner
        if shutil.which("age") is None:
            raise SystemExit("archive is age-encrypted but `age` is not installed.")
        idf = home / ".vault" / "age-identity.txt"
        if not idf.exists():
            raise SystemExit(f"missing identity {idf} — the archive is unrecoverable without it.")
        p = subprocess.run(["age", "-d", "-i", str(idf)], input=raw, capture_output=True)
        if p.returncode != 0:
            raise SystemExit(f"age decrypt failed: {p.stderr.decode(errors='ignore')[:200]}")
        plain = p.stdout
    else:
        if shutil.which("openssl") is None:
            raise SystemExit("openssl not found (archive is openssl-encrypted).")
        keyf = home / ".vault" / "key"
        if not keyf.exists():
            raise SystemExit(f"missing key {keyf} — the archive is unrecoverable without it.")
        p = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-pass", f"file:{keyf}"],
                           input=raw, capture_output=True)
        if p.returncode != 0:
            raise SystemExit(f"openssl decrypt failed (wrong key?): {p.stderr.decode(errors='ignore')[:200]}")
        plain = p.stdout
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        tar.extractall(home)   # archive only ever contains BRAIN_DIRS/FILES we wrote
    return home


def main():
    ap = argparse.ArgumentParser(prog="vault_snapshot.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("init", "snapshot", "encrypt", "decrypt"):
        s = sub.add_parser(c); s.add_argument("home")
        if c == "snapshot": s.add_argument("--msg", default=None)
        if c == "encrypt": s.add_argument("--out", default=None)
        if c == "decrypt": s.add_argument("--in", dest="blob", required=True)
    a = ap.parse_args()
    home = pathlib.Path(a.home).resolve()
    if a.cmd == "init":
        print(f"vault: {ensure_repo(home)}")
    elif a.cmd == "snapshot":
        r, hits = snapshot(home, a.msg)
        if r == "blocked":
            sys.stderr.write("BLOCKED — credential in tracked memory (git history is forever):\n")
            for f, snip in hits:
                sys.stderr.write(f"  {f}: {snip}\n")
            sys.stderr.write("  Move it to secrets/ and redact from memory, then re-run.\n")
            sys.exit(3)
        print("ok" if r == "ok" else "nochange" if r == "nochange" else "no-git")
    elif a.cmd == "encrypt":
        print(f"encrypted → {encrypt(home, a.out)}")
    elif a.cmd == "decrypt":
        print(f"restored brain into {decrypt(home, a.blob)}")


if __name__ == "__main__":
    main()
