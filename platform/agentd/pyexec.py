#!/usr/bin/env python3
"""
pyexec.py — code-over-object big-data tool (own impl; NO external dependency).

The problem it solves: rlm.py's map-reduce is the WRONG shape for counting/aggregation over a
large structured file — each chunk counts locally and the reduce cannot sum what it never saw.
Measured 2026-08-17 (operator's NOOA pilot, ENCLAVE-NOOA-EVAL doc): on "count failures per tool"
over a real 1.3MB events.jsonl, map-reduce burned 638k tokens across 129 calls and answered
WRONG (97 events vs 3,049); a worker model writing Python against the parsed object answered
exactly right in 1-2 calls / ~2k tokens. This is the distilled pass-by-reference idea from
NVIDIA's NOOA harness, re-implemented stdlib-only rather than adopting the framework (same
discipline as rlm.py vs the research repo it distills — see knowledge/security-external-tools.md).

Shape: parse the file ONCE into a Python value `data`; show the model a BOUNDED contract
(type, size, per-key coverage, truncated samples — never the data itself); the model writes a
code cell; we run it in a fresh python3 subprocess with `data` preloaded; stdout (or the
traceback) goes back to the model; loop until it declares FINAL or the cell budget runs out.
Per-key coverage is in the contract because heterogeneous records are exactly where a small
worker model face-plants (a key present on most-but-not-all rows → KeyError or a silently
wrong filter — both observed in the pilot).

Security: the cell runs as the same user that already runs the agent's arbitrary `bash` tool —
inside a pod that is the container boundary, on a host this is exactly `python3 <script>`. No
new privilege is introduced; the subprocess just bounds runtime (timeout) and state (fresh
interpreter per cell, so a wedged cell can't wedge the loop).

CLI:
  python3 pyexec.py --query "how many failures per tool?" --file events.jsonl
  python3 pyexec.py --query "..." --file big.json --max-cells 4

As a tool (BRAIN=local): {"tool":"pyexec","input":{"query":"...","file":"..."}}. Prefer pyexec
for counts/stats/aggregations/joins over structured data (JSONL/JSON/CSV/logs); prefer rlm for
semantic synthesis over unstructured prose ("summarize the argument of this document").

Endpoints come from local_agent.resolve_endpoints() — the cheap LOCAL/worker brain, same as rlm.
Offline (no endpoint): degrades to printing the data contract, so the agent still learns the
file's shape and can fall back to its own bash/python.
"""
import os, sys, re, json, csv, io, argparse, pathlib, subprocess, tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
try:
    from local_agent import chat, resolve_endpoints
    _HAVE_BRAIN = True
except Exception:
    _HAVE_BRAIN = False

DEFAULT_MAX_CELLS = 4       # model cells per question; the pilot needed 1-2, badly-doc'd data 5+
DEFAULT_MAX_TOKENS = 1200
DEFAULT_TEMP = 0.2
DEFAULT_TIMEOUT = 120       # per LLM call, seconds
CELL_TIMEOUT = 90           # per subprocess cell, seconds
STDOUT_CAP = 4000           # chars of cell output fed back to the model / returned as answer
SAMPLE_CAP = 400            # chars per sample record in the contract


