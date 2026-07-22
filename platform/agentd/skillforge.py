#!/usr/bin/env python3
"""
skillforge.py — turn a RECURRING task into a SKILL, and hold that skill to evidence.

THE GAP THIS CLOSES
Across 4 deployed agents, 486 scored ticks produced 4 memory/skill writes (0.8%). Two pods held
only the seeded starter pack, every file at `version: 1`, untouched since the day it was seeded.
The framework had `memory.py learn` and a recall path that reloads skills — the machinery was fine.
Nothing ever told an agent *"you have now done this same thing five times; write it down."* So the
loop existed and never ran.

This is the missing half. It is deterministic and zero-LLM: it reads the agent's OWN work log and
scorecard, finds task shapes that repeat, and emits a proposal. The agent (or an operator) decides.
Nothing here writes a skill by itself — a proposal is a prompt, not an action.

  RECUR (detect)  ->  PROPOSE (name it)  ->  learn --gate (memory.py)  ->  VALIDATE (score it)

The validation half lives in memory.py (`_validation_gate`): a skill may declare `validate:` in its
frontmatter and a revision is REFUSED if the held-out score drops, with a rejected-edit buffer so a
losing edit cannot be re-proposed next tick. Re-authored from microsoft/SkillOpt's one transferable
idea; the package itself was rejected (its Sleep engine ships raw transcripts to an optimizer model
and ours carry .secrets paths). See D-117 / D-117a / D-119.

WHAT COUNTS AS "THE SAME TASK"
Not string equality — work ids carry dates and specifics ("crewai-cost-audit-source-verified-
2026-07-22"). The signature is the id/title with numbers, dates, and the project noun stripped, then
reduced to its stable head terms. `langchain-cost-audit-source-verified` and
`crewai-cost-audit-source-verified` collapse to `cost-audit-source-verified`, which is the skill.

CLI
  skillforge.py <agent-dir> recur   [--min 3] [--json]     # what repeats, with evidence
  skillforge.py <agent-dir> propose [--min 3]              # markdown proposals, ready to edit
  skillforge.py <agent-dir> nudge   [--min 3]              # <=6 lines for the tick digest
  skillforge.py --selftest
"""
import argparse
import collections
import json
import pathlib
import re
import sys

# Words that carry no task identity: dates, versions, and the churn of status prose.
STOP = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "at", "by", "with", "from",
    "done", "wip", "fix", "fixed", "update", "updated", "new", "add", "added", "final",
    "again", "more", "now", "then", "this", "that", "it", "is", "was", "be", "not", "no",
    "task", "work", "item", "tick", "day", "today", "session", "part", "step", "v", "vs",
}
DATE_RE = re.compile(r"\b(20\d{2}[-/]?\d{0,2}[-/]?\d{0,2}|\d{4}z|\d{1,2}h\d{2})\b", re.I)
NUM_RE = re.compile(r"\b[a-z]*\d[\w.]*\b", re.I)


def terms(text):
    """Content words of a task id/title, dates and version numbers removed."""
    t = DATE_RE.sub(" ", (text or "").lower())
    t = NUM_RE.sub(" ", t)
    return {w for w in re.split(r"[^a-z]+", t) if len(w) > 2 and w not in STOP}


def signature(text, head=4):
    """Single-string signature. Order-insensitive by construction (terms are sorted), so
    'verify-source-crewai' and 'crewai-source-verify' cannot become two different tasks."""
    words = sorted(terms(text), key=lambda w: (-len(w), w))[:head]
    return "-".join(sorted(words))


def corpus_signatures(texts, head=4):
    """Signatures computed ACROSS the corpus — the only version that actually groups.

    A per-string signature cannot work here: 'langchain-cost-audit-source-verified' and
    'crewai-cost-audit-source-verified' are the same job, but the varying project noun is one of
    the longest words in each, so a fixed head-term rule keeps it and splits one task into three
    (this failed the selftest before it ever touched a pod). The fix is to let the corpus say what
    varies: keep only terms that appear in MORE THAN ONE item. Terms unique to a single item —
    langchain, crewai, autogen — are the task's *arguments*, not its identity, and drop out."""
    tokenized = [terms(t) for t in texts]
    df = collections.Counter()
    for ts in tokenized:
        df.update(ts)
    sigs = []
    for ts in tokenized:
        shared = {w for w in ts if df[w] > 1}
        # nothing shared -> genuinely a one-off; fall back to its own terms so it never
        # collides with another one-off
        pool = shared or ts
        sigs.append("-".join(sorted(sorted(pool, key=lambda w: (-len(w), w))[:head])))
    return sigs


