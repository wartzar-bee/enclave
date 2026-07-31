#!/usr/bin/env python3
"""catalog.py — the editable console catalog: models, providers, presets. NOTHING hardcoded in the UI.

Operator directive (2026-07-30): every list the dashboard offers (model dropdowns, provider defs,
one-click presets) must be manageable FROM the dashboard, not baked into Python. This module is the
single source of truth behind /api/presets and fleet_config.apply_preset:

  * SEED — the in-code defaults. Used ONLY to create the store on first read (a fresh install works
    with zero setup) and to fill keys a stored catalog predates (upgrades add keys, never clobber).
  * store — a JSON file the console edits via POST /api/catalog. Path: $ENCLAVE_CONSOLE_CATALOG,
    else sibling of $ENCLAVE_MODEL_RECS (console-catalog.json), else ~/.enclave/console-catalog.json.

Model-id FORMAT is validated per pool (the 2026-07-30 chat outage was an OpenRouter slug handed to
the claude CLI): pool "claude" needs BARE ids (claude-opus-4-8); api pools (nvidia/openrouter/api)
need provider/model SLUGS (qwen/qwen3-…); "local" is free-form. Every mutation is audited to the
fleet audit log (shows up in the console's Audit tab) and written atomically.
"""
import json, os, pathlib, tempfile, time

# ── seed defaults (used only when the store lacks a key; edit the STORE, not this) ──────────────
SEED = {
    "models": {
        # bare CLI ids — verified against the claude CLI 2026-07-30 (each answered a live prompt)
        "claude": ["claude-opus-4-8", "claude-fable-5", "claude-sonnet-5",
                   "claude-sonnet-4-6", "claude-haiku-4-5-20251001"],
        # extra api-pool ids offered alongside the eval-recs file (slugs)
        "api": [],
        # local pool is machine-specific; empty until the operator adds theirs
        "local": [],
    },
    "providers": {
        "nvidia":     {"label": "NVIDIA (free)", "base": "https://integrate.api.nvidia.com/v1",
                       "key_env": "NVIDIA_API_KEY", "secret": "nvidia.env"},
        "openrouter": {"label": "OpenRouter", "base": "https://openrouter.ai/api/v1",
                       "key_env": "OPENROUTER_API_KEY", "secret": "openrouter.env"},
    },
    "provider_models": {
        "nvidia": ["qwen/qwen3-next-80b-a3b-instruct", "minimaxai/minimax-m3",
                   "openai/gpt-oss-120b", "openai/gpt-oss-20b",
                   "meta/llama-4-maverick-17b-128e-instruct",
                   "nvidia/llama-3.3-nemotron-super-49b-v1.5"],
        "openrouter": [],
    },
    "presets": {
        "claude-managed": {"BRAIN": "claude", "MODEL": "claude-opus-4-8",
                           "MODEL_ROUTINE": "claude-sonnet-4-6", "ROUTER": "on",
                           "DELEGATION_ENFORCE": "on", "SUPERVISE": "auto"},
        "autonomous-local-cheap": {"BRAIN": "local", "SUPERVISE": "auto", "ROUTER": "on"},
        "chat-only-sonnet": {"BRAIN": "claude", "MODEL": "claude-sonnet-4-6", "SUPERVISE": "off"},
        "optimize": {"BRAIN": "optimize", "ROUTER": "on", "SUPERVISE": "auto"},
        "no-claude-nvidia": {"BRAIN": "api",
                             "BRAIN_API_BASE": "https://integrate.api.nvidia.com/v1",
                             "BRAIN_API_KEY_ENV": "NVIDIA_API_KEY",
                             "BRAIN_MODEL": "qwen/qwen3-next-80b-a3b-instruct",
                             "ESCALATION_MODEL": "minimaxai/minimax-m3",
                             "SUPERVISE": "off"},
    },
    # per-model DOCUMENTED sampling params + thinking behaviour, from the model card (never guessed —
    # one global temp invalidates comparisons). temp/top_p may be [thinking, non_thinking] or a scalar.
    # can_think = supports the enable_thinking chat-template toggle. rep = repetition_penalty (null=off).
    # Read by eval/runner.params_for(); results are tagged "default" when a model has no entry.
    "model_params": {
        "mlx-community/gemma-4-26b-a4b-it-4bit":
            {"temp": 1.0, "top_p": 0.95, "top_k": 64, "min_p": 0.0, "rep": None, "can_think": True},
        "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit":
            {"temp": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0, "rep": 1.05, "can_think": False},
        "mlx-community/Qwen3-8B-4bit":
            {"temp": [0.6, 0.7], "top_p": [0.95, 0.8], "top_k": 20, "min_p": 0.0, "rep": None, "can_think": True},
        "lmstudio-community/NVIDIA-Nemotron-3-Nano-30B-A3B-MLX-4bit":
            {"temp": 1.0, "top_p": 1.0, "top_k": 0, "min_p": 0.0, "rep": None, "can_think": True},
    },
}

