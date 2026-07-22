#!/usr/bin/env python3
"""
citation_check.py — deterministic verifier for code citations in published articles.

WHY THIS EXISTS
The demopod cost-audit series quotes upstream source and links each block to a pinned tag.
Twice, the quoted code did not exist: a `_get_context` block attributed to `task.py:180` that
actually lives at `crew.py:723`, and an exec-loop snippet cited "around line 290" that was never
in the file. A reader clicking either link finds nothing — the fastest way to lose a technical
audience. This turns "is the citation real?" into a command.

It is also the SCORER for the skill-learning experiment (D-117a): the task
"source-verify an article's code citations against upstream at a pinned tag" recurs (langchain,
autogen, crewai, langgraph) and this gives it a deterministic pass/fail, so a skill revision can be
gated on a held-out score instead of on vibes. Zero LLM calls, no judge, no rubric.

WHAT IT CHECKS
For each ```lang block immediately followed by a `Source: [...](https://github.com/O/R/blob/REF/path)`
line, it fetches the raw file at that ref and grades the block by CLAIM TYPE:

  VERBATIM  (default) — every distinctive line of the block must appear in the source file.
                        Distinctive = not a comment, not a bare delimiter, >= MIN_LINE_CHARS.
  PARAPHRASE          — the block declares itself simplified (e.g. "Simplified to", "# inside ...",
                        "..."). Verbatim matching is WRONG here, so instead every referenced SYMBOL
                        (def/class names, called functions, kwargs) must exist in the file.

  Line anchors (`#L723`) are checked separately: the block's primary symbol must appear within
  --line-tol lines of the anchor. A right-file/wrong-line citation is its own failure class, because
  that is exactly the crewai defect.

FAIL CLASSES: missing-file, absent-code, missing-symbol, wrong-line, unfetchable.
An unfetchable source is a FAIL, not a skip — fail-closed, mirroring contracts.py: a citation you
cannot check is not a verified citation.

USAGE
  citation_check.py <article.md> [<article2.md> ...]        # human report
  citation_check.py --json <article.md> ...                 # machine record (one JSON object)
  citation_check.py --score <article.md> ...                # just the score, "0.9130"
  citation_check.py --selftest                              # offline fixtures, no network

Cache: fetched sources go to ~/.cache/citation-check/ keyed by URL, so re-scoring an unchanged
article costs no network.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.request

MIN_LINE_CHARS = 12          # shorter lines ("return x", "}") are not distinctive enough to grade
DEFAULT_LINE_TOL = 40        # a #L anchor may drift this much before it counts as wrong-line
CACHE = pathlib.Path(os.environ.get("CITATION_CACHE",
                                    pathlib.Path.home() / ".cache" / "citation-check"))

# A block that says any of these is TELLING the reader it is not verbatim. Grading it verbatim
# would manufacture failures and, worse, teach the skill to stop simplifying — which would make the
# articles less readable, not more honest.
PARAPHRASE_MARKERS = (
    "simplified", "abridged", "pseudo", "paraphrase", "roughly", "sketch",
    "# inside", "# in ", "…", "...", "<snip", "# (", "elided",
)

# Two citation styles are in use and BOTH must be parsed. Grading only one of them is not a
# lenient scorer, it is a blind one: the first run reported langchain and autogen as "0/0 verified",
# which reads like a clean article and actually meant "I found nothing to check."
#   (1) trailing:  Source: [`path#L12`](https://github.com/…)      ← crewai, langgraph
#   (2) inline:    …`Foo.bar` ([source](https://github.com/…)):    ← langchain, autogen
SOURCE_RE = re.compile(
    r"^\s*(?:\*\*)?Source(?:\*\*)?\s*:\s*\[[^\]]*\]\(\s*(?P<url>https?://[^\s)]+)\s*\)",
    re.I,
)
INLINE_SOURCE_RE = re.compile(
    r"\[\s*source\s*\]\(\s*(?P<url>https?://github\.com/[^\s)]+)\s*\)", re.I
)
# A ref that moves. The citation may be true today and false next week, and nobody will notice —
# it is unverifiable by construction, so it is graded as a failure of its own class.
MOVING_REFS = {"master", "main", "HEAD", "latest", "dev", "develop"}
BLOB_RE = re.compile(
    r"https?://github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)/blob/(?P<ref>[^/]+)/(?P<path>[^#?\s]+)"
    r"(?:#L(?P<line>\d+))?"
)


def raw_url(m):
    return (f"https://raw.githubusercontent.com/{m['org']}/{m['repo']}/"
            f"{m['ref']}/{m['path']}")


def fetch(url, timeout=25):
    """Return source text, or None. Cached on disk by URL hash."""
    CACHE.mkdir(parents=True, exist_ok=True)
    key = CACHE / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")
    if key.exists():
        return key.read_text(errors="replace")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "citation-check/1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            body = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    key.write_text(body)
    return body


def parse_citations(md_text):
    """[(code, lang, url, block_line_no)] for each fenced block followed by a Source: link.

    'Followed by' tolerates blank lines and one paragraph of prose between the block and its
    Source line — matching how the articles are actually written."""
    lines = md_text.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"^```(\w+)?\s*$", lines[i])
        if not m:
            i += 1
            continue
        lang, start = (m.group(1) or ""), i
        j = i + 1
        while j < len(lines) and not lines[j].startswith("```"):
            j += 1
        code = "\n".join(lines[start + 1:j])
        # (1) look AHEAD for a trailing attribution — either `Source: [...](url)` or a bare
        #     `[source](url)` line (autogen's style; a third variant, found the same way the
        #     second was: by an article scoring 0/0 and that being investigated, not believed).
        url = None
        for k in range(j + 1, min(j + 6, len(lines))):
            sm = SOURCE_RE.match(lines[k]) or INLINE_SOURCE_RE.search(lines[k])
            if sm:
                url = sm.group("url")
                break
            if lines[k].startswith("```"):
                break
        # (2) else look BACK for the inline `([source](url))` style on the introducing prose
        if not url:
            for k in range(start - 1, max(start - 4, -1), -1):
                if lines[k].startswith("```"):
                    break
                im = INLINE_SOURCE_RE.search(lines[k])
                if im:
                    url = im.group("url")
                    break
        if url:
            out.append({"code": code, "lang": lang, "url": url, "line": start + 1})
        i = j + 1
    return out


def is_paraphrase(code):
    low = code.lower()
    return any(mk in low for mk in PARAPHRASE_MARKERS)


def distinctive_lines(code):
    keep = []
    for ln in code.splitlines():
        s = ln.strip()
        if not s or len(s) < MIN_LINE_CHARS:
            continue
        if s.startswith("#") or s.startswith("//"):
            continue
        keep.append(s)
    return keep


def strip_comments(code):
    """Comments are PROSE, not claims about the source. Extracting symbols from them produced a
    false 'missing-symbol: APPEND' on a langgraph block whose comment read
    `# new id → APPEND (the list grows)` — the regex saw `APPEND (` and demanded upstream define it.
    A scorer that invents failures teaches the skill the wrong lesson, so strip them first."""
    out = []
    for ln in code.splitlines():
        s = re.sub(r"(^|\s)#.*$", "", ln)
        s = re.sub(r"(^|\s)//.*$", "", s)
        out.append(s)
    return "\n".join(out)


def symbols(code):
    """Identifiers a reader would go looking for: definitions, calls, kwargs."""
    code = strip_comments(code)
    syms = set()
    syms |= set(re.findall(r"\bdef\s+(\w+)", code))
    syms |= set(re.findall(r"\bclass\s+(\w+)", code))
    syms |= set(re.findall(r"\b([A-Za-z_]\w{3,})\s*\(", code))
    syms |= set(re.findall(r"\b(\w{4,})\s*=", code))
    # Builtins and universal container methods are NOT claims about the cited file — demanding
    # upstream "define" `append` or `get` invents failures out of ordinary Python.
    noise = {"self", "return", "print", "if", "for", "while", "with", "def", "class",
             "import", "from", "and", "not", "None", "True", "False", "str", "int",
             "list", "dict", "type", "range", "len", "super", "format",
             "append", "copy", "extend", "insert", "remove", "sort", "index", "count",
             "keys", "values", "items", "update", "get", "add", "pop", "join", "split",
             "strip", "lower", "upper", "replace", "startswith", "endswith", "encode",
             "decode", "read", "write", "open", "close", "enumerate", "isinstance",
             "getattr", "setattr", "hasattr", "sorted", "reversed", "zip", "map", "filter"}
    return {s for s in syms if s not in noise}


def norm(s):
    """Whitespace-insensitive compare — reflow inside a quoted block is cosmetic, not a fabrication."""
    return re.sub(r"\s+", " ", s).strip()


def grade(cit, line_tol=DEFAULT_LINE_TOL):
    m = BLOB_RE.search(cit["url"])
    if not m:
        return {"ok": False, "why": "missing-file", "detail": "URL is not a github blob link"}
    if m["ref"] in MOVING_REFS:
        return {"ok": False, "why": "unpinned-ref",
                "detail": f"cites `{m['ref']}`, which moves — pin a tag or commit SHA"}
    src = fetch(raw_url(m))
    if src is None:
        return {"ok": False, "why": "unfetchable", "detail": raw_url(m)}

    src_norm = norm(src)
    src_lines = src.splitlines()
    para = is_paraphrase(cit["code"])
    syms = symbols(cit["code"])

    # USAGE block — the article's OWN example code that imports the cited API. The link means
    # "this is where `Send` is defined", not "this text is in that file". Grading it verbatim
    # produced two false fabrication alarms on langgraph (`fanout_node`, `my_node` are the
    # author's functions). The honest check is that the IMPORTED NAMES exist in the cited file.
    # `[^\n]+` not `[\w,\s]+` — the latter is greedy ACROSS newlines and swallowed the next
    # `def` into the imported-name list, failing a valid citation (caught by selftest).
    imported = set(re.findall(r"^\s*from\s+[\w.]+\s+import\s+([^\n]+)$", cit["code"], re.M))
    names = {n.strip() for grp in imported for n in grp.split(",") if n.strip()}
    if names:
        missing = sorted(n for n in names if not re.search(rf"\b(def|class)\s+{re.escape(n)}\b", src)
                         and f"{n} =" not in src and f"{n}=" not in src)
        if missing:
            return {"ok": False, "why": "missing-symbol", "mode": "usage",
                    "detail": "imported but not defined in cited file: " + ", ".join(missing[:6])}
        return {"ok": True, "why": "", "mode": "usage", "symbols": len(names)}

    if para:
        missing = sorted(s for s in syms if s not in src)
        if missing:
            return {"ok": False, "why": "missing-symbol", "detail": ", ".join(missing[:6]),
                    "mode": "paraphrase"}
        result = {"ok": True, "why": "", "mode": "paraphrase", "symbols": len(syms)}
    else:
        want = distinctive_lines(cit["code"])
        missing = [l for l in want if norm(l) not in src_norm]
        if missing:
            return {"ok": False, "why": "absent-code", "mode": "verbatim",
                    "detail": f"{len(missing)}/{len(want)} lines not in source: "
                              + missing[0][:70]}
        result = {"ok": True, "why": "", "mode": "verbatim", "lines": len(want)}

    # line anchor: the primary symbol must be near the cited line
    anchor = m.group("line")
    if anchor and syms:
        want_line = int(anchor)
        hits = [i + 1 for i, l in enumerate(src_lines) if any(s in l for s in syms)]
        if hits and not any(abs(h - want_line) <= line_tol for h in hits):
            nearest = min(hits, key=lambda h: abs(h - want_line))
            result = {"ok": False, "why": "wrong-line", "mode": result.get("mode"),
                      "detail": f"cited #L{want_line}, symbol found at L{nearest}"}
    return result


def check_article(path, line_tol=DEFAULT_LINE_TOL):
    text = pathlib.Path(path).read_text(errors="replace")
    cits = parse_citations(text)
    rows = []
    for c in cits:
        g = grade(c, line_tol)
        rows.append({"line": c["line"], "url": c["url"], **g})
    passed = sum(1 for r in rows if r["ok"])
    return {"article": str(path), "total": len(rows), "passed": passed,
            "score": (passed / len(rows)) if rows else None, "citations": rows}


# ── selftest: offline fixtures, no network ────────────────────────────────────────────────────
def _selftest():
    # NOTE: this fixture must both DEFINE and USE the helper — usage-mode checks that an imported
    # name is *defined* in the cited file, which is the real claim ("this is where Send lives").
    fake = ("def aggregate_raw_outputs_from_task_outputs(outs):\n    return outs\n"
            "line one is padding here\n"
            "def _get_context(self, task, task_outputs):\n"
            "    context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n"
            "    return context\n") + "filler\n" * 200
    url = "https://raw.githubusercontent.com/o/r/tag/src/x.py"
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / (hashlib.sha256(url.encode()).hexdigest()[:24] + ".txt")).write_text(fake)
    blob = "https://github.com/o/r/blob/tag/src/x.py"
    fails = []

    def case(name, md, want_ok, want_why=None):
        cits = parse_citations(md)
        if not cits:
            fails.append(f"{name}: no citation parsed")
            return
        g = grade(cits[0])
        if g["ok"] != want_ok or (want_why and g.get("why") != want_why):
            fails.append(f"{name}: got {g}")

    case("verbatim-present",
         "```python\ndef _get_context(self, task, task_outputs):\n"
         "    context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n"
         f"\nSource: [`x`]({blob})\n", True)
    case("verbatim-fabricated",
         "```python\nfor task in self.tasks:  # this loop does not exist upstream\n"
         "    result = task.run_everything(context=context)\n```\n"
         f"\nSource: [`x`]({blob})\n", False, "absent-code")
    case("paraphrase-ok",
         "```python\n# inside _get_context, simplified:\n"
         "context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n"
         f"\nSource: [`x`]({blob})\n", True)
    case("paraphrase-bad-symbol",
         "```python\n# inside the loop, simplified:\n"
         "context = totally_invented_helper(task_outputs)\n```\n"
         f"\nSource: [`x`]({blob})\n", False, "missing-symbol")
    case("wrong-line",
         "```python\ndef _get_context(self, task, task_outputs):\n"
         "    context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n"
         f"\nSource: [`x`]({blob}#L900)\n", False, "wrong-line")
    case("right-line",
         "```python\ndef _get_context(self, task, task_outputs):\n"
         "    context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n"
         f"\nSource: [`x`]({blob}#L2)\n", True)

    # inline `([source](url))` style, cited BEFORE the block (langchain / autogen)
    case("inline-source-style",
         f"Here is `_get_context` ([source]({blob})):\n\n"
         "```python\ndef _get_context(self, task, task_outputs):\n"
         "    context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n", True)
    # a moving ref is unverifiable by construction
    case("unpinned-ref",
         "```python\ndef _get_context(self, task, task_outputs):\n"
         "    context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n"
         "\nSource: [`x`](https://github.com/o/r/blob/master/src/x.py)\n", False, "unpinned-ref")

    # usage-example block: imports the cited API, defines the author's own function
    case("usage-import-ok",
         "```python\nfrom x import aggregate_raw_outputs_from_task_outputs\n\n"
         "def my_node(state):\n    return aggregate_raw_outputs_from_task_outputs(state)\n```\n"
         f"\nSource: [`x`]({blob})\n", True)
    case("usage-import-missing",
         "```python\nfrom x import totally_invented_symbol\n\n"
         "def my_node(state):\n    return totally_invented_symbol(state)\n```\n"
         f"\nSource: [`x`]({blob})\n", False, "missing-symbol")

    # a symbol named only inside a COMMENT must not be demanded of upstream
    case("comment-symbol-ignored",
         "```python\n# inside the merge loop, simplified:\n"
         "merged.append(m)                  # new id -> APPEND (the list grows)\n"
         "context = aggregate_raw_outputs_from_task_outputs(task_outputs)\n```\n"
         f"\nSource: [`x`]({blob})\n", True)

    md = ("```python\nx = 1\n```\n\nno source line here\n")
    if parse_citations(md):
        fails.append("uncited-block: should not be graded")

    total = 12
    for f in fails:
        print("FAIL", f)
    print(f"{total - len(fails)}/{total} selftest checks passed")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("articles", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--score", action="store_true")
    ap.add_argument("--line-tol", type=int, default=DEFAULT_LINE_TOL)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    if not a.articles:
        ap.error("give at least one article path")

    results = [check_article(p, a.line_tol) for p in a.articles]
    tot = sum(r["total"] for r in results)
    ok = sum(r["passed"] for r in results)
    overall = (ok / tot) if tot else None

    if a.json:
        print(json.dumps({"overall": overall, "total": tot, "passed": ok,
                          "articles": results}, indent=1))
    elif a.score:
        print("" if overall is None else f"{overall:.4f}")
    else:
        for r in results:
            name = pathlib.Path(r["article"]).name
            s = "—" if r["score"] is None else f"{r['score']*100:.0f}%"
            print(f"\n{name}  {r['passed']}/{r['total']} verified  ({s})")
            for c in r["citations"]:
                if c["ok"]:
                    print(f"  ✓ L{c['line']:<5} {c.get('mode','')}")
                else:
                    print(f"  ✗ L{c['line']:<5} {c['why']}: {c.get('detail','')}")
                    print(f"      {c['url']}")
        print(f"\nOVERALL {ok}/{tot} citations verified"
              + ("" if overall is None else f"  ({overall*100:.1f}%)"))
    sys.exit(0)


if __name__ == "__main__":
    main()
