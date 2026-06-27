---
skill: security-self-review
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (security-review skill + security-reviewer agent)
---

# Security self-review — fast pattern scan before shipping

Run this whenever you touch auth, user input, secrets, API endpoints, payments, or external fetches.
A first-pass scanner, not a full audit.

## Pattern → severity → fix (scan the diff for these)
| Pattern | Severity | Fix |
|---|---|---|
| Hardcoded secret / key / password | CRITICAL | read from env / `.secrets/` at runtime — never in source |
| String-concatenated SQL with user input | CRITICAL | parameterized query / ORM |
| Shell command built from user input | CRITICAL | safe API / `execFile` with arg array, no shell |
| Plaintext password compare | CRITICAL | bcrypt/argon2 `compare()` |
| No auth check on a sensitive route | CRITICAL | auth middleware before the handler |
| Balance/quota check without a lock | CRITICAL | `SELECT … FOR UPDATE` in a txn (TOCTOU race) |
| `fetch(userProvidedUrl)` | HIGH | allowlist domains (SSRF) — and block cloud-metadata IPs |
| `innerHTML = userInput` | HIGH | textContent / DOMPurify |
| No rate limiting on expensive/auth op | HIGH | limit (e.g. 10/min on search, 100/15min global) |
| Logging passwords / tokens / PII | MEDIUM | redact — log `last4`/`userId`, never the secret |

## Concrete defaults worth keeping
- Secrets only from env/`.secrets/`; `.env`/secret files gitignored; **rotate first** if one leaks (git history isn't undone by deletion).
- Validate input with a schema; **whitelist, not blacklist**; errors don't leak internal info.
- JWT in httpOnly+Secure+SameSite cookies, **not** localStorage. CSP: start strict, never default to `unsafe-inline`/`unsafe-eval`.
- File upload: size cap + MIME + extension whitelist.
- Deps: `npm ci` (not `install`) in CI; commit lockfiles; `npm audit` / `pip-audit`.

## False-positive guards (don't cry wolf)
Skip: `.env.example` placeholders, clearly-marked test creds, intentionally-public keys, SHA256/MD5 used for checksums (not passwords). **Always verify context before flagging.**

## On a real exposed secret
Document → rotate the credential → provide the secure replacement → verify. Rotation comes first; current-tree removal doesn't undo the leak.