BARE_POOLS = {"claude"}            # claude CLI: bare ids, no provider prefix
SLUG_POOLS = {"api", "nvidia", "openrouter"}   # OpenAI-compatible endpoints: provider/model slugs


def store_path():
    p = os.environ.get("ENCLAVE_CONSOLE_CATALOG", "").strip()
    if p:
        return pathlib.Path(p)
    recs = os.environ.get("ENCLAVE_MODEL_RECS", "").strip()
    if recs:
        return pathlib.Path(recs).parent / "console-catalog.json"
    return pathlib.Path.home() / ".enclave" / "console-catalog.json"


def _merge(seed, stored):
    """Stored wins per top-level section KEY; seed fills sections/keys the store predates.
    (models/providers/provider_models/presets are dicts — merge per entry so an upgrade that adds
    e.g. a new provider to SEED surfaces it without touching operator edits.)"""
    out = {}
    for sect, sdef in seed.items():
        got = stored.get(sect)
        if isinstance(sdef, dict) and isinstance(got, dict):
            out[sect] = {**sdef, **got}
        else:
            out[sect] = got if got is not None else sdef
    for sect, val in stored.items():          # operator-added sections survive too
        out.setdefault(sect, val)
    return out


def load():
    """The live catalog: store merged over seed. Creates the store from SEED on first read."""
    p = store_path()
    if not p.exists():
        save(SEED)
        return json.loads(json.dumps(SEED))
    try:
        stored = json.loads(p.read_text())
    except Exception:
        return json.loads(json.dumps(SEED))   # unreadable store: serve seed, never crash the console
    return _merge(SEED, stored)


def save(cat):
    p = store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".catalog-")
    with os.fdopen(fd, "w") as f:
        json.dump(cat, f, indent=1)
    os.replace(tmp, p)


def _audit(action, detail):
    """Catalog mutations land in the fleet audit log → visible on the console's Audit tab."""
    try:
        import fleet_config
        audit = pathlib.Path(os.environ.get("ENCLAVE_FLEET_AUDIT", fleet_config.AUDIT))
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "who": "console", "action": f"catalog:{action}",
                                "agent": "-", "detail": str(detail)[:200]}) + "\n")
    except Exception:
        pass


def validate_model(pool, mid):
    """Enforce the per-pool id format (the claude-CLI-vs-slug trap). Returns an error string or None."""
    mid = (mid or "").strip()
    if not mid or " " in mid:
        return "model id must be non-empty, no spaces"
    if pool in BARE_POOLS and "/" in mid:
        return f"pool '{pool}' uses the claude CLI — needs a BARE id (no provider/ prefix)"
    if pool in SLUG_POOLS and "/" not in mid:
        return f"pool '{pool}' is an OpenAI-compatible endpoint — needs a provider/model slug"
    return None


def presets():
    """Catalog-backed preset defs — what fleet_config.apply_preset and /api/presets serve."""
    return load().get("presets", {})


