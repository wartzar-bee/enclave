---
skill: search-before-build
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (search-first)
---

# Search before you build — don't reinvent, don't reimplement

Before writing custom code for anything non-trivial, find what already solves it. Reimplementing a
solved problem burns a tick for nothing.

## Preflight — and disclose what you couldn't check (HARD)
Check each channel and HONESTLY report any you couldn't run. Never say "nothing found" when you didn't actually search — silent skipping is the worst anti-pattern here.
- **Repo first** — does it already exist here? (`rg`, codegraph, read sibling modules). Most "new" needs already have a pattern in-tree.
- **Memory / knowledge** — qmd (if configured) + `bin/memory.py recall` + `knowledge/`. You may have solved or rejected this before.
- **Package registries** — npm / PyPI / crates for a common need.
- **MCP / skills / tools** — is there already a bridge or skill for it?

## Decision matrix
| Signal | Action |
|---|---|
| Exact match, maintained, permissive licence | **Adopt** as-is |
| Partial match, good base | **Extend** — thin wrapper over it |
| Several weak matches | **Compose** — combine 2–3 small pieces |
| Nothing suitable | **Build** custom — but informed by what you found |

## Vetting gate (non-negotiable)
Adopting/extending external code = it must pass a security vetting FIRST (provenance, pin version, read the actual code for exfil, CVE-scan). Too sprawling to fully review → **don't install it; author your own from the distilled, vetted idea.** See `docs/VETTING.md`.

## Anti-patterns
Jumping straight to code; ignoring an existing MCP/skill; over-customizing a lib until you've lost its benefit; pulling a huge dependency for one function.
