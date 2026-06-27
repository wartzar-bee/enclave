---
skill: verify-before-done
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (verification-loop + tdd-workflow)
---

# Verify before "done" — a fixed gate, not a self-claim

"Done" is a thing you PROVE, never a thing you assert. Before marking any work item done, run the
gate in order and STOP at the first hard failure — fixing it before going further.

(A PostToolUse `verify_edit` hook already auto-catches syntax/parse breakage the instant you save a
code file — if it tells you an edit broke a file, fix it then. That's the cheap reflex check; THIS
gate is the deeper one — build, tests, behaviour — you run before calling work done.)

## The gate (run in this order; stop on a hard fail)
1. **Build / runs** — does it actually build/start? (`<build cmd> 2>&1 | tail -20`). Build fails → STOP, fix first. Nothing downstream matters if it doesn't build.
2. **Types** — typecheck clean (`tsc --noEmit` / `pyright` / equivalent). Fix criticals first.
3. **Lint** — linter clean for changed files.
4. **Tests** — run the suite; report Passed/Failed and coverage. If you wrote a test for a fix, it must have gone RED before the fix and GREEN after (see RED-gate below) — a test that never ran RED proves nothing.
5. **Behaviour** — actually exercise the change the way a user/caller hits it (run it, render it, drive the real path, read the real output). For a UI/game: render + screenshot + read the image. For an API: call it and read the response. NOT "it should work."
6. **Diff review** — `git diff --stat` then read each changed file for unintended edits, dropped error handling, stray debug logs, leaked secrets.

## RED-gate (when a fix has a test)
A "RED" only counts if the test was **compiled AND executed** and failed *because of the bug you're fixing* — not because of a syntax error, broken setup, missing dep, or unrelated regression. A test you wrote but never ran is NOT a RED. Do not touch production code until you've seen the real RED.

## Verdict
End with one line: **READY** (all gates pass) or **NOT READY** + the numbered blockers. "Done = implemented · validated · integrated · recorded · no immediate follow-up." Zero blockers is the only done.

## If you can spawn subagents (BRAIN=claude)
Don't self-certify — spawn a verifier subagent (Task tool, e.g. the `code-reviewer` / `security-reviewer` agents) to adversarially prove it runs, item by item. You're the CEO; launch as many as needed. The verifier's evidence is the gate, not your own claim.

## Anti-fabrication
Quote the actual command and its real output. Never write a PASS for a check you didn't run, or a metric you didn't measure.