# ── mutators (each: load → validate → mutate → atomic save → audit) ─────────────────────────────
def add_model(pool, mid):
    mid = (mid or "").strip()
    err = validate_model(pool, mid)
    if err:
        return {"error": err}
    cat = load()
    tgt = cat["provider_models"] if pool in cat.get("provider_models", {}) else cat["models"]
    lst = tgt.setdefault(pool, [])
    if mid in lst:
        return {"error": f"{mid} already in {pool}"}
    lst.append(mid)
    save(cat); _audit("add_model", f"{pool} += {mid}")
    return {"ok": True, "catalog": cat}


def remove_model(pool, mid):
    cat = load()
    for sect in ("models", "provider_models"):
        lst = cat.get(sect, {}).get(pool)
        if lst and mid in lst:
            lst.remove(mid)
            save(cat); _audit("remove_model", f"{pool} -= {mid}")
            return {"ok": True, "catalog": cat}
    return {"error": f"{mid} not found in pool {pool}"}


def upsert_provider(name, spec):
    name = (name or "").strip().lower()
    if not name or not isinstance(spec, dict):
        return {"error": "need a provider name + spec object"}
    base = (spec.get("base") or "").strip()
    key_env = (spec.get("key_env") or "").strip()
    if not base.startswith("http") or not key_env:
        return {"error": "provider spec needs base (http…) and key_env"}
    cat = load()
    cat["providers"][name] = {"label": spec.get("label") or name, "base": base,
                              "key_env": key_env, "secret": spec.get("secret") or f"{name}.env"}
    cat["provider_models"].setdefault(name, [])
    save(cat); _audit("upsert_provider", f"{name} → {base}")
    return {"ok": True, "catalog": cat}


def remove_provider(name):
    cat = load()
    if name not in cat.get("providers", {}):
        return {"error": f"unknown provider {name}"}
    cat["providers"].pop(name, None)
    cat["provider_models"].pop(name, None)
    save(cat); _audit("remove_provider", name)
    return {"ok": True, "catalog": cat}


def upsert_preset(name, defn):
    name = (name or "").strip()
    if not name or not isinstance(defn, dict) or not defn:
        return {"error": "need a preset name + non-empty {KEY: value} object"}
    bad = [k for k in defn if not str(k).isupper()]
    if bad:
        return {"error": f"preset keys must be UPPERCASE env keys (bad: {bad})"}
    cat = load()
    cat["presets"][name] = {str(k): str(v) for k, v in defn.items()}
    save(cat); _audit("upsert_preset", f"{name} = {sorted(defn)}")
    return {"ok": True, "catalog": cat}


def remove_preset(name):
    cat = load()
    if name not in cat.get("presets", {}):
        return {"error": f"unknown preset {name}"}
    cat["presets"].pop(name)
    save(cat); _audit("remove_preset", name)
    return {"ok": True, "catalog": cat}


# ── model params + eval evidence (read by eval/runner; written by `enclave eval`) ───────────────
def model_params(mid):
    """The documented sampling/thinking params for a model id, or None (caller falls back + tags it)."""
    return load().get("model_params", {}).get(mid)


def set_model_params(mid, params):
    mid = (mid or "").strip()
    if not mid or not isinstance(params, dict) or not params:
        return {"error": "need a model id + non-empty params object"}
    cat = load()
    cat.setdefault("model_params", {})[mid] = params
    save(cat); _audit("set_model_params", f"{mid} = {sorted(params)}")
    return {"ok": True, "catalog": cat}


def record_eval(mid, summary):
    """Append an eval summary to the model's evidence trail (last 10 kept) — the eval→catalog loop
    that lets a routing pick point at its evidence."""
    mid = (mid or "").strip()
    if not mid or not isinstance(summary, dict):
        return {"error": "need a model id + summary object"}
    cat = load()
    lst = cat.setdefault("eval_results", {}).setdefault(mid, [])
    lst.append(summary)
    del lst[:-10]
    save(cat); _audit("record_eval", f"{mid}: {summary.get('adapter')} {summary.get('acc', summary.get('score'))}")
    return {"ok": True, "catalog": cat}
