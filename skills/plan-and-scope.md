---
skill: plan-and-scope
version: 1
ts: 2026-06-28T00:00:00Z
origin: ECC-distilled (planner + intent-driven-development)
---

# Plan + scope a non-trivial change before building

For anything beyond a one-line fix, write a short plan FIRST (to `state/plan.md`), grounded in the
real code. The point is a scoped, verifiable plan — not ceremony.

## The plan atom (one per step)
Each step states: **Action** · **Why** · **Dependencies** (which earlier step it needs) · **Risk** (Low/Med/High) · the **exact file path** it touches. A step with no file path or no done condition isn't a plan, it's a wish.

## Phasing — each phase independently shippable
Break a large feature into phases that each merge + work on their own:
1. **Minimum viable** — the smallest slice that delivers value.
2. **Core** — the happy path.
3. **Edge cases** — error handling / polish.
4. **Optimization** — perf / monitoring.
Red flag: a plan where nothing works until every phase lands. Re-cut it.

## Acceptance criteria must be OBSERVABLE
Write each criterion as: starting condition + trigger + expected outcome + a named verification method. Ban "works correctly", "is secure", "is fast" — replace with a measurable scenario or mark it as needing human judgment. If you can't say how you'd check it, it's not a criterion yet.

## The hard wall: discovered facts vs supplied constraints
- **Discovered facts** = what you read from the repo (how the system *behaves*). Cite file:line.
- **Business/product constraints** = pricing, quotas, SLAs, retention policy, compliance, target users, prioritization. **These CANNOT be read from code.** Never infer them from the repo — capture them from the operator or an authoritative doc, and record as *assumptions flagged for confirmation*, never as discovered facts.

## Scope discipline
Inspect the repo before asking anything discoverable. Ask only the un-inferable, scope-changing questions. Don't block on questions whose answers don't change the build. If scope balloons past ~5 files, stop and flag it — that's the #1 sign the plan was wrong.
