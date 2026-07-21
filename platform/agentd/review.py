#!/usr/bin/env python3
"""review.py — framework review primitive: review(artifact, criticality, independence).

One primitive replaces every hand-rolled "ask a model to judge this" script (the orchestrator had three,
each with hardcoded endpoints). The FRAMEWORK decides which model judges, from policy.json — the
caller declares WHAT KIND of judgment it needs, not which endpoint to hit:

  criticality   routine | important | critical   → how strong a judge (cost tier via policy)
  independence  same_ok | cross_family           → must the judge be a DIFFERENT model family
                                                   from the default judge/generator?

Empirical basis (2026-07-20, three real candidates, same rubric):
  free NVIDIA qwen tier: 5.0/5 ADVANCE on all three — a RUBBER STAMP, i.e. worthless as a judge.
  Opus 4.8 and DeepSeek: 2.2–3.0, agreed on ranking, found real objections.
So pools with cost=="free" are REFUSED for review unless --allow-free is passed explicitly.
Model choice is not cosmetic — it is the difference between a judge and a rubber stamp.

The rubric is the CALLER's (domain logic stays with the caller). This primitive owns: routing,
the API call, robust JSON extraction, score aggregation, and an audit line in state/reviews.jsonl
when run inside an agent (AGENT_DIR set).

CLI:
  review.py <artifact-file|-> --rubric <rubric-file> [--criticality routine|important|critical]
            [--independence same_ok|cross_family] [--allow-free] [--json]
Library:
  from review import review
  verdict = review(text, rubric, criticality="critical", independence="cross_family")

policy.json (agent-local <AGENT_DIR>/policy.json overrides the baked default):
  pools.<name>.family   — model family tag ("anthropic", "google", "deepseek", …) for independence
  review.tiers.<crit>   — {"pool": <name>, "model": <optional override>}
  review.independent    — {"pool": <name>, "model": ..., "family": ...} the cross-family judge
"""
import argparse
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
WORKSPACE = pathlib.Path(os.environ.get("TOOLS_ROOT", "/workspace"))


def _load_policy():
    agent_dir = os.environ.get("AGENT_DIR", "")
    cands = []
    if agent_dir:
        cands.append(pathlib.Path(agent_dir) / "policy.json")
    cands += [HERE / "policy.json", WORKSPACE / "tools/llm/policy.json",
              WORKSPACE / "platform/agentd/policy.json"]
    for p in cands:
        try:
            if p.is_file():
                return json.loads(p.read_text())
        except Exception:
            continue
    return {}


def _resolve_key(key_env, default=None):
    """Env first, then any scoped .secrets/*.env — same resolution runtime.sh uses."""
    v = os.environ.get(key_env)
    if v:
        return v
    for sdir in (pathlib.Path(os.environ.get("AGENT_DIR", "/agent")) / ".secrets",
                 WORKSPACE / ".secrets",
                 pathlib.Path(os.getcwd()) / ".secrets"):   # host/studio checkout runs
        if not sdir.is_dir():
            continue
        for f in sorted(sdir.glob("*.env")):
            try:
                for ln in f.read_text().splitlines():
                    if ln.startswith(key_env + "="):
                        return ln.split("=", 1)[1].strip()
            except OSError:
                continue
    return default


def pick_judge(policy, criticality="important", independence="same_ok"):
    """(base_url, model, key_env, pool_name) or raises ValueError with a routable reason."""
    pools = policy.get("pools", {})
    rev = policy.get("review", {})
    if independence == "cross_family":
        ind = rev.get("independent", {})
        pool = pools.get(ind.get("pool", ""), {})
        if not pool:
            raise ValueError("independence=cross_family but policy.review.independent names no valid pool")
        return (pool["base_url"], ind.get("model") or pool.get("model"),
                pool.get("api_key_env", "OPENROUTER_API_KEY"), ind.get("pool"))
    tier = rev.get("tiers", {}).get(criticality) or rev.get("tiers", {}).get("important") or {}
    name = tier.get("pool")
    pool = pools.get(name, {})
    if not pool:
        # Fallback: strongest non-free remote pool by cost.
        ranked = [(n, p) for n, p in pools.items()
                  if not n.startswith("_") and p.get("base_url") and p.get("cost") != "free"]
        ranked.sort(key=lambda np: {"high": 0, "low": 1}.get(np[1].get("cost"), 2))
        if not ranked:
            raise ValueError("no non-free pool in policy.json to route a review to")
        name, pool = ranked[0]
    return (pool["base_url"], tier.get("model") or pool.get("model"),
            pool.get("api_key_env", "OPENROUTER_API_KEY"), name)


