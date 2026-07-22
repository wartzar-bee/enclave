#!/usr/bin/env python3
"""
memory.py — durable memory + self-improvement (skill persistence) for agents.

ONE substrate used by deployed agents AND the host agent itself. It implements
the closed learning loop the platform must enforce:

    RECALL (cheap, before acting)  ->  ACT  ->  WRITE-BACK (remember + learn)

Why: a scheduled agent's conversation context is wiped between ticks (and even
compacted mid-session). Without durable, cheaply-recalled memory it re-derives
the world every tick and never improves. This fixes that: state lives in files,
recall is a grep (not a re-read of everything), and the agent writes back lessons
and reusable SKILLS so the next tick is smarter than the last.

Layout under <base>/ (default: the agent dir; the host can point elsewhere):
  memory/INDEX.md            one-line pointer per memory (cheap to load each tick)
  memory/<type>/<slug>.md    ONE fact per file  (type: fact|decision|lesson|user)
  skills/INDEX.md            one-line pointer per skill
  skills/<slug>.md           a reusable PROCEDURE the agent learned (the ⭐ loop)

CLI:
  memory.py --base D remember --type lesson "text" [--tags a,b] [--slug s]
  memory.py --base D recall   "query terms" [-k 5]
  memory.py --base D learn    <slug> "Title" (--body "..." | --body-file f) [--gate]
  memory.py --base D index    [skills]
  memory.py --base D forget   <type>/<slug>
"""
import sys, os, re, json, argparse, pathlib, datetime, subprocess, math, time, hashlib

def _slug(s, n=48):
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return (s[:n] or "x").strip("-")

def _norm_stem(t):
    """Normalize a link target to a bare page stem: strip [[ ]], a .md suffix, and any dir."""
    t = t.strip().strip("[]").strip()
    if t.endswith(".md"):
        t = t[:-3]
    return t.split("/")[-1]