def _load_work(agent_dir):
    """[(id, title)] from work.json — the agent's own record of what it did."""
    f = pathlib.Path(agent_dir) / "work.json"
    try:
        d = json.loads(f.read_text())
    except Exception:
        return []
    items = d if isinstance(d, list) else (d.get("items") or [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        out.append((str(it.get("id") or ""), str(it.get("title") or "")))
    return out


def _load_products(agent_dir, limit=400):
    """Product paths from tick-scorecard.jsonl — externally computed, the pod cannot fake it."""
    f = pathlib.Path(agent_dir) / "state" / "tick-scorecard.jsonl"
    out = []
    try:
        for ln in f.read_text(errors="replace").splitlines()[-limit:]:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            for p in (r.get("product_paths") or []):
                out.append(str(p))
    except Exception:
        pass
    return out


def _existing_skills(agent_dir):
    d = pathlib.Path(agent_dir) / "skills"
    if not d.is_dir():
        return {}
    out = {}
    for f in sorted(d.glob("*.md")):
        if f.name == "INDEX.md":
            continue
        m = re.search(r"(?m)^#\s+(.+)$", f.read_text(errors="replace"))
        out[f.stem] = (m.group(1).strip() if m else f.stem)
    return out


def recur(agent_dir, min_count=3):
    """Task shapes that repeat, with the evidence and whether a skill already covers them."""
    work = _load_work(agent_dir)
    labels = [(wid or title[:70]) for wid, title in work]
    groups = collections.defaultdict(list)
    for sig, label in zip(corpus_signatures(labels), labels):
        if sig:
            groups[sig].append(label)

    # a product path repeated across ticks is corroboration the work really recurred
    prod = collections.Counter()
    for p in _load_products(agent_dir):
        stem = pathlib.Path(p).stem
        s = signature(stem)
        if s:
            prod[s] += 1

    skills = _existing_skills(agent_dir)
    skill_sigs = {signature(s + " " + t): s for s, t in skills.items()}

    rows = []
    for sig, ids in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(ids) < min_count:
            continue
        rows.append({
            "signature": sig,
            "count": len(ids),
            "product_ticks": prod.get(sig, 0),
            "examples": ids[:5],
            "covered_by": skill_sigs.get(sig),
        })
    return rows


def propose(agent_dir, min_count=3):
    out = []
    for r in recur(agent_dir, min_count):
        if r["covered_by"]:
            continue
        slug = r["signature"]
        ex = "\n".join(f"  - {e}" for e in r["examples"])
        out.append(
            f"### Proposed skill: `{slug}`\n"
            f"You have done this **{r['count']}×**"
            + (f" (product writes in {r['product_ticks']} ticks)" if r["product_ticks"] else "")
            + ".\n\nEvidence:\n" + ex + "\n\n"
            f"Write it once so the next run is not re-derived:\n"
            f"```\nbin/memory.py --base . learn {slug} \"<one-line title>\" "
            f"--body-file <steps.md> --gate\n```\n"
            "Give it a `validate:` command in the frontmatter if the task has a checkable "
            "outcome — then a future revision is refused if the measured result drops.\n"
        )
    return out


def nudge(agent_dir, min_count=3):
    """<=6 lines for state/recall.md. Silent when there is nothing to say."""
    rows = [r for r in recur(agent_dir, min_count) if not r["covered_by"]]
    if not rows:
        return ""
    lines = ["## Recurring work with no skill — write one (you are re-deriving this every time)"]
    for r in rows[:3]:
        lines.append(f"- **{r['signature']}** — done {r['count']}×; "
                     f"`bin/memory.py --base . learn {r['signature']} \"...\" --gate`")
    return "\n".join(lines) + "\n"


def _selftest():
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as d:
        base = pathlib.Path(d)
        (base / "state").mkdir()
        work = [
            {"id": "langchain-cost-audit-source-verified-2026-07-19", "title": "DONE: verified"},
            {"id": "autogen-cost-audit-source-verified-2026-07-22", "title": "DONE: verified"},
            {"id": "crewai-cost-audit-source-verified-2026-07-22", "title": "DONE: verified"},
            {"id": "some-unrelated-one-off-2026-07-01", "title": "a thing"},
        ]
        (base / "work.json").write_text(json.dumps(work))
        rows = recur(base, min_count=3)
        if not rows:
            fails.append("did not detect the 3x recurring cost-audit task")
        else:
            if rows[0]["count"] != 3:
                fails.append(f"wrong count: {rows[0]}")
            if "verified" not in rows[0]["signature"]:
                fails.append(f"signature lost the task identity: {rows[0]['signature']}")
        if recur(base, min_count=5):
            fails.append("min_count not honoured")
        if not propose(base, 3):
            fails.append("no proposal emitted")
        if "learn" not in nudge(base, 3):
            fails.append("nudge missing the learn command")

        # once a covering skill exists, it must stop nagging
        sk = base / "skills"; sk.mkdir()
        sig = rows[0]["signature"] if rows else "x"
        (sk / f"{sig}.md").write_text(f"---\nskill: {sig}\n---\n\n# {sig.replace('-',' ')}\n\nsteps\n")
        if nudge(base, 3):
            fails.append("still nudging after a covering skill exists")

        # word ORDER must not split one task into two
        if signature("verify-source-crewai") != signature("crewai-source-verify"):
            fails.append("signature is order-sensitive")
        # dates and versions must not create identity
        if signature("cost-audit-2026-07-22") != signature("cost-audit-2026-06-01"):
            fails.append("dates leak into the signature")

    for f in fails:
        print("FAIL", f)
    print(f"{8 - len(fails)}/8 selftest checks passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("agent_dir", nargs="?")
    ap.add_argument("cmd", nargs="?", choices=["recur", "propose", "nudge"], default="recur")
    ap.add_argument("--min", type=int, default=3)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    if not a.agent_dir:
        ap.error("agent_dir is required")

    if a.cmd == "recur":
        rows = recur(a.agent_dir, a.min)
        if a.json:
            print(json.dumps(rows, indent=1))
        elif not rows:
            print(f"no task repeated >= {a.min}x")
        else:
            for r in rows:
                mark = f"covered by `{r['covered_by']}`" if r["covered_by"] else "NO SKILL"
                print(f"{r['count']:>3}x  {r['signature']:<40} {mark}")
                for e in r["examples"]:
                    print(f"        {e}")
    elif a.cmd == "propose":
        out = propose(a.agent_dir, a.min)
        print("\n".join(out) if out else "nothing to propose — recurring work is covered")
    else:
        sys.stdout.write(nudge(a.agent_dir, a.min))
    sys.exit(0)


if __name__ == "__main__":
    main()