def _extract_json(text):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    blob = m.group(1) if m else text[text.find("{"):]
    try:
        return json.loads(blob)
    except Exception:
        try:
            return json.JSONDecoder().raw_decode(blob)[0]
        except Exception:
            return None


def review(artifact, rubric, criticality="important", independence="same_ok",
           allow_free=False, timeout=180, temperature=0.2):
    """Returns the judge's parsed JSON verdict + _model/_pool/_mean, or {"error": ...}."""
    policy = _load_policy()
    try:
        base, model, key_env, pool_name = pick_judge(policy, criticality, independence)
    except ValueError as e:
        return {"error": str(e)}
    pool_cost = policy.get("pools", {}).get(pool_name, {}).get("cost")
    if pool_cost == "free" and not allow_free:
        return {"error": f"pool '{pool_name}' is a free tier — refused for review (measured "
                         "rubber stamp: 5.0/5 on everything). Pass allow_free to override."}
    key = _resolve_key(key_env)
    if not key:
        return {"error": f"no API key for {key_env} (env or .secrets/*.env)"}
    body = {"model": model, "temperature": temperature,
            "messages": [{"role": "user", "content": f"{rubric}\n\n--- ARTIFACT ---\n{artifact}"}]}
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        j = json.load(urllib.request.urlopen(req, timeout=timeout))
        text = j["choices"][0]["message"]["content"]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "_model": model, "_pool": pool_name}
    v = _extract_json(text)
    if not isinstance(v, dict):
        return {"error": "unparseable judge output", "_model": model, "_pool": pool_name,
                "raw": (text or "")[:400]}
    v["_model"], v["_pool"] = model, pool_name
    scores = [d.get("score") for d in v.values()
              if isinstance(d, dict) and isinstance(d.get("score"), (int, float))]
    v["_mean"] = round(sum(scores) / len(scores), 2) if scores else None
    _audit(v, criticality, independence)
    return v


def _audit(verdict, criticality, independence):
    agent_dir = os.environ.get("AGENT_DIR", "")
    if not agent_dir or not os.path.isdir(agent_dir):
        return
    try:
        p = pathlib.Path(agent_dir) / "state" / "reviews.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps({"ts": int(time.time()), "criticality": criticality,
                                 "independence": independence,
                                 "model": verdict.get("_model"), "pool": verdict.get("_pool"),
                                 "mean": verdict.get("_mean"),
                                 "recommendation": verdict.get("recommendation"),
                                 "error": verdict.get("error")}) + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact", help="artifact file, or - for stdin")
    ap.add_argument("--rubric", required=True, help="rubric file")
    ap.add_argument("--criticality", default="important",
                    choices=("routine", "important", "critical"))
    ap.add_argument("--independence", default="same_ok", choices=("same_ok", "cross_family"))
    ap.add_argument("--allow-free", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()
    artifact = sys.stdin.read() if a.artifact == "-" else open(a.artifact, encoding="utf-8",
                                                               errors="replace").read()
    rubric = open(a.rubric, encoding="utf-8", errors="replace").read()
    v = review(artifact, rubric, a.criticality, a.independence, a.allow_free)
    if a.as_json:
        print(json.dumps(v, indent=2))
    else:
        if v.get("error"):
            print(f"ERROR: {v['error']}", file=sys.stderr)
            return 1
        print(f"{v.get('_pool')}/{v.get('_model')}  mean={v.get('_mean')}  "
              f"-> {v.get('recommendation', '?')}")
        print(json.dumps({k: val for k, val in v.items() if not k.startswith('_')}, indent=2))
    return 0 if not v.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