class Memory:
    TYPES = ("fact", "decision", "lesson", "user")
    REPO = pathlib.Path(__file__).resolve().parents[2]
    def __init__(self, base):
        self.base = pathlib.Path(base)
        self.mem = self.base / "memory"; self.skills = self.base / "skills"
        self.users = self.mem / "users"
        for d in (self.mem, self.skills, self.users): d.mkdir(parents=True, exist_ok=True)
        for idx, hdr in ((self.mem / "INDEX.md", "# Memory index\n"),
                         (self.skills / "INDEX.md", "# Skills index\n")):
            if not idx.exists(): idx.write_text(hdr)

    def _now(self): return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def remember(self, text, mtype="lesson", tags=None, slug=None, related=None):
        if mtype not in self.TYPES: mtype = "fact"
        slug = slug or _slug(text)
        d = self.mem / mtype; d.mkdir(exist_ok=True)
        f = d / f"{slug}.md"
        # `related: [[..]]` makes this fact a first-class GRAPH node — one linked brain, not a flat pile.
        # Cross-link to the knowledge/ wiki pages (or other memories) it relates to; `wiki.py graph
        # --brain` then traverses memory + knowledge + skills as one vault.
        rel = " ".join(f"[[{_norm_stem(r)}]]" for r in (related or []) if r and r.strip())
        f.write_text(f"---\ntype: {mtype}\ntags: {','.join(tags or [])}\nts: {self._now()}\n"
                     f"related: {rel}\n---\n\n{text.strip()}\n")
        line = f"- [{mtype}] {text.strip().splitlines()[0][:120]} → `{mtype}/{slug}.md`"
        idx = self.mem / "INDEX.md"; lines = idx.read_text().splitlines()
        lines = [l for l in lines if f"`{mtype}/{slug}.md`" not in l]  # de-dup
        idx.write_text("\n".join(lines + [line]) + "\n")
        return f

    def link(self, rel, targets):
        """Cross-link an existing memory into the graph: merge `[[targets]]` into its `related:`
        frontmatter (the cross-linker pattern). Targets are page stems (knowledge pages or memories)."""
        f = self.mem / rel
        if not f.suffix: f = f.with_suffix(".md")
        if not f.exists(): return None
        text = f.read_text()
        existing = set(re.findall(r"\[\[([^\]]+)\]\]", text))
        add = [s for s in (_norm_stem(t) for t in targets if t and t.strip()) if s and s not in existing]
        if not add: return f
        add_str = " ".join(f"[[{s}]]" for s in add)
        if re.search(r"(?m)^related:", text):
            text = re.sub(r"(?m)^(related:.*)$", lambda m: m.group(1).rstrip() + " " + add_str, text, count=1)
        elif text.startswith("---") and "\n---" in text:
            end = text.find("\n---", 3)
            text = text[:end] + f"\nrelated: {add_str}" + text[end:]
        else:
            text = f"related: {add_str}\n\n" + text
        f.write_text(text)
        return f

    # ── SKILL QUALITY GATE (P3, Hermes "learn-from-success" borrow) ──────────────────
    # A skill is a REUSABLE PROCEDURE, not a one-off note. Without a gate the learn-loop
    # silts the vault with thin/duplicate entries that then cost recall tokens every tick.
    # Gate = deterministic STRUCTURE checks (always run) + a local-LLM dedup/usefulness pass
    # ($0, off-cap, FAIL-OPEN: route.mjs down → ADMIT but tag `unverified`, never block on infra —
    # same report-only philosophy as the egress guard). Returns (ok, reason, verdict_tag).
    def _skill_gate(self, slug, title, body):
        b = (body or "").strip()
        # (1) substance — a procedure needs enough to be reusable, not a one-liner
        if len(b) < 120:
            return False, "too thin (<120 chars) — a skill captures a reusable procedure, not a note", None
        # (2) shape — must read as how-to (numbered/bulleted steps, or several lines), not a bare fact
        lines = [l for l in b.splitlines() if l.strip()]
        procedural = (len(lines) >= 3 or bool(re.search(r"(?m)^\s*(\d+[.)]|[-*])\s+\S", b)))
        if not procedural:
            return False, "not procedural — needs steps (numbered/bulleted) or multiple how-to lines", None
        # (3) dedup vs existing skills (local LLM; fail-open). A true twin → update it, don't fork.
        existing = []
        for sf in sorted(self.skills.glob("*.md")):
            if sf.name == "INDEX.md" or sf.stem == slug: continue
            t = sf.read_text(); m = re.search(r"(?m)^#\s+(.+)$", t)
            existing.append((sf.stem, (m.group(1).strip() if m else sf.stem)[:80]))
        if not existing:
            return True, "ok", "verified"
        listing = "\n".join(f"{i}: {nm} — {ti}" for i, (nm, ti) in enumerate(existing))
        prompt = ('A new agent SKILL is being saved. Is it a NEAR-DUPLICATE of an existing one (same '
                  'procedure)? Return ONLY JSON {"dup": <index or -1>}. -1 if genuinely new. BE '
                  f'CONSERVATIVE — only flag a true duplicate.\nNEW: {title} — {b[:300]}\nEXISTING:\n{listing}')
        try:
            r = subprocess.run(["node", str(self.REPO / "tools/llm/route.mjs"), "--task", "classify",
                                "--sensitivity", "internal"], input=prompt, capture_output=True,
                               text=True, timeout=40, cwd=str(self.REPO))
            mm = re.search(r'"dup"\s*:\s*(-?\d+)', r.stdout)
            if mm and 0 <= int(mm.group(1)) < len(existing):
                twin = existing[int(mm.group(1))][0]
                return False, f"near-duplicate of `{twin}.md` — re-learn THAT slug to re-version it instead", None
            # a clean -1 verdict ⇒ verified; no parseable verdict (router missing/garbage) ⇒ admit unverified
            return (True, "ok", "verified") if mm else (True, "ok", "unverified")
        except Exception:
            return True, "ok", "unverified"         # router unavailable → admit, mark unverified

    # ── VALIDATION GATE (borrowed, re-authored, from microsoft/SkillOpt — D-117/D-117a) ──────────
    # SkillOpt's one genuinely transferable idea: **accept a skill edit only when a held-out
    # measurement does not get worse.** Our existing _skill_gate checks SHAPE (length, bullets,
    # dedup) and never asks whether the skill helped — a skill could be rewritten into nonsense and
    # sail through. This closes that asymmetry without adopting the package (its Sleep engine ships
    # raw transcripts to an optimizer model; ours carry .secrets paths — see D-117 blocker (b)).
    #
    # HONEST SCOPE: this is a REGRESSION gate, not an optimizer. SkillOpt replays tasks with the
    # candidate skill and scores the rollouts. We do not replay; we score the ARTIFACTS the skill
    # governs. So it answers "did revising this skill coincide with the evidence getting worse?",
    # not "is this edit optimal". That is the honest claim, and it is still the thing that was
    # missing: a skill revision must now come with evidence.
    #
    # A skill opts in by carrying `validate:` in its frontmatter — a command printing a float 0..1:
    #   ---
    #   skill: source-verify-code-citation
    #   validate: python3 /workspace/tools/verify/citation_check.py --score <held-out files>
    #   score: 0.8462
    #   ---
    # No `validate:` → unchanged behaviour (shape gate only). Fail-open on an unrunnable command,
    # but recorded as `unvalidated` so it is never mistaken for a pass.
    VAL_EPS = 1e-6

    def _read_frontmatter(self, path, key):
        try:
            m = re.search(rf"(?m)^{key}:\s*(.+)$", path.read_text())
            return m.group(1).strip() if m else None
        except Exception:
            return None

    def _rejected(self):
        f = self.skills / ".rejected.json"
        try: return json.loads(f.read_text())
        except Exception: return {}

    def _reject(self, slug, body, reason, score=None):
        """Rejected-edit buffer (also SkillOpt's): remember what was refused so the same edit is not
        re-proposed next tick. Without it a losing revision loops forever at full model price."""
        f = self.skills / ".rejected.json"
        d = self._rejected()
        h = hashlib.sha256(body.strip().encode()).hexdigest()[:16]
        d.setdefault(slug, [])
        if not any(e.get("hash") == h for e in d[slug]):
            d[slug].append({"hash": h, "ts": self._now(), "reason": reason, "score": score})
            d[slug] = d[slug][-20:]
        try: f.write_text(json.dumps(d, indent=1))
        except Exception: pass

    def _validation_gate(self, slug, body):
        """(ok, reason, new_score). Runs the skill's own `validate:` command against the CURRENT
        artifacts and compares to the score recorded when the skill was last accepted."""
        f = self.skills / f"{slug}.md"
        if not f.exists():
            return True, "new skill — no prior score to regress against", None
        cmd = self._read_frontmatter(f, "validate")
        if not cmd:
            return True, "no validate: declared", None

        h = hashlib.sha256(body.strip().encode()).hexdigest()[:16]
        for e in self._rejected().get(slug, []):
            if e.get("hash") == h:
                return False, (f"this exact revision was already rejected ({e.get('reason')}) — "
                               "change the approach, do not re-submit it"), None
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300,
                               cwd=str(self.REPO))
            new = float((r.stdout or "").strip().splitlines()[-1])
        except Exception as ex:
            return True, f"validate command did not run ({type(ex).__name__}) — admitted UNVALIDATED", None

        prev = self._read_frontmatter(f, "score")
        try: old = float(prev) if prev is not None else None
        except Exception: old = None
        if old is not None and new < old - self.VAL_EPS:
            self._reject(slug, body, f"held-out score fell {old:.4f} → {new:.4f}", new)
            return False, (f"REGRESSION: held-out score fell {old:.4f} → {new:.4f}. The revision is "
                           "refused and recorded; fix the evidence or change the approach."), new
        return True, "ok", new

    def learn(self, slug, title, body, gate=False):
        slug = _slug(slug)
        verdict = None
        new_score = None
        if gate:
            ok, reason, verdict = self._skill_gate(slug, title, body)
            if not ok:
                return None, reason
            ok, reason, new_score = self._validation_gate(slug, body)
            if not ok:
                return None, reason
        f = self.skills / f"{slug}.md"
        ver = 1
        prior_validate = self._read_frontmatter(f, "validate") if f.exists() else None
        if f.exists():
            m = re.search(r"version:\s*(\d+)", f.read_text()); ver = int(m.group(1)) + 1 if m else 2
        gline = f"gated: {verdict}\n" if verdict else ""
        # carry `validate:` forward (a revision must not silently drop its own gate) and stamp the
        # score this version was accepted at — that becomes the bar the NEXT revision must clear.
        vline = ""
        if prior_validate and "validate:" not in body:
            vline += f"validate: {prior_validate}\n"
        if new_score is not None:
            vline += f"score: {new_score:.4f}\n"
        f.write_text(f"---\nskill: {slug}\nversion: {ver}\nts: {self._now()}\n{gline}{vline}---\n\n# {title}\n\n{body.strip()}\n")
        line = f"- **{title}** → `{slug}.md` (v{ver})"
        idx = self.skills / "INDEX.md"; lines = idx.read_text().splitlines()
        lines = [l for l in lines if f"`{slug}.md`" not in l]
        idx.write_text("\n".join(lines + [line]) + "\n")
        return f, ver

    def _rank_skills(self, query, k=2):
        """Keyword-rank learned skills for the focus query — closes the WRITE→RELOAD loop so a skill
        saved last tick is RECALLED this tick when relevant (persistence alone left this half open:
        skills/ snapshots fine but never re-entered the tick context)."""
        terms = [t for t in re.findall(r"[a-z0-9]+", (query or "").lower()) if len(t) > 2]
        if not terms: return []
        out = []
        for f in sorted(self.skills.glob("*.md")):
            if f.name == "INDEX.md": continue
            txt = f.read_text()
            m = re.search(r"(?m)^#\s+(.+)$", txt); title = (m.group(1).strip() if m else f.stem)
            sc = sum(txt.lower().count(t) for t in terms)
            if sc: out.append((sc, f.stem, title))
        return sorted(out, key=lambda x: -x[0])[:k]

    def _entries(self):
        out = []
        for f in self.mem.rglob("*.md"):
            if f.name == "INDEX.md": continue
            if f.parent.name == "activity": continue        # auto-captured tick log — qmd-searchable, not ranked in recall
            txt = f.read_text(); body = txt.split("---", 2)[-1].strip()
            out.append((f, body, txt))
        return out

    def _kw_rank(self, query, entries):
        terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        scored = [(sum(txt.lower().count(t) for t in terms), f, body) for f, body, txt in entries]
        return sorted([s for s in scored if s[0]], key=lambda x: -x[0])

    # ── REINFORCEMENT + DECAY (jcode roadmap #2): used memories rise, stale ones fade. Lifecycle
    #    (access_count, last_used) lives in a SIDECAR so the agents' memory markdown is never rewritten.
    HALF_LIFE = {"correction": 365, "preference": 90, "decision": 120, "procedure": 60, "lesson": 45,
                 "fact": 30, "inferred": 7}

    def _ls(self):
        try: return json.loads((self.mem / ".lifecycle.json").read_text())
        except Exception: return {}
    def _ls_save(self, d):
        try: (self.mem / ".lifecycle.json").write_text(json.dumps(d))
        except Exception: pass
    def _age_days(self, f):
        try:
            m = re.search(r"\bts:\s*(\S+)", f.read_text())
            t = datetime.datetime.strptime(m.group(1)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
            return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 86400.0)
        except Exception:
            return 0.0
    def _life_weight(self, f, lc):
        """Multiplier on a memory's rank: confidence DECAYS by per-type half-life, boosted by
        access_count and recency-of-last-use. Floor 0.05 (never fully buried)."""
        rel = str(f.relative_to(self.mem)); meta = lc.get(rel, {}); acc = meta.get("acc", 0)
        decay = math.exp(-self._age_days(f) / self.HALF_LIFE.get(f.parent.name, 45)) * (1 + 0.1 * math.log(acc + 1))
        used = meta.get("used")
        rec = (1.0 + 0.5 * math.exp(-(time.time() - used) / 86400.0)) if used else 1.0
        return max(0.05, decay) * rec

    def _semantic_rerank(self, query, cands, k):
        """Rerank candidate bodies by MEANING via the local LLM router ($0, off-cap,
        private). Returns indices into `cands`, or None on any failure (→ keyword)."""
        listing = "\n".join(f"{i}) {b[:220]}" for i, (_, b, _) in enumerate(cands))
        prompt = (f"You are a memory retrieval reranker. Return ONLY a JSON array of the candidate "
                  f"numbers most RELEVANT IN MEANING to the query (best first, max {k}), e.g. [3,0,7].\n"
                  f"QUERY: {query}\nCANDIDATES:\n{listing}")
        try:
            r = subprocess.run(["node", str(self.REPO / "tools/llm/route.mjs"), "--task", "classify",
                                "--sensitivity", "internal"], input=prompt, capture_output=True,
                               text=True, timeout=40, cwd=str(self.REPO))
            m = re.search(r"\[[\d,\s]*\]", r.stdout)
            return [i for i in json.loads(m.group(0)) if isinstance(i, int)] if m else None
        except Exception:
            return None

    def recall(self, query, k=5, semantic=False):
        entries = self._entries()
        if not entries: return []
        ranked = self._kw_rank(query, entries)
        lc = self._ls()                                    # reinforcement+decay: re-weight by lifecycle
        ranked = sorted(((sc * self._life_weight(f, lc), f, b) for sc, f, b in ranked), key=lambda x: -x[0])
        if not semantic:
            picks = [(f, b) for _, f, b in ranked[:k]]
        else:
            # prefilter to a sane pool (keyword hits, else everything), then rerank by meaning
            pool = [(f, b) for _, f, b in ranked][:20] or [(f, b) for f, b, _ in entries][:20]
            cands = [(f, b, "") for f, b in pool]
            order = self._semantic_rerank(query, cands, k)
            picks = ([pool[i] for i in order if i < len(pool)][:k]) if order else [(f, b) for _, f, b in ranked[:k]]
        now = time.time()                                  # REINFORCE: surfaced ⇒ used ⇒ bump (jcode on_used)
        for f, _ in picks:
            m = lc.setdefault(str(f.relative_to(self.mem)), {}); m["acc"] = m.get("acc", 0) + 1; m["used"] = now
        self._ls_save(lc)
        return [f"[{f.parent.name}/{f.stem}] {b[:160]}" for f, b in picks]

    # ── COMPACTION (P3.6): scheduled lean-up so continuous ticks don't bloat the store / qmd index.
    def _llm_summarize(self, raw, target="a compact bulleted digest of what was done (keep dates, "
                                          "decisions, outcomes; drop noise)"):
        """Summarize via the LOCAL LLM router ($0/off-cap). None on failure (caller keeps raw)."""
        try:
            r = subprocess.run(["node", str(self.REPO / "tools/llm/route.mjs"), "--task", "write",
                                "--sensitivity", "internal"],
                               input=f"Compress the following activity log into {target}. Be terse.\n\n{raw[:24000]}",
                               capture_output=True, text=True, timeout=90, cwd=str(self.REPO))
            out = (r.stdout or "").strip()
            return out if len(out) > 40 else None
        except Exception:
            return None

    def compact(self, keep_days=14, month_cap=30000):
        """Daily housekeeping: (1) roll old activity captures into monthly archives, (2) CONSOLIDATE
        lessons (dedup + supersede). Always runs both. Safe: archives, never silently drops content."""
        return "compact: " + self._roll_activity(keep_days, month_cap) + "; " + self._consolidate()

    def _roll_activity(self, keep_days, month_cap):
        actdir = self.mem / "activity"
        if not actdir.is_dir():
            return "no activity/"
        cutoff = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=keep_days)
        by_month = {}
        for f in sorted(actdir.glob("20*-*-*.md")):
            try:
                d = datetime.date.fromisoformat(f.stem)
            except Exception:
                continue
            if d >= cutoff:
                continue
            by_month.setdefault(d.strftime("%Y-%m"), []).append(f)
        if not by_month:
            return "nothing older than %dd" % keep_days
        arc = actdir / "_archive"; arc.mkdir(exist_ok=True)
        rolled = 0
        for month, files in by_month.items():
            af = arc / f"{month}.md"
            prior = af.read_text() if af.exists() else f"# Activity archive {month} (compacted)\n"
            raw = prior + "\n\n" + "\n\n".join(f"## {f.stem}\n{f.read_text()}" for f in files)
            if len(raw) > month_cap:
                raw = f"# Activity archive {month} (compacted, summarized)\n\n" + (self._llm_summarize(raw) or raw[:month_cap])
            af.write_text(raw)
            for f in files:
                f.unlink(); rolled += 1
        return f"rolled {rolled} daily capture(s) into {len(by_month)} monthly archive(s)"

    def _consolidate(self, max_action=6):
        """CONSOLIDATION (jcode roadmap #3): dedup near-duplicate LESSONS + supersede contradictions via
        ONE local-LLM pass ($0, off-cap). ARCHIVES (never hard-deletes) the redundant/superseded to
        memory/_archive/consolidated/, folds their access into the survivor, drops them from INDEX.
        Conservative + bounded (<= max_action)."""
        ld = self.mem / "lesson"
        files = sorted(ld.glob("*.md")) if ld.is_dir() else []
        if len(files) < 4:
            return "consolidate: <4 lessons, skip"
        items = []
        for i, f in enumerate(files):
            body = f.read_text().split("---", 2)[-1].strip().splitlines()
            items.append((i, f, (body[0] if body else "")[:160]))
        listing = "\n".join(f"{i}: {one}" for i, _, one in items)
        prompt = ('Dedup an agent lesson-memory. Return ONLY JSON {"dups":[[i,j,..]],"contradicts":[[old_i,new_j]]}. '
                  '"dups"=groups stating the SAME thing (near-identical); "contradicts"=pairs where one OVERTURNS '
                  f'the other (newer wins). Empty arrays if none. BE CONSERVATIVE.\nLESSONS:\n{listing}')
        try:
            r = subprocess.run(["node", str(self.REPO / "tools/llm/route.mjs"), "--task", "classify",
                                "--sensitivity", "internal"], input=prompt, capture_output=True,
                               text=True, timeout=90, cwd=str(self.REPO))
            mm = re.search(r"\{.*\}", r.stdout, re.S)
            plan = json.loads(mm.group(0)) if mm else {}
        except Exception:
            return "consolidate: LLM unavailable, skip"
        lc = self._ls(); arc = self.mem / "_archive" / "consolidated"; archived = []
        byidx = {i: f for i, f, _ in items}
        def acc(f): return lc.get(str(f.relative_to(self.mem)), {}).get("acc", 0)
        def merge(victim, survivor):
            if victim == survivor or not victim.exists() or len(archived) >= max_action: return
            sm = lc.setdefault(str(survivor.relative_to(self.mem)), {})
            sm["acc"] = sm.get("acc", 0) + acc(victim) + 1                 # fold access into survivor (reinforce)
            arc.mkdir(parents=True, exist_ok=True)
            victim.rename(arc / victim.name); lc.pop(str(victim.relative_to(self.mem)), None)
            archived.append(victim.name)
        for grp in (plan.get("dups") or []):
            g = [byidx[i] for i in grp if isinstance(i, int) and i in byidx and byidx[i].exists()]
            if len(g) < 2: continue
            survivor = max(g, key=acc)                                    # keep the most-used
            for v in g: merge(v, survivor)
        for pair in (plan.get("contradicts") or []):
            if isinstance(pair, list) and len(pair) == 2 and pair[0] in byidx and pair[1] in byidx:
                merge(byidx[pair[0]], byidx[pair[1]])                     # older superseded by newer
        self._ls_save(lc)
        if archived:                                                      # drop archived lessons from INDEX
            idx = self.mem / "INDEX.md"
            if idx.exists():
                keep = [l for l in idx.read_text().splitlines() if not any(f"lesson/{n}" in l for n in archived)]
                idx.write_text("\n".join(keep) + "\n")
        return f"consolidate: archived {len(archived)} redundant/superseded" if archived else "consolidate: 0 redundant"

    # ── USER memory (per-entity modeling — for support / relationship agents) ──
    def user_note(self, uid, note):
        f = self.users / f"{_slug(uid)}.md"
        if not f.exists(): f.write_text(f"# user: {uid}\n\n")
        with f.open("a") as h: h.write(f"- {self._now()} — {note.strip()}\n")
        return f

    def user_get(self, uid):
        f = self.users / f"{_slug(uid)}.md"
        return f.read_text() if f.exists() else f"(no record for user '{uid}')"

    # ── WORK QUEUE (continuity across ticks — the "what am I in the middle of") ──
    def _workf(self): return self.base / "work.json"
    def work_list(self):
        # Tolerate a non-conforming work.json: only a LIST of dict items is a queue. An agent that
        # tracks work its own way (e.g. a free-form dict) yields an empty queue here instead of
        # crashing the digest (which would leave the tick with NO recall.md and force a full re-read).
        if not self._workf().exists():
            return []
        try:
            d = json.loads(self._workf().read_text())
        except Exception:
            return []
        return [i for i in d if isinstance(i, dict) and "status" in i] if isinstance(d, list) else []
    def work_add(self, text, verify=""):
        w = self.work_list(); wid = max([i["id"] for i in w], default=0) + 1
        w.append({"id": wid, "text": text, "status": "todo", "ts": self._now(), "evidence": "", "verify": verify})
        self._workf().write_text(json.dumps(w, indent=2) + "\n"); return wid
    def _run_verify(self, cmd):
        """Run an item's verify command (cwd=base). Returns (ok, output). Deterministic completion gate
        so the model can't self-mark 'done' without the work actually being verifiable (anti-fabrication)."""
        import subprocess
        try:
            p = subprocess.run(["bash", "-lc", cmd], cwd=str(self.base), capture_output=True, text=True, timeout=180)
            return p.returncode == 0, (p.stdout + p.stderr)[-500:]
        except Exception as e:
            return False, str(e)
    def work_set(self, wid, status, evidence=""):
        w = self.work_list()
        for i in w:
            if i["id"] == int(wid):
                # VERIFY GATE: 'done' requires the item's verify command to pass (if one is set).
                if status == "done" and i.get("verify"):
                    ok, out = self._run_verify(i["verify"])
                    if not ok:
                        return {"ok": False, "id": i["id"], "reason": "verify FAILED — not marked done", "output": out}
                    evidence = (evidence + f" | verify PASSED: {i['verify']}").strip(" |")
                i["status"] = status; i["ts"] = self._now()
                if evidence: i["evidence"] = evidence
        self._workf().write_text(json.dumps(w, indent=2) + "\n")
        return {"ok": True, "id": int(wid), "status": status}

    def _directive_state(self):
        """(active, retracted) from state/directives.json — the compiled directive STATE
        (see directives.py: the revocation primitive). Self-contained load (no import) so a
        per-agent copy of memory.py works without directives.py beside it. ([], []) if the
        file is absent/unparseable — callers fall back to the inbox."""
        f = self.base / "state" / "directives.json"
        if not f.exists():
            return [], []
        try:
            items = json.loads(f.read_text()).get("directives", [])
            if not isinstance(items, list): return [], []
        except Exception:
            return [], []
        items = [x for x in items if isinstance(x, dict) and str(x.get("text", "")).strip()]
        acts = sorted([x for x in items if x.get("status") == "active"],
                      key=lambda x: (x.get("priority", 50), x.get("id", "")))
        retr = [x for x in items if x.get("status") == "retracted"]
        return acts, retr

    def _auto_query(self):
        """Derive a recall focus from the agent's current state: ACTIVE directives (compiled
        state — a retracted/superseded order must not anchor recall) + the latest rollup line.
        Falls back to open inbox items only when no directives.json exists."""
        parts = []
        acts, _ = self._directive_state()
        if acts:
            parts += [x["text"][:160] for x in acts]
        else:
            ib = self.base / "inbox.md"
            if ib.exists():
                parts += [ln.strip()[5:].strip() for ln in ib.read_text().splitlines() if ln.strip().startswith("- [ ]")]
        rl = self.base / "state" / "rollup.md"
        if rl.exists():
            body = [l for l in rl.read_text().splitlines() if l.strip() and not l.strip().startswith("#")]
            if body: parts.append(body[0])
        return " ".join(parts)[:500]

    def _qmd_query(self, query, k=4, timeout=12):
        """PASSIVE SEMANTIC recall via the host qmd MCP (vector search — embeddings, $0, fast).
        Returns [(pct, filename, snippet)] filtered to THIS agent's memory + shared knowledge/.
        [] on any failure (caller keeps keyword recall). This is the embeddings-backed upgrade to
        the digest — relevant memory + knowledge by MEANING, not keyword (jcode cascade-retrieval idea)."""
        import urllib.request
        url = os.environ.get("QMD_URL", "http://host.docker.internal:18181/mcp")
        aid = os.environ.get("AGENT_ID", "")
        def _post(method, params, sid=None):
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
            h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            if sid: h["Mcp-Session-Id"] = sid
            r = urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout)
            raw = r.read().decode()
            if "data:" in raw[:24]:
                for ln in raw.splitlines():
                    if ln.startswith("data:"): raw = ln[5:].strip(); break
            return r.headers.get("Mcp-Session-Id"), (json.loads(raw) if raw.strip() else {})
        try:
            sid, _ = _post("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                          "clientInfo": {"name": "digest", "version": "1"}})
            try: _post("notifications/initialized", {}, sid)
            except Exception: pass
            _, res = _post("tools/call", {"name": "query", "arguments": {
                "intent": f"pre-tick recall for {aid}", "searches": [{"type": "vec", "query": query[:300]}]}}, sid)
            text = res.get("result", {}).get("content", [{}])[0].get("text", "")
        except Exception:
            return []
        hits = []
        for ln in text.splitlines():
            m = re.match(r"#\w+\s+(\d+)%\s+(\S+)\s+-\s+(.*)", ln.strip())
            if not m: continue
            pct, path, snip = int(m.group(1)), m.group(2), m.group(3)
            if pct < 35: continue
            # KNOWLEDGE-type files only (lessons/skills/shared knowledge) from ANY source — surfaces
            # peers' learnings too (network compounding). Excludes operational state/content/tmp/reports.
            if ("/memory/" in path) or ("/skills/" in path) or ("/knowledge/" in path):
                hits.append((pct, path.split("/")[-1], snip[:120]))
        return hits[:k]

    def digest(self, query=None, k=3):
        """Pre-tick recall digest (P3): the agent's OPEN WORK + the most relevant past memory for
        the current focus, so a context-wiped tick doesn't re-derive the world. Keyword-ranked over
        own memory + PASSIVE SEMANTIC recall via qmd (relevant memory + shared knowledge by meaning).
        Kept deliberately LEAN — it's built + read EVERY tick, so size is a recurring token cost
        (see docs/CONTEXT-AND-TICKS.md). The agent recalls more on demand via qmd only if needed."""
        work = [i for i in self.work_list() if i["status"] not in ("done", "dropped")]
        q = query if query is not None else self._auto_query()
        hits = self.recall(q, k=k, semantic=False) if q else []
        # Directives OVERRIDE the work queue — surface them at the very top so the loop's momentum
        # (a 'doing' work item) can't bury a pause/redirect. Preferred source = the COMPILED state
        # (state/directives.json, active entries IN FULL, priority order — the revocation primitive).
        # Fallback = ALL open inbox items. The old `\bboard\b|via comms|BOARD` keyword filter is gone:
        # it silently dropped every directive that didn't happen to contain those tokens (all 3 of
        # scoutpod's [tier:top] orders, verified 2026-07-19).
        directives = []; retracted = []
        acts, retr = self._directive_state()
        if acts:
            directives = [f"**{x.get('id','?')}** — {x['text'].strip()}" for x in acts[:8]]
            retracted = [f"{x.get('id','?')}: {x['text'].strip()[:200]}" for x in retr[:4]]
        else:
            _seen = set()
            ib = self.base / "inbox.md"
            if ib.exists():
                for ln in ib.read_text().splitlines():
                    s = ln.strip()
                    if s.startswith("- [ ]"):
                        d = s[5:].strip()[:280]
                        # dedup by the directive BODY (drop the leading "<ts> via comms (src): " prefix) so a
                        # recurring cadence directive fired on many days collapses to one line.
                        body = d.split("): ", 1)[-1]
                        key = re.sub(r"\s+", " ", body.lower())[:80]
                        if key in _seen: continue
                        _seen.add(key); directives.append(d)
        out = ["# Recall — auto-loaded for this tick (read FIRST, alongside inbox.md)\n"]
        # MEASURED PERFORMANCE (analytics plan P0): the pod's own externally-computed deficiencies,
        # ≤4 lines, from state/tick-scorecard.jsonl (written by the harness post-tick — the pod
        # reads its numbers, it never computes them). Self-contained import-free reader would
        # duplicate scorecard.summary; a subprocess call keeps ONE implementation.
        try:
            import subprocess as _sp
            _sc = pathlib.Path(__file__).resolve().parent / "scorecard.py"
            if _sc.exists() and (self.base / "state" / "tick-scorecard.jsonl").exists():
                _p = _sp.run(["python3", str(_sc), str(self.base), "summary"],
                             capture_output=True, text=True, timeout=10)
                _perf = [l for l in _p.stdout.splitlines() if l.strip()]
                if _perf:
                    out.append("## 📉 MEASURED PERFORMANCE (computed OUTSIDE you — fix the worst "
                               "line before adding anything new)\n" + "\n".join(_perf) + "\n")
        except Exception:
            pass
        if directives:
            out.append("## ⚠ DIRECTIVES — the authoritative instruction state, in priority order "
                       "(act on these FIRST; they OVERRIDE your work queue)\n" +
                       "\n".join(f"- {d}" for d in directives[:8]) + "\n")
        if retracted:
            out.append("## ✕ RETRACTED — never act on or cite these again\n" +
                       "\n".join(f"- {d}" for d in retracted) + "\n")
        # Open work: show 'doing' first (in full-ish), then todos as truncated one-liners, capped — the
        # full verbose queue is the bulk of the digest and is re-read every tick. The agent can `work
        # list` for the complete queue if it needs it; this is the at-a-glance focus set.
        WORK_CAP = 12
        doing_items = [i for i in work if i.get("status") == "doing"]
        todo_items = [i for i in work if i.get("status") != "doing"]
        ordered = doing_items + todo_items
        work_lines = [f"- #{i['id']} [{i['status']}] {i['text'][:140]}" for i in ordered[:WORK_CAP]]
        if len(ordered) > WORK_CAP:
            work_lines.append(f"- …+{len(ordered)-WORK_CAP} more open (run `memory.py work list` for the full queue)")
        out.append("## Open work (continue 'doing' before starting new)\n" +
                   ("\n".join(work_lines) if work_lines else "- (none)"))
        out.append("\n## Relevant past memory (keyword)\n" +
                   ("\n".join(f"- {h}" for h in hits) if hits else "- (none yet)"))
        # query qmd on the single FOCUS task (top 'doing', else top item) — a focused vector matches
        # distilled lessons/knowledge better than a verbose directive or a diluted multi-task concat.
        doing = [i for i in work if i["status"] == "doing"] or work
        sem_q = (doing[0]["text"][:240]) if doing else q
        sem = self._qmd_query(sem_q) if sem_q else []           # passive SEMANTIC recall (qmd embeddings)
        if sem:
            out.append("\n## Relevant by MEANING (semantic — your memory + shared knowledge)\n" +
                       "\n".join(f"- ({p}%) {fn}: {snip}" for p, fn, snip in sem))
        # Learned skills for the focus — closes the write→reload loop (Hermes borrow): a procedure the
        # agent learned earlier comes BACK when it's relevant, instead of being re-derived from scratch.
        skills = self._rank_skills(sem_q or q, k=2)
        if skills:
            out.append("\n## Learned skills — reusable procedures (recall, don't re-derive)\n" +
                       "\n".join(f"- **{ti}** → `skills/{nm}.md`" for _, nm, ti in skills))
        # SKILLFORGE nudge — the half that was missing. Recall could only ever return skills that
        # were WRITTEN, and across 4 pods / 486 ticks only 4 ever were. This surfaces work the agent
        # has now repeated N times with no skill covering it, every tick, in its own recall file.
        # Deterministic, zero-LLM, silent when there is nothing to say.
        try:
            sf = pathlib.Path(__file__).resolve().parent / "skillforge.py"
            if sf.exists():
                r = subprocess.run([sys.executable, str(sf), str(self.base), "nudge", "--min", "3"],
                                   capture_output=True, text=True, timeout=20)
                if (r.stdout or "").strip():
                    out.append("\n" + r.stdout.strip())
        except Exception:
            pass
        out.append(f"\n_focus: {q[:160] or '(none)'}_\n")
        return "\n".join(out)

    def index(self, which="memory"):
        f = (self.skills if which == "skills" else self.mem) / "INDEX.md"
        return f.read_text() if f.exists() else ""

    def forget(self, rel):
        f = self.mem / rel
        if not f.suffix: f = f.with_suffix(".md")
        if f.exists():
            f.unlink()
            idx = self.mem / "INDEX.md"
            idx.write_text("\n".join(l for l in idx.read_text().splitlines() if f"`{rel}`" not in l and f"`{rel}.md`" not in l) + "\n")
            return True
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=".")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("remember"); r.add_argument("text"); r.add_argument("--type", default="lesson"); r.add_argument("--tags", default=""); r.add_argument("--slug"); r.add_argument("--related", default="", help="comma-separated page stems to [[link]] (knowledge pages or memories)")
    lk = sub.add_parser("link"); lk.add_argument("rel", help="memory file, e.g. lesson/foo"); lk.add_argument("targets", nargs="+", help="page stems to [[link]] into its related:")
    c = sub.add_parser("recall"); c.add_argument("query"); c.add_argument("-k", type=int, default=5); c.add_argument("--semantic", action="store_true")
    l = sub.add_parser("learn"); l.add_argument("slug"); l.add_argument("title"); l.add_argument("--body", default=""); l.add_argument("--body-file"); l.add_argument("--gate", action="store_true", help="run the quality gate (structure + local-LLM dedup) before saving; reject thin/duplicate skills")
    i = sub.add_parser("index"); i.add_argument("which", nargs="?", default="memory")
    dg = sub.add_parser("digest"); dg.add_argument("--query", default=None); dg.add_argument("-k", type=int, default=3)
    fg = sub.add_parser("forget"); fg.add_argument("rel")
    un = sub.add_parser("user-note"); un.add_argument("uid"); un.add_argument("note")
    ug = sub.add_parser("user-get"); ug.add_argument("uid")
    w = sub.add_parser("work"); w.add_argument("op", choices=["list", "add", "doing", "done", "drop"]); w.add_argument("arg", nargs="?", default=""); w.add_argument("--evidence", default=""); w.add_argument("--verify", default="")
    cp = sub.add_parser("compact"); cp.add_argument("--keep-days", type=int, default=14); cp.add_argument("--month-cap", type=int, default=30000)
    a = ap.parse_args()
    m = Memory(a.base)
    if a.cmd == "remember":
        f = m.remember(a.text, a.type, [t for t in a.tags.split(",") if t], a.slug,
                       [r for r in a.related.split(",") if r.strip()]); print(f"remembered → {f}")
    elif a.cmd == "link":
        f = m.link(a.rel, a.targets); print(f"linked → {f}" if f else f"not found: {a.rel}")
    elif a.cmd == "recall":
        hits = m.recall(a.query, a.k, a.semantic); print("\n".join(hits) if hits else "(no memory matches)")
    elif a.cmd == "user-note":
        print(f"noted → {m.user_note(a.uid, a.note)}")
    elif a.cmd == "user-get":
        print(m.user_get(a.uid))
    elif a.cmd == "work":
        if a.op == "add": print(f"added work #{m.work_add(a.arg, a.verify)}")
        elif a.op in ("doing", "done", "drop"):
            r = m.work_set(a.arg, {"doing": "doing", "done": "done", "drop": "dropped"}[a.op], a.evidence)
            if isinstance(r, dict) and not r.get("ok"):
                print(f"#{a.arg} REFUSED: {r.get('reason')}\n{r.get('output','')}")
            else: print(f"#{a.arg} → {a.op}")
        else:
            items = [i for i in m.work_list() if i["status"] not in ("done", "dropped")]
            print("\n".join(f"#{i['id']} [{i['status']}] {i['text']}" for i in items) if items else "(work queue empty)")
    elif a.cmd == "learn":
        body = pathlib.Path(a.body_file).read_text() if a.body_file else a.body
        f, v = m.learn(a.slug, a.title, body, gate=a.gate)
        print(f"skill REJECTED by quality gate: {v}" if f is None else f"learned skill → {f} (v{v})")
    elif a.cmd == "digest": print(m.digest(a.query, a.k))
    elif a.cmd == "index": print(m.index(a.which))
    elif a.cmd == "forget": print("forgot" if m.forget(a.rel) else "not found")
    elif a.cmd == "compact": print(m.compact(a.keep_days, a.month_cap))

if __name__ == "__main__":
    main()
