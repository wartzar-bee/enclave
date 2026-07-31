#!/usr/bin/env python3
"""eval/runner.py — endpoint-based model-eval runner (framework primitive).

Hits any OpenAI-compatible /chat/completions endpoint — local MLX, ollama, NVIDIA free, OpenRouter,
anything the deployment can route to — so the SAME eval runs host-side or in-container, against any
pool. No mlx_lm/torch imports here; a Metal-direct fallback stays a host-side tool where it belongs.

Per-model sampling params come from catalog.model_params() (DOCUMENTED values from the model card —
a single global temp invalidates comparisons; results are tagged params:"default" when a model has no
entry so an undocumented run is never mistaken for a documented one). Thinking mode is requested via
chat_template_kwargs.enable_thinking; endpoints that reject extra fields get one retry with the
extras stripped (recorded as extras_stripped so think-mode claims stay honest).

One request at a time, models run sequentially — the local MLX server holds one resident model and
evicts on switch ([[mlx-wired-memory-freeze]]: never make it hold two).
"""
import json, os, pathlib, time, urllib.request, urllib.error

DEFAULT_PARAMS = {"temp": 0.7, "top_p": 0.95, "top_k": 0, "min_p": 0.0, "rep": None, "can_think": None}
EXTRA_KEYS = ("top_k", "min_p", "repetition_penalty", "chat_template_kwargs")


def pick(v, think_on):
    """Resolve a [thinking, non_thinking] pair — or a scalar — for the active mode."""
    return v[0 if think_on else 1] if isinstance(v, (tuple, list)) else v


def _key_from_secrets(key_env, secret_file):
    """env wins; else scan the standard secrets mounts for KEY= in the named secret file."""
    k = os.environ.get(key_env or "")
    if k:
        return k
    if not (key_env and secret_file):
        return ""
    for root in (os.environ.get("AGENT_DIR", "/agent"), os.environ.get("TOOLS_ROOT", "/workspace"),
                 str(pathlib.Path.home())):
        f = pathlib.Path(root) / ".secrets" / secret_file
        try:
            for ln in f.read_text().splitlines():
                if ln.startswith(key_env + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def resolve_endpoint(pool=None, base=None, key_env=None, policy_path=None):
    """→ {base, key, label}. Explicit --base wins; else the named pool from a policy.json `pools`
    section (studio-style) or the catalog `providers` store. Raises ValueError if unresolvable."""
    if base:
        return {"base": base.rstrip("/"), "key": _key_from_secrets(key_env, None) if key_env else
                os.environ.get(key_env or "", ""), "label": base}
    if not pool:
        raise ValueError("need --pool or --base")
    pp = policy_path or os.environ.get("LLM_POLICY", "")
    if pp and pathlib.Path(pp).exists():
        pools = json.loads(pathlib.Path(pp).read_text()).get("pools", {})
        if pool in pools:
            p = pools[pool]
            b = os.environ.get(p.get("base_url_env", ""), "") or p.get("base_url_default", "")
            k = os.environ.get(p.get("api_key_env", ""), "") or p.get("api_key_default", "")
            if not k:
                k = _key_from_secrets(p.get("api_key_env"), f"{pool}.env")
            if b:
                return {"base": b.rstrip("/"), "key": k, "label": f"policy:{pool}"}
    import catalog
    prov = catalog.load().get("providers", {}).get(pool)
    if prov:
        return {"base": prov["base"].rstrip("/"), "key": _key_from_secrets(prov.get("key_env"), prov.get("secret")),
                "label": f"catalog:{pool}"}
    raise ValueError(f"pool '{pool}' not in policy pools or catalog providers")


def params_for(model, think_on):
    """Documented params from the catalog, resolved for the active thinking mode.
    Falls back to DEFAULT_PARAMS with source='default' — surfaced in results, never silent."""
    import catalog
    p = catalog.model_params(model)
    src = "catalog" if p else "default"
    p = {**DEFAULT_PARAMS, **(p or {})}
    return {"temperature": pick(p["temp"], think_on), "top_p": pick(p["top_p"], think_on),
            "top_k": p["top_k"], "min_p": p["min_p"], "rep": p["rep"],
            "can_think": p["can_think"], "source": src}


def chat(ep, model, prompt, params, think=None, max_tokens=1024, timeout=300):
    """One /chat/completions call → {text, secs, tokens, extras_stripped, error}.
    Extra sampling fields (top_k/min_p/rep/chat_template_kwargs) are sent when set; a 4xx triggers
    one retry with them stripped so strict endpoints still eval (flagged in the result)."""
    body = {"model": model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens, "temperature": params["temperature"], "top_p": params["top_p"]}
    if params.get("top_k"):
        body["top_k"] = params["top_k"]
    if params.get("min_p"):
        body["min_p"] = params["min_p"]
    if params.get("rep"):
        body["repetition_penalty"] = params["rep"]
    if think is not None and params.get("can_think"):
        body["chat_template_kwargs"] = {"enable_thinking": bool(think)}
    hdrs = {"Content-Type": "application/json"}
    if ep["key"]:
        hdrs["Authorization"] = "Bearer " + ep["key"]
    stripped = False
    for attempt in (0, 1):
        req = urllib.request.Request(ep["base"] + "/chat/completions",
                                     data=json.dumps(body).encode(), headers=hdrs)
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
            txt = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            toks = (d.get("usage") or {}).get("completion_tokens")
            return {"text": txt, "secs": round(time.time() - t0, 1), "tokens": toks,
                    "extras_stripped": stripped, "error": None}
        except urllib.error.HTTPError as e:
            if attempt == 0 and 400 <= e.code < 500 and any(k in body for k in EXTRA_KEYS):
                for k in EXTRA_KEYS:
                    body.pop(k, None)
                stripped = True
                continue
            return {"text": "", "secs": round(time.time() - t0, 1), "tokens": None,
                    "extras_stripped": stripped, "error": f"HTTP {e.code}: {e.read()[:200]}"}
        except Exception as e:
            return {"text": "", "secs": round(time.time() - t0, 1), "tokens": None,
                    "extras_stripped": stripped, "error": f"{type(e).__name__}: {e}"}


def run(adapter, models, ep, out_path, opts=None):
    """Eval each model on the adapter's tasks; append one jsonl row per (model, task) to out_path;
    return per-model summaries. Models run sequentially (single-resident local server)."""
    opts = opts or {}
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    summaries = []
    for model in models:
        rows = []
        for task in adapter.tasks(opts):
            think = task.get("think", False)
            p = params_for(model, think)
            r = chat(ep, model, task["prompt"], p, think=think if p["can_think"] else None,
                     max_tokens=task.get("max_tokens", 1024), timeout=opts.get("timeout", 300))
            ok, detail = (None, r["error"]) if r["error"] else adapter.grade(task, r["text"])
            row = {"ts": time.strftime("%FT%TZ", time.gmtime()), "adapter": adapter.name,
                   "endpoint": ep["label"], "model": model, "task": task["id"], "think": think,
                   "params_source": p["source"], "ok": ok, "detail": str(detail)[:200],
                   "secs": r["secs"], "tokens": r["tokens"], "extras_stripped": r["extras_stripped"],
                   "error": r["error"]}
            rows.append(row)
            with out.open("a") as f:
                f.write(json.dumps(row) + "\n")
        s = adapter.summarize(rows)
        s.update({"model": model, "adapter": adapter.name, "endpoint": ep["label"],
                  "params_source": rows[0]["params_source"] if rows else "default",
                  "ts": time.strftime("%FT%TZ", time.gmtime())})
        summaries.append(s)
    return summaries
