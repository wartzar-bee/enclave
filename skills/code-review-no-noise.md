---
skill: code-review-no-noise
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (code-reviewer agent)
---

# Review code without the noise — confidence-gated, zero-findings-OK

Whether reviewing your own diff or another agent's, the goal is real findings only. Manufactured nits,
speculative "consider using X", and hypothetical edge cases with no trigger are the primary failure
mode of LLM reviewers — and they erode trust faster than a missed finding.

## Process
Read the diff (`git diff` / staged), then **read the surrounding code** — full file, imports, callers, tests. Never review a hunk in isolation. Report only findings you're **>80% confident** are real. Consolidate similar issues ("5 handlers miss error handling", not 5 findings).

## Pre-report gate — 4 questions; any "no/unsure" → downgrade or drop
1. Can I cite the exact **file:line**?
2. Can I describe the concrete **failure mode** — the input, state, and bad outcome?
3. Have I read the surrounding context (callers / imports / tests)?
4. Is the severity **defensible**? (A missing docstring is never HIGH. One `any` in a test fixture is never CRITICAL.)

**HIGH/CRITICAL require all three:** exact snippet+line, a specific failure scenario, and why existing guards (types/validation/framework defaults) don't already catch it. Can't produce all three → demote to MEDIUM or drop.

## Zero findings is a valid review
A clean diff gets approved. Don't manufacture findings to look rigorous; don't withhold approval to seem thorough.

## Known LLM false positives — skip these
- "add error handling" when a caller/framework/`.catch`/error-boundary already handles it (trace one caller first).
- "missing input validation" on internal funcs whose callers validate.
- "magic number" for well-known constants (200/404/1024/-1/HTTP codes).
- "function too long" for exhaustive switches / config tables.
- "possible null deref" when a guard already narrows the type (trace the type flow, don't pattern-match `?.`).
- "missing await" on intentional fire-and-forget (`void`/comment present).
- "hardcoded value" in test fixtures; security theater (`Math.random()` in non-crypto contexts).

**Litmus test:** would a senior engineer on this team actually change this in review? If no — skip it.

## Output
Per finding: `[SEVERITY] title · file:line · the failure · the fix` + a BAD/GOOD snippet. End with a counts summary and a verdict: **Approve** (no CRITICAL/HIGH, incl. zero findings) · **Warning** (HIGH only, merge with caution) · **Block** (any CRITICAL).
