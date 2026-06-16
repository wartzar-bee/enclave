#!/usr/bin/env python3
"""
rlm.py — Recursive-Language-Model big-context tool (own impl; NO external dependency).

The problem it solves: a single LLM call rots / truncates on a 200k-token blob (a giant log,
a whole repo dump, a long transcript). RLM treats the large input as a VARIABLE the model
queries rather than a prompt to stuff: it MAPS a sub-query over bounded chunks with a cheap
model, then RECURSIVELY REDUCES the partial answers into one — so depth, not context length,
scales the work. Inspired by the alexzhang13/rlm pattern (context-as-variable + recursive
sub-calls); we re-implemented the distilled idea ourselves rather than adopt a research repo
wholesale (same discipline as comic-creator vs the external comic repos — see
knowledge/security-external-tools.md). Pure stdlib + the runtime's own `chat()` — no new dep,
no sprawling code to vet, fits the guard (it's just another tool the agent calls).

CLI:
  python3 rlm.py --query "list every TODO and who owns it" --file big.txt
  cat big.txt | python3 rlm.py --query "summarize the failures"
  python3 rlm.py --query "..." --file a.log --window 12000 --overlap 400 --max-tokens 1024

As a tool (BRAIN=local): the agent calls {"tool":"rlm","input":{"query":"...","file":"..."}}
(or "text":"..."). Returns the reduced answer.

Endpoints come from local_agent.resolve_endpoints() (the same MLX/OpenRouter policy the brain
uses): MAP runs on the cheap LOCAL brain; the FINAL reduce can optionally escalate (--final-escalate)
to the reasoning model for a cleaner synthesis. If no endpoint is reachable, it degrades to an
extractive concatenation of per-chunk keyword hits so the agent still gets *something* offline.
"""
import os, sys, re, json, argparse, pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    from local_agent import chat, resolve_endpoints   # reuse the one stdlib OpenAI-compatible caller
    _HAVE_BRAIN = True
except Exception:
    _HAVE_BRAIN = False

DEFAULT_WINDOW = 12000      # chars per chunk (~3k tokens); bounded so each map call is cheap+reliable
DEFAULT_OVERLAP = 400       # carry context across chunk boundaries
DEFAULT_FANIN = 8           # partials combined per reduce node (tree reduce)
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMP = 0.2
DEFAULT_TIMEOUT = 120
MAX_DEPTH = 6               # recursion backstop


