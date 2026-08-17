"""monitor/intel.py — the OFF-OPUS intelligence layer (D2b).

The deterministic playbooks (monitor/playbooks.py) cover the known failure modes with high confidence.
This layer handles the NOVEL ones: when diagnostics flags a high-severity anomaly that no playbook
recognises, a cheap LLM hypothesises a likely cause + fix. The result is labelled source="llm" with its
own confidence so the dashboard never confuses a guess for a deterministic finding.

HARD CONSTRAINTS:
  • NEVER Opus. The worker is NVIDIA's free OpenAI-compatible API (build.nvidia.com) or a local MLX
    model — same pool delegate.py uses. Resolved at call time; absent key ⇒ this layer is a no-op.
  • FAIL-OPEN. Any resolution/transport/parse error returns None — the monitor degrades to
    deterministic-only, never crashes.
  • The caller (fleet_monitor) owns gating (high-sev + uncovered), caching (one call per distinct
    problem) and rate-limiting. This module is a pure function: anomaly → finding|None.
"""
import os
import json
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
import sys
sys.path.insert(0, str(HERE.parent))   # platform/agentd for local_agent
import local_agent

# Anomaly keys a deterministic playbook already explains well — the LLM stays out of these.
DETERMINISTIC_ANOMALY_KEYS = {"context_explosion", "context_bloat", "tool_failures", "failures"}

BASE = (os.environ.get("MONITOR_LLM_BASE") or os.environ.get("NVIDIA_API_BASE")
        or "https://integrate.api.nvidia.com/v1")


def _resolve_model():
    """Worker model from env or the same policy.json the delegation tier reads. NEVER a hardcoded
    vendor id: the previous default here (qwen/qwen3-next-80b-a3b-instruct) outlived its vendor —
    NVIDIA retired it 2026-07-27 — and this fail-open layer degraded to a silent no-op while
    available() still reported it on. No model resolvable ⇒ '' ⇒ the layer reports unavailable."""
    m = os.environ.get("MONITOR_LLM_MODEL")
    if m:
        return m
    cands = []
    for env in ("MONITOR_LLM_POLICY", "DELEGATE_POLICY"):
        if os.environ.get(env):
            cands.append(pathlib.Path(os.environ[env]))
    roots = [r for r in os.environ.get("ENCLAVE_STACKS_ROOTS", "").split(os.pathsep) if r]
    for r in roots:
        rp = pathlib.Path(r)
        cands += [rp / "tools" / "llm" / "policy.json", rp.parent / "tools" / "llm" / "policy.json"]
    for f in cands:
        try:
            models = (json.loads(f.read_text()).get("models") or {}).get("nvidia") or {}
            if models.get("fast") or models.get("default"):
                return models.get("fast") or models.get("default")
        except Exception:
            continue
    models = (local_agent._policy().get("models") or {}).get("nvidia") or {}
    return models.get("fast") or models.get("default") or ""


def _resolve_key():
    """NVIDIA-free key from env, an explicit file, or a .secrets/nvidia.env under any stacks root (or
    one level of subdir — so $HOME/Dev as a root still finds $HOME/Dev/<ws>/.secrets/nvidia.env)."""
    k = os.environ.get("MONITOR_LLM_KEY") or os.environ.get("NVIDIA_API_KEY")
    if k:
        return k
    cands = []
    ep = os.environ.get("ENCLAVE_NVIDIA_ENV")
    if ep:
        cands.append(pathlib.Path(ep))
    roots = [r for r in os.environ.get("ENCLAVE_STACKS_ROOTS", "").split(os.pathsep) if r]
    for r in roots:
        rp = pathlib.Path(r)
        cands.append(rp / ".secrets" / "nvidia.env")
        try:
            for sub in sorted(rp.iterdir()):
                if sub.is_dir():
                    cands.append(sub / ".secrets" / "nvidia.env")
        except Exception:
            pass
    for f in cands:
        try:
            for ln in f.read_text().splitlines():
                if ln.startswith("NVIDIA_API_KEY="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return ""


def available():
    # Key AND model: a resolvable key with no live model is exactly the state that let this layer
    # read as "on" for a week while every call 410'd.
    return bool(_resolve_key()) and bool(_resolve_model())


def is_novel(anomaly):
    """A high-severity anomaly no deterministic playbook covers — the LLM's job."""
    return anomaly.get("severity") == "high" and anomaly.get("key") not in DETERMINISTIC_ANOMALY_KEYS


def _parse_json(text):
    """Extract the first brace-balanced JSON object (the model may wrap it in prose/fences)."""
    for _, sub in local_agent._json_objects(text or ""):
        try:
            obj = json.loads(sub, strict=False)
            if isinstance(obj, dict) and ("cause" in obj or "fix" in obj):
                return obj
        except Exception:
            continue
    return None


def hypothesize(aid, anomaly, diag, *, timeout=25):
    """Ask the off-Opus worker for a likely cause + fix. Returns a finding dict (source='llm') or None.
    Pure + fail-open: never raises, never calls Opus."""
    key = _resolve_key()
    model = _resolve_model()
    if not key or not model:
        return None
    health = (diag or {}).get("health") or {}
    metrics = (diag or {}).get("metrics") or {}
    ctx = {k: metrics[k] for k in ("context_latest", "cost_per_tick", "duration_s_latest", "cache_pct")
           if k in metrics}
    system = ("You are an SRE diagnosing an autonomous AI agent from telemetry. Given ONE anomaly and its "
              "evidence, reply with STRICT JSON and nothing else: "
              '{"cause":"<one-sentence most-likely root cause>","fix":"<one concrete operator action>",'
              '"confidence":"low|med|high"}. '
              "Be honest: if the evidence is thin, use confidence \"low\" and name what to check. Never invent metrics.")
    user = (f"Agent: {aid}\nAnomaly: {anomaly.get('title')}\nSeverity: {anomaly.get('severity')}\n"
            f"Evidence: {anomaly.get('evidence')}\n"
            f"Current health: {health.get('label')} — {health.get('reason')}\n"
            f"Metrics: {json.dumps(ctx)}")
    try:
        raw = local_agent.chat({"model": model, "base": BASE, "key": key},
                               [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                               max_tokens=220, temperature=0.2, timeout=timeout)
    except Exception:
        return None
    obj = _parse_json(raw)
    if not obj:
        return None
    conf = str(obj.get("confidence", "low")).lower()
    if conf not in ("low", "med", "high"):
        conf = "low"
    return {
        "key": "llm_" + str(anomaly.get("key", "anomaly")),
        "title": anomaly.get("title") or "Anomaly (LLM-diagnosed)",
        "severity": anomaly.get("severity", "med"),
        "cause": str(obj.get("cause", "")).strip()[:300] or None,
        "confidence": conf,
        "evidence": anomaly.get("evidence", ""),
        "recommendation": str(obj.get("fix", "")).strip()[:300] or None,
        "source": "llm",
        "model": model,
    }