# ── loading: file → Python value (the "object" the model codes against) ─────────────────────
def load_data(path):
    """Parse a file into the richest stdlib value that fits: JSONL → list, JSON → value,
    CSV (by extension) → list[dict], else the raw text. Never raises on content — a file that
    parses as nothing structured is still analyzable as text."""
    p = pathlib.Path(path)
    text = p.read_text(errors="replace")
    kind = "text"
    data = text
    if p.suffix.lower() == ".csv":
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
            if rows:
                return rows, "csv rows (list of dicts)"
        except Exception:
            pass
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines:
        parsed, bad = [], 0
        for ln in lines:
            try:
                parsed.append(json.loads(ln))
            except Exception:
                bad += 1
                if bad > max(3, len(lines) // 10):   # >10% junk → not JSONL
                    parsed = None
                    break
        # ≥2 records required: a one-line JSON file is JSON, not single-record JSONL
        if parsed is not None and len(parsed) >= 2:
            return parsed, "JSONL records (list, one parsed line each)"
    try:
        return json.loads(text), "parsed JSON value"
    except Exception:
        pass
    return data, kind


def describe(data, kind):
    """Bounded, ACCURATE contract for the prompt — never the data itself. For record lists the
    per-key coverage line ('ok': 2874/3049) is the load-bearing part: it tells the model which
    keys it must .get() instead of [] — the exact failure the pilot hit with a hand-written
    (and wrong) schema doc."""
    out = [f"data = {kind}"]
    if isinstance(data, list):
        out.append(f"len(data) = {len(data)}")
        dicts = [d for d in data[:5000] if isinstance(d, dict)]
        if dicts:
            cov = {}
            for d in dicts:
                for k in d:
                    cov[k] = cov.get(k, 0) + 1
            n = len(dicts)
            keys = ", ".join(f"{k!r}: {v}/{n}" for k, v in sorted(cov.items(), key=lambda kv: -kv[1]))
            out.append(f"key coverage over first {n} records — a key below {n}/{n} is MISSING on "
                       f"some records, use .get(): {keys}")
        for i, d in enumerate(data[:2]):
            out.append(f"data[{i}] = {json.dumps(d, default=str)[:SAMPLE_CAP]}")
    elif isinstance(data, dict):
        out.append(f"top-level keys: {list(data)[:40]}")
        out.append(f"sample: {json.dumps(data, default=str)[:SAMPLE_CAP]}")
    else:
        out.append(f"len(data) = {len(data)} chars of text")
        out.append("first lines:\n" + "\n".join(str(data).splitlines()[:8])[:SAMPLE_CAP])
    return "\n".join(out)


# ── the cell loop ───────────────────────────────────────────────────────────────────────────
_SYS = ("You analyze a large dataset by writing Python. The data is ALREADY LOADED in a variable "
        "`data` (contract below) — you never see it whole, you compute over it.\n"
        "Each turn reply with EITHER:\n"
        "  * one ```python code cell — stdlib only; it runs in a FRESH interpreter with `data` "
        "preloaded (no state carries over between cells, so a cell must compute AND print "
        "everything it needs seen); print() what you want to observe\n"
        "  * or a line starting with FINAL: followed by the complete answer to the QUESTION\n"
        "Rules: compute, don't estimate — every number you output must come from code output you "
        "saw. Keys missing on some records: use .get(). No imports beyond the stdlib, no network, "
        "no writing files. When one cell can both compute and print the full answer, finish with "
        "FINAL: on the NEXT turn quoting those printed results.")

_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def _extract(reply):
    """Model reply → ('final', answer) | ('code', cell) | ('neither', reply)."""
    t = (reply or "").strip()
    m = re.search(r"^FINAL:\s*(.*)", t, re.S | re.M)
    f = _FENCE.search(t)
    # a reply containing BOTH takes the code first unless FINAL precedes the fence
    if m and (not f or m.start() < f.start()):
        return "final", m.group(1).strip()
    if f:
        return "code", f.group(1)
    return "neither", t


def run_cell(cell, path, timeout=CELL_TIMEOUT):
    """Run one model cell in a fresh python3 with `data` preloaded (same loader). Returns
    (stdout+stderr capped, failed: bool)."""
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
        f.write(f"import sys; sys.path.insert(0, {str(HERE)!r})\n"
                f"from pyexec import load_data\n"
                f"data, _kind = load_data({str(path)!r})\n"
                f"del sys\n" + cell)
        cf = f.name
    try:
        p = subprocess.run([sys.executable or "python3", cf], capture_output=True, text=True,
                           timeout=timeout, cwd=tempfile.gettempdir())
        out = (p.stdout or "") + (("\n[stderr]\n" + p.stderr) if p.stderr.strip() else "")
        return out[:STDOUT_CAP] or "(no output — did the cell print()?)", p.returncode != 0
    except subprocess.TimeoutExpired:
        return f"(cell timed out after {timeout}s — write a cheaper cell)", True
    finally:
        try:
            os.unlink(cf)
        except OSError:
            pass


def run_pyexec(query, path, max_cells=DEFAULT_MAX_CELLS, max_tokens=DEFAULT_MAX_TOKENS,
               temp=DEFAULT_TEMP, timeout=DEFAULT_TIMEOUT, progress=None):
    """Contract → cell loop → FINAL answer (or the last observed output as a labelled fallback)."""
    data, kind = load_data(path)
    contract = describe(data, kind)
    if not _HAVE_BRAIN:
        return f"pyexec offline (no brain endpoint). Data contract:\n{contract}"
    brain, _esc = resolve_endpoints()
    if progress:
        progress(f"pyexec: {path} → {kind}, cells on {brain.get('model')}")
    msgs = [{"role": "system", "content": _SYS},
            {"role": "user", "content": f"QUESTION:\n{query}\n\nDATA CONTRACT:\n{contract}"}]
    last_out = ""
    for cell_no in range(1, max_cells + 1):
        try:
            reply = chat(brain, msgs, max_tokens, temp, timeout)
        except Exception as e:
            return (f"pyexec: endpoint error ({e}). Data contract so the caller can proceed "
                    f"by hand:\n{contract}")
        verdict, payload = _extract(reply)
        if verdict == "final":
            return payload
        if verdict == "neither":
            # Short prose is probably a genuine answer; long fence-less output is the small-model
            # degeneration mode (observed live: gpt-oss-20b import-spamming to the token cap) —
            # never return that as an answer, nudge back onto the protocol instead.
            if len(payload) < 600:
                return payload
            msgs.append({"role": "assistant", "content": reply[:800]})
            msgs.append({"role": "user", "content":
                         "That reply was neither a ```python cell nor FINAL:. Reply with exactly "
                         "one ```python cell, or FINAL: <complete answer>."})
            continue
        out, failed = run_cell(payload, path)
        last_out = out
        if progress:
            progress(f"pyexec: cell {cell_no}/{max_cells} {'FAILED' if failed else 'ok'} "
                     f"({len(out)} chars out)")
        msgs.append({"role": "assistant", "content": reply})
        msgs.append({"role": "user", "content":
                     f"CELL OUTPUT (status={'error' if failed else 'ok'}):\n{out}\n\n"
                     f"Reply with another ```python cell, or FINAL: <complete answer>."})
    # budget exhausted — the last observed output is real computed evidence; return it labelled
    return (f"pyexec: cell budget ({max_cells}) exhausted before FINAL. Last cell output:\n"
            f"{last_out}")


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Code-over-object big-data tool: the worker model "
                                             "writes Python against the parsed file.")
    ap.add_argument("--query", "-q", required=True, help="the question to answer over the data")
    ap.add_argument("--file", "-f", required=True, help="path to the data file (JSONL/JSON/CSV/text)")
    ap.add_argument("--max-cells", type=int, default=DEFAULT_MAX_CELLS)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--temp", type=float, default=DEFAULT_TEMP)
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    a = ap.parse_args()
    print(run_pyexec(a.query, a.file, a.max_cells, a.max_tokens, a.temp, a.timeout,
                     progress=lambda m: print(m, file=sys.stderr)))


if __name__ == "__main__":
    main()
