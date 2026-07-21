#!/usr/bin/env python3
"""
test_vault_snapshot.py — the vault secret gate: block real credentials, allow references.

The gate is fail-closed and silent (runtime.sh logs "blocked or no-op"), so a false positive costs
DAYS of missing backups before anyone notices — scribepod wrote a browser recipe containing
`imap_code{secret:google-logancross.env,...}` (a filename REFERENCE) and its vault stopped
committing. These tests pin both directions: a reference must pass, a credential must still block.
"""
import os, shutil, subprocess, sys, tempfile, pathlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vault_snapshot as V

fails = []


def ck(name, cond):
    if not cond:
        fails.append(name)


def fx(*parts):
    """Assemble a fixture. Credential-shaped strings are never written as literals here — this file is
    scanned by the repo's own pre-commit hook, and the same discipline keeps any copy of it from
    freezing a vault (a literal fixture in `hooks/secret_scan.py` did exactly that to demopod)."""
    return "".join(parts)


# ── must BLOCK: real credential values ───────────────────────────────────────────────────────
ck("ghp-token",        V.scan_text("token ghp_" + "a" * 36) is not None)
ck("anthropic-key",    V.scan_text("sk-ant-api03-" + "x" * 40) is not None)
ck("openrouter-key",   V.scan_text("key: sk-or-v1-" + "0" * 32) is not None)
ck("aws-key",          V.scan_text(fx("AKIA", "IOSFODNN7EXAMPLE")) is not None)
ck("private-key",      V.scan_text(fx("-----BEGIN ", "OPENSSH PRIVATE KEY-----")) is not None)
ck("slack-token",      V.scan_text(fx("xoxb", "-123456789012-abcdef")) is not None)
ck("google-api-key",   V.scan_text("AIza" + "b" * 35) is not None)
ck("password-value",   V.scan_text(fx("password: hunter", "2xyzABC9")) is not None)
ck("rr-password",      V.scan_text(fx("RR_PASSWORD=", "Sw0rdfish!longenough")) is not None)
ck("api-key-value",    V.scan_text(fx("api_key = ", "8f3a91c2b7d40e65")) is not None)
ck("auth-header",      V.scan_text(fx("Authorization: Bearer ", "eyJhbGciOiJIUzI1NiJ9", ".abcdefghij")) is not None)
ck("env-punct-value",  V.scan_text("GOOGLE_APP_PASSWORD='abcd efgh'".replace(" ", "!")) is not None)
# a format hit inside an otherwise reference-looking line still blocks
ck("format-wins",      V.scan_text("secret: .secrets/x.env ghp_" + "c" * 36) is not None)
# The REAL leak this gate caught in labpod's memory: a bluesky app password (since revoked).
# Fixtures are ASSEMBLED, never literal — a credential-shaped literal in a scanned tree is exactly
# what froze demopod's vault, and it trips the repo's pre-commit hook too.
ck("bsky-app-password", V.scan_text(
    "- App password: " + "3r2s-e726" + "-ct5d-y4ee" + " works for x.bsky.social") is not None)
ck("google-app-pw",    V.scan_text("app password: abcd " + "efgh ijkl mnop") is not None)

# ── must ALLOW: references, placeholders, prose ──────────────────────────────────────────────
ck("recipe-ref",       V.scan_text(
    "imap_code{secret:google-logancrossy.env,from_contains:spotify,max_age:240}") is None)
ck("secrets-path",     V.scan_text("api_key: .secrets/nvidia.env") is None)
ck("bare-secrets-path", V.scan_text("password = secrets/royalroad.env") is None)
ck("env-var-ref",      V.scan_text("password: $RR_PASSWORD") is None)
ck("env-var-braced",   V.scan_text("api_key=${NVIDIA_API_KEY}") is None)
ck("redacted",         V.scan_text("password: [REDACTED]".replace("[", "").replace("]", "")) is None)
ck("placeholder",      V.scan_text("access_token: <your-token-here>") is None)
ck("stars",            V.scan_text("password: ************") is None)
ck("xxx",              V.scan_text("client_secret: xxxxxxxxxxxx") is None)
ck("prose",            V.scan_text("rotate the password before the next tick") is None)
ck("prose-4x4",        V.scan_text("that was when they were done with this") is None)
ck("short-value",      V.scan_text("password: abc") is None)
ck("json-ref",         V.scan_text('"secret": "credentials.json"') is None)
# every false positive that actually froze a live vault (2026-07-21) — regression pins
ck("fp-code-attr",     V.scan_text("password = args.password") is None)
ck("fp-code-call",     V.scan_text("FOREIGN_SECRET = re.compile(r'x')") is None)
ck("fp-shell-subst",   V.scan_text(
    "export CLOUDFLARE_API_TOKEN=$(grep ^CLOUDFLARE_API_TOKEN= /workspace/.secrets/cf.env|cut -d= -f2-)")
   is None)
ck("fp-bracket-redact", V.scan_text("RR_PASSWORD: [REDACTED->.secrets/royalroad.env] login form")
   is None)
ck("fp-env-index",     V.scan_text("api_key = os.environ['NVIDIA_API_KEY']") is None)
ck("fp-shell-default", V.scan_text('TOKEN="${BROWSER_BRIDGE_TOKEN:-$X_TOKEN}"') is None)

# ── end-to-end: a vault with a recipe reference COMMITS; one with a token BLOCKS ─────────────
if shutil.which("git"):
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="vaulttest-"))
    try:
        home = tmp / "home"
        (home / "skills").mkdir(parents=True)
        (home / "skills" / "recipe.md").write_text(
            "STEPS: imap_code{secret:google-logancrossy.env,from_contains:spotify}\n")
        V.ensure_repo(home)
        r, hits = V.snapshot(home, "reference-only")
        ck("e2e-ref-commits", r in ("ok", "nochange") and not hits)

        (home / "memory").mkdir(exist_ok=True)
        (home / "memory" / "leak.md").write_text("token ghp_" + "d" * 36 + "\n")
        r2, hits2 = V.snapshot(home, "real leak")
        ck("e2e-leak-blocks", r2 == "blocked" and hits2)
        # blocked => nothing staged, so the leak never reaches history
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=home,
                                capture_output=True, text=True).stdout.strip()
        ck("e2e-leak-unstaged", staged == "")
        log = subprocess.run(["git", "log", "--oneline"], cwd=home,
                             capture_output=True, text=True).stdout
        ck("e2e-leak-not-committed", "real leak" not in log)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
else:
    print("(git absent — end-to-end vault checks skipped)")

total = 35 if shutil.which("git") else 31
if fails:
    print(f"FAIL ({len(fails)}): " + ", ".join(fails))
    sys.exit(1)
print(f"vault_snapshot secret gate OK ({total}/{total})")
