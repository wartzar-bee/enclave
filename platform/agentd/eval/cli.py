#!/usr/bin/env python3
"""enclave eval — benchmark models on any OpenAI-compatible pool endpoint.

Usage:
  enclave eval <adapter> --models m1,m2 (--pool NAME | --base URL [--key-env K])
      [--policy policy.json] [--n 50] [--data rows.json] [--out results.jsonl]
      [--timeout 300] [--record]

Adapters: capability (6-task battery) · gsm8k (external math, exact-match).
--pool resolves from a policy.json `pools` section ($LLM_POLICY or --policy) or the catalog
`providers` store. --record appends each model's summary to the catalog evidence trail
(catalog.record_eval) so routing picks can cite their eval. Results jsonl defaults to
$AGENT_DIR/state/evals/<adapter>-<utc>.jsonl. Params come from catalog model_params —
summaries with params_source:"default" mean the model has no documented entry yet.
"""
import argparse, json, os, pathlib, sys, time

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))          # runner/adapters
sys.path.insert(0, str(HERE.parent))   # catalog
import runner
from adapters import ADAPTERS


def main():
    ap = argparse.ArgumentParser(prog="enclave eval", description=__doc__)
    ap.add_argument("adapter", choices=sorted(ADAPTERS))
    ap.add_argument("--models", required=True, help="comma-separated model ids")
    ap.add_argument("--pool", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--key-env", default="")
    ap.add_argument("--policy", default="")
    ap.add_argument("--n", type=int, default=50, help="gsm8k: rows to run")
    ap.add_argument("--data", default="", help="gsm8k: local rows json instead of the HF fetch")
    ap.add_argument("--out", default="")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--record", action="store_true", help="append summaries to the catalog evidence trail")
    args = ap.parse_args()

    ep = runner.resolve_endpoint(pool=args.pool or None, base=args.base or None,
                                 key_env=args.key_env or None, policy_path=args.policy or None)
    out = args.out or str(pathlib.Path(os.environ.get("AGENT_DIR", ".")) / "state" / "evals" /
                          f"{args.adapter}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.jsonl")
    opts = {"n": args.n, "data": args.data, "timeout": args.timeout}
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    summaries = runner.run(ADAPTERS[args.adapter](), models, ep, out, opts)
    for s in summaries:
        if args.record:
            import catalog
            catalog.record_eval(s["model"], s)
        flag = "" if s["params_source"] == "catalog" else "  [params:DEFAULT — undocumented]"
        print(json.dumps(s) + flag)
    print(f"rows → {out}")


if __name__ == "__main__":
    main()
