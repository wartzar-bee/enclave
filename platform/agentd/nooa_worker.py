#!/usr/bin/env python3
"""
nooa_worker.py — optional NOOA-harness worker for code-over-data questions.

The OPTIONAL sibling of pyexec.py: same job (answer a question about a big structured file by
letting a cheap worker model write code against the parsed object), but run on NVIDIA's NOOA
harness (persistent Jupyter-style session, typed retries, tracing) instead of our stdlib loop.
Exists so the harness comparison stays honest — `enclave eval bigdata` can race pyexec against
NOOA on identical tasks — and as the upgrade path if NOOA's session/typing wins on harder tasks.
Pilot verdict 2026-08-17 (ENCLAVE-NOOA-EVAL doc): both exact-correct at ~2-4k tokens on the
counting class; pyexec is the default because it is stdlib-only.

Only present when the image was built with INSTALL_NOOA=1 (see Dockerfile.agent — pinned,
vetted, pip-audit-gated). Without it, this CLI exits 2 with a one-line explanation, so callers
can fall back to pyexec.

ROUTING DOCTRINE (hard, 2026-08-17 operator rule): Claude tokens come ONLY from the claude CLI
pool. This worker REFUSES any model id matching claude/anthropic — NOOA speaks metered APIs via
LiteLLM, and a Claude id here would silently move Claude usage from the subscription to per-token
billing. Workers are the NVIDIA free pool (or another explicitly configured non-Claude base).

CLI:
  python3 nooa_worker.py --query "failures per tool?" --file events.jsonl
  python3 nooa_worker.py --query "..." --file big.json --model openai/gpt-oss-120b

Model resolution mirrors delegate.py: --model / NOOA_WORKER_MODEL / the delegation policy's
models.nvidia (fast then default) — never a hardcoded vendor id (the retired-qwen lesson).
Metrics (calls/tokens/wall) print to stderr as one JSON line; SPEND_LOG is honored like
local_agent.chat so eval token accounting works unchanged.
"""
import argparse, json, os, pathlib, re, sys, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

_CLAUDE = re.compile(r"anthropic/|claude", re.I)


def resolve_model(explicit=None):
    """--model / env / delegation policy — same chain shape as delegate._model_for, no constants."""
    m = explicit or os.environ.get("NOOA_WORKER_MODEL")
    if m:
        return m
    try:
        import delegate
        models = delegate._policy_models()
        m = models.get("fast") or models.get("default")
        if m:
            return m
    except Exception:
        pass
    raise SystemExit("nooa_worker: no model — pass --model, or set NOOA_WORKER_MODEL, or provide "
                     "the delegation policy.json (models.nvidia). Refusing to guess.")


def main():
    ap = argparse.ArgumentParser(description="NOOA-harness worker: answer a question about a big "
                                             "structured file by writing code against it.")
    ap.add_argument("--query", "-q", required=True)
    ap.add_argument("--file", "-f", required=True)
    ap.add_argument("--model", default="", help="worker model id (NVIDIA pool); NEVER claude/anthropic")
    ap.add_argument("--base", default="", help="OpenAI-compatible base (default: NVIDIA via nvidia_nim/)")
    ap.add_argument("--temp", type=float, default=0.2)
    a = ap.parse_args()

    model = resolve_model(a.model or None)
    # model ids match anthropic/ or claude; a BASE is refused on the bare hostname too
    if _CLAUDE.search(model or "") or re.search(r"anthropic|claude", a.base or "", re.I):
        raise SystemExit(f"nooa_worker REFUSED: '{model}' is a Claude/anthropic target — doctrine: "
                         "Claude only via the claude CLI pool, never a metered harness. Use the "
                         "NVIDIA pool or another non-Claude base.")

    try:
        from nooa import Agent
        from nooa.unifiedllm.registry import get_llm_client
    except ImportError:
        print("nooa_worker: NOOA is not installed in this image (build with INSTALL_NOOA=1); "
              "fall back to pyexec.", file=sys.stderr)
        raise SystemExit(2)

    # LOCAL_BRAIN_KEY / NVIDIA_API_KEY → the env var litellm's nvidia_nim provider reads.
    key = os.environ.get("LOCAL_BRAIN_KEY") or os.environ.get("NVIDIA_API_KEY")
    if key and not os.environ.get("NVIDIA_NIM_API_KEY"):
        os.environ["NVIDIA_NIM_API_KEY"] = key
    if a.base or os.environ.get("LOCAL_BRAIN_BASE"):
        base = a.base or os.environ["LOCAL_BRAIN_BASE"]
        llm = get_llm_client(f"nvidia_nim/{model}", api_base=base, temperature=a.temp)
    else:
        llm = get_llm_client(f"nvidia_nim/{model}", temperature=a.temp)

    metrics = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
    spend = os.environ.get("SPEND_LOG")
    _orig = llm.acall

    async def _acall(*args, **kw):
        r = await _orig(*args, **kw)
        # Harmony-leak repair (measured 2026-08-17, harder-task h2h): NVIDIA's gpt-oss serving
        # stochastically leaks channel markers INTO the tool-call name ('execute_python<|channel|>
        # commentary'); NOOA exact-matches tool names, so each leak burns a retry and runs die at
        # the default budget. Strip from '<|' on — the real name is always the prefix.
        try:
            for tc in (getattr(r, "tool_calls", None) or []):
                if tc.name and "<|" in tc.name:
                    tc.name = tc.name.split("<|", 1)[0]
        except Exception:
            pass
        try:
            u = getattr(r.raw_response, "usage", None)
            metrics["llm_calls"] += 1
            metrics["prompt_tokens"] += getattr(u, "prompt_tokens", 0) or 0
            metrics["completion_tokens"] += getattr(u, "completion_tokens", 0) or 0
            if spend:   # same per-call ledger local_agent.chat writes — eval accounting reads it
                with open(spend, "a") as f:
                    f.write(json.dumps({"ts": time.strftime("%FT%TZ", time.gmtime()),
                                        "model": model,
                                        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
                                        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
                                        "usd": 0.0}) + "\n")
        except Exception:
            pass
        return r

    llm.acall = _acall

    from pyexec import load_data, describe   # shared loader/contract — one sniffing behavior
    data, kind = load_data(a.file)
    contract = describe(data, kind)

    class DataWorker(Agent, llm=llm):
        """You are a worker analyzing a dataset for an autonomous agent runtime."""

        def __init__(self):
            super().__init__()
            self._data = data

        def get_data(self):
            """The parsed dataset as a live Python object (see the DATA CONTRACT in the task —
            size, shape, per-key coverage). Compute over this; never retype its contents."""
            return self._data

        async def answer(self, question: str) -> str:
            """Answer {question} about the dataset exactly. Every number must come from code you
            ran over get_data()'s return value — compute, never estimate. Keys missing on some
            records: use .get()."""
            ...

    import asyncio

    async def run():
        t0 = time.time()
        ans = await DataWorker().answer(f"{a.query}\n\nDATA CONTRACT:\n{contract}")
        metrics["wall_s"] = round(time.time() - t0, 1)
        return ans

    ans = asyncio.run(run())
    print(json.dumps(metrics), file=sys.stderr)
    print(ans)


if __name__ == "__main__":
    main()