# ── chunking: context-as-variable, split into bounded windows with overlap ──────────────────
def chunk_text(text, window=DEFAULT_WINDOW, overlap=DEFAULT_OVERLAP):
    text = text or ""
    if len(text) <= window:
        return [text] if text.strip() else []
    chunks, i, n = [], 0, len(text)
    step = max(1, window - overlap)
    while i < n:
        end = min(i + window, n)
        # prefer to break on a newline near the window edge (keeps lines/records intact)
        if end < n:
            nl = text.rfind("\n", i + step // 2, end)
            if nl > i:
                end = nl + 1
        chunks.append(text[i:end])
        if end >= n:
            break
        i = max(i + step, end - overlap)
    return chunks


_MAP_SYS = ("You answer a QUESTION using ONLY the provided text chunk. Extract the relevant facts "
            "verbatim where possible; cite line snippets. If the chunk has nothing relevant, reply "
            "exactly 'NONE'. Be terse — this is one of many partial answers that will be merged.")
_REDUCE_SYS = ("You merge several PARTIAL answers (each from a different slice of one large document) "
               "into one coherent, de-duplicated answer to the QUESTION. Drop 'NONE' partials. Keep "
               "every distinct fact; resolve overlaps; do not invent anything not in the partials.")


def _map_one(endpoint, query, chunk, idx, total, max_tokens, temp, timeout):
    msgs = [{"role": "system", "content": _MAP_SYS},
            {"role": "user", "content": f"QUESTION:\n{query}\n\nCHUNK {idx+1}/{total}:\n{chunk}"}]
    try:
        out = (chat(endpoint, msgs, max_tokens, temp, timeout) or "").strip()
        return "" if out.upper() == "NONE" else out
    except Exception as e:
        # offline / endpoint down → extractive fallback: lines matching any query keyword
        kws = [w.lower() for w in re.findall(r"\w{4,}", query)]
        hits = [ln for ln in chunk.splitlines() if any(k in ln.lower() for k in kws)]
        return ("\n".join(hits[:20]) if hits else "") + (f"\n[rlm: map fell back, endpoint error: {e}]" if idx == 0 else "")


def _reduce(endpoint, query, partials, max_tokens, temp, timeout, depth=0):
    partials = [p for p in partials if p and p.strip()]
    if not partials:
        return "NONE — no chunk contained anything relevant to the question."
    if len(partials) == 1:
        return partials[0]
    if depth >= MAX_DEPTH:
        return "\n\n---\n\n".join(partials)   # backstop: stop recursing, concatenate
    # tree-reduce in fan-in batches so each reduce call stays bounded
    if len(partials) > DEFAULT_FANIN:
        merged = []
        for i in range(0, len(partials), DEFAULT_FANIN):
            merged.append(_reduce(endpoint, query, partials[i:i + DEFAULT_FANIN], max_tokens, temp, timeout, depth + 1))
        return _reduce(endpoint, query, merged, max_tokens, temp, timeout, depth + 1)
    joined = "\n\n".join(f"[partial {i+1}]\n{p}" for i, p in enumerate(partials))
    msgs = [{"role": "system", "content": _REDUCE_SYS},
            {"role": "user", "content": f"QUESTION:\n{query}\n\nPARTIAL ANSWERS:\n{joined}"}]
    try:
        return (chat(endpoint, msgs, max_tokens, temp, timeout) or "").strip() or "\n\n".join(partials)
    except Exception:
        return "\n\n---\n\n".join(partials)   # offline → just stitch the partials


def run_rlm(query, text, window=DEFAULT_WINDOW, overlap=DEFAULT_OVERLAP,
            max_tokens=DEFAULT_MAX_TOKENS, temp=DEFAULT_TEMP, timeout=DEFAULT_TIMEOUT,
            final_escalate=False, progress=None):
    """Map a sub-query over bounded chunks of `text`, then recursively reduce to one answer."""
    chunks = chunk_text(text, window, overlap)
    if not chunks:
        return "NONE — empty input."
    brain = esc = None
    if _HAVE_BRAIN:
        brain, esc = resolve_endpoints()
    map_ep = brain or {"base": "", "model": "offline", "key": ""}
    if progress:
        progress(f"rlm: {len(text)} chars → {len(chunks)} chunks, mapping on {map_ep.get('model')}")
    partials = [_map_one(map_ep, query, c, i, len(chunks), max_tokens, temp, timeout)
                for i, c in enumerate(chunks)]
    reduce_ep = (esc if (final_escalate and esc and esc.get("key")) else map_ep)
    if progress:
        kept = sum(1 for p in partials if p.strip())
        progress(f"rlm: {kept}/{len(chunks)} chunks had hits → reducing on {reduce_ep.get('model')}")
    return _reduce(reduce_ep, query, partials, max_tokens, temp, timeout)


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="RLM big-context tool: map a query over chunks, recursively reduce.")
    ap.add_argument("--query", "-q", required=True, help="the question to answer over the big input")
    ap.add_argument("--file", "-f", help="path to the large input (else read stdin)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--overlap", type=int, default=DEFAULT_OVERLAP)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--temp", type=float, default=DEFAULT_TEMP)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    ap.add_argument("--final-escalate", action="store_true",
                    help="reduce the final answer on the reasoning model instead of the cheap brain")
    a = ap.parse_args()
    text = pathlib.Path(a.file).read_text(errors="replace") if a.file else sys.stdin.read()
    ans = run_rlm(a.query, text, a.window, a.overlap, a.max_tokens, a.temp, a.timeout,
                  a.final_escalate, progress=lambda m: print(m, file=sys.stderr))
    print(ans)


if __name__ == "__main__":
    main()
