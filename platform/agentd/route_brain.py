#!/usr/bin/env python3
"""
route_brain.py — BRAIN=optimize: pick the per-tick engine by COST (the "Phase-2 standalone runner").

Claude (the subscription — free at the margin) runs the tick while its 5h/7d cap has headroom, tiered
Opus(judgment)/Sonnet(mechanical). As the cap FILLS it shifts work to the cheapest REACHABLE pool in
policy.json (free local → low → high); judgment leaves Claude last and prefers a higher-quality pool.
Every pool is an OpenAI-compatible endpoint (xAI / OpenAI / Groq / OpenRouter / local mlx-ollama) — add
one by editing policy.json. Degrades to Claude whenever no pool is reachable, so it never breaks.

Prints ONE line for runtime.sh to consume:
    claude <model>
    pool <base_url> <api_key_env> <model>

Usage: python3 route_brain.py <agent-dir> --reason <startup|inbox|heartbeat|comms>
Env: MODEL (top/judgment Claude model), MODEL_ROUTINE (cheap/mechanical Claude model).
"""
import os, sys, json, pathlib, urllib.request, urllib.error

HERE = pathlib.Path(__file__).resolve().parent
JUDG = ("decide", "strateg", "review", "design", "analy", "investigat", "diagnos", "plan", "assess",
        "judg", "recommend", "evaluat", "root cause", "compare", "architect", "audit")
MECH = ("post", "upload", "schedule", "narrate", "render", "commit", "format", "csv", "export",
        "fetch", "list", "sync", "backup", "rename", "download", "convert", "tag")


def _flag(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def _tier(agent_dir, reason):
    """judgment vs mechanical — same signal as route_tier.py: pending inbox directives + the wake reason."""
    text = ""
    try:
        for ln in (agent_dir / "inbox.md").read_text(errors="ignore").splitlines():
            s = ln.strip()
            if s.startswith("- [ ]"):
                text += " " + s.lower()
    except Exception:
        pass
    if reason == "startup":
        return "judgment"
    if any(w in text for w in JUDG):
        return "judgment"
    if text and any(w in text for w in MECH):
        return "mechanical"
    if reason == "heartbeat" and not text:
        return "mechanical"
    return "judgment"            # safe default: uncertain → top


def _cap_pct(agent_dir):
    """Claude subscription utilization % (max of 5h/7d) from state/claude-usage.json (claude_usage.py writes it)."""
    try:
        d = json.loads((agent_dir / "state" / "claude-usage.json").read_text())
        vals = [float(d.get(k, 0) or 0) for k in ("util_5h", "util_7d", "utilization_5h", "utilization_7d")]
        return max(vals) if vals else 0.0
    except Exception:
        return 0.0               # unknown → assume headroom → stay on Claude


def _secret_present(api_key_env):
    if os.environ.get(api_key_env):
        return True
    sec = pathlib.Path(os.environ.get("TOOLS_ROOT", "/workspace")) / ".secrets"
    if sec.is_dir():
        for f in sec.glob("*.env"):
            try:
                for ln in f.read_text(errors="ignore").splitlines():
                    if ln.strip().startswith(api_key_env + "=") and ln.split("=", 1)[1].strip():
                        return True
            except Exception:
                pass
    return False


def _reachable(pool):
    base = (pool.get("base_url") or "").rstrip("/")
    if not base:
        return False
    if not pool.get("local") and not _secret_present(pool.get("api_key_env", "")):
        return False             # remote pool with no key → skip
    try:
        urllib.request.urlopen(base + "/models", timeout=2.5)
        return True
    except urllib.error.HTTPError:
        return True              # 401/404 etc. = server is up, just gated/no such route
    except Exception:
        return False             # connection refused / timeout = down


def _load_policy(agent_dir):
    # Per-deployment policy (written by `enclave init`, operator-editable) wins; baked default is fallback.
    for p in (agent_dir / "policy.json", HERE / "policy.json"):
        try:
            return json.loads(p.read_text())
        except Exception:
            continue
    return {"pools": {}, "cap": {}}


def decide(agent_dir, reason):
    pol = _load_policy(agent_dir)
    pools = {k: v for k, v in pol.get("pools", {}).items() if not k.startswith("_")}
    soft = float(pol.get("cap", {}).get("soft_pct", 70))
    hard = float(pol.get("cap", {}).get("hard_pct", 90))
    opus = os.environ.get("MODEL", "claude-opus-4-8")
    sonnet = os.environ.get("MODEL_ROUTINE", "claude-sonnet-4-6")

    cap = _cap_pct(agent_dir)
    tier = _tier(agent_dir, reason)

    # 1) plenty of Claude headroom → run on Claude (free at the margin), tiered.
    if cap < soft:
        return f"claude {opus if tier == 'judgment' else sonnet}"
    # 2) soft..hard: keep JUDGMENT on Claude (Sonnet, to conserve); mechanical leaves.
    if cap < hard and tier == "judgment":
        return f"claude {sonnet}"

    # 3) need a pool (mechanical in soft..hard, or anything >= hard). Cheapest reachable, ranked by cost.
    rank = {"free": 0, "low": 1, "high": 2}
    avail = [(k, v) for k, v in pools.items() if _reachable(v)]
    if avail:
        if tier == "judgment":           # judgment off-Claude → prefer the highest-quality reachable pool
            avail.sort(key=lambda kv: -rank.get(kv[1].get("cost", "high"), 2))
        else:                            # mechanical → cheapest
            avail.sort(key=lambda kv: rank.get(kv[1].get("cost", "high"), 2))
        p = avail[0][1]
        return f"pool {p['base_url']} {p.get('api_key_env', '')} {p.get('model', '')}"
    # 4) no pool reachable → degrade to Claude (Sonnet to conserve the cap).
    return f"claude {sonnet}"


def main():
    agent_dir = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
        else pathlib.Path(os.environ.get("AGENT_DIR", "/agent"))
    reason = _flag("--reason", "heartbeat")
    try:
        print(decide(agent_dir, reason))
    except Exception:
        print(f"claude {os.environ.get('MODEL', 'claude-opus-4-8')}")   # never break the tick


if __name__ == "__main__":
    main()
