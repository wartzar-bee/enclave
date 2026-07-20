#!/usr/bin/env python3
"""effective_config.py — boot-time "what is actually ACTIVE on this pod" report (N1, 2026-07-20).

The three worst enclave failures were all the same class: a mechanism silently INACTIVE while
everyone believed it was running — preflight (never executed for BRAIN=api pods, for weeks),
BRAIN_MODEL fail-open to a paid model (L-310), ROUTER nullified by MODEL_ROUTINE==MODEL. Config
lives in four layers (compose env > agent.env > fleet.conf > defaults) and nothing ever showed the
MERGED result, so a dead mechanism looked identical to a live one.

Every tick start this writes state/effective-config.json: each mechanism ACTIVE|INACTIVE + why,
the effective brain, and per-variable provenance. It also DIFFS against the previous report and
logs any mechanism that flipped ACTIVE→INACTIVE — a flip is exactly the silent death this exists
to catch. Deterministic, read-only besides its own output file, never fails the tick.

Run (from runtime.sh, after env layering): effective_config.py <agent_dir>
"""
import json
import os
import pathlib
import sys
import time


def _parse_env_file(path):
    d = {}
    try:
        for ln in pathlib.Path(path).read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    except OSError:
        pass
    return d


def provenance(var, agent_env):
    """Approximate source of the effective value: compose/session env beats agent.env beats default.
    (runtime.sh loads agent.env defaults-only — a pre-set env var always wins.)"""
    if var not in os.environ:
        return "default(unset)"
    if agent_env.get(var) == os.environ[var]:
        return "agent.env"
    return "env"


def _key_resolvable(key_env, agent_dir):
    if os.environ.get(key_env):
        return True
    for sdir in (agent_dir / ".secrets", pathlib.Path(os.environ.get("TOOLS_ROOT", "/workspace")) / ".secrets"):
        if sdir.is_dir():
            for f in sdir.glob("*.env"):
                try:
                    if any(ln.startswith(key_env + "=") for ln in f.read_text().splitlines()):
                        return True
                except OSError:
                    continue
    return False


def collect(agent_dir):
    ad = pathlib.Path(agent_dir)
    env = os.environ
    aenv = _parse_env_file(ad / "agent.env")
    brain = env.get("BRAIN", "claude")
    model = env.get("MODEL", "")
    routine = env.get("MODEL_ROUTINE", "")
    guard_hook = ad / ".claude" / "hooks" / "guard.py"

    mech = {}

    def m(name, active, why, **extra):
        mech[name] = {"active": bool(active), "why": why, **extra}

    # brain path — the engine everything else rides on
    key_env = env.get("BRAIN_API_KEY_ENV", "OPENROUTER_API_KEY")
    brain_info = {"mode": brain}
    if brain == "api":
        brain_info.update({"model": env.get("BRAIN_MODEL", ""), "base": env.get("BRAIN_API_BASE", ""),
                           "key_env": key_env, "key_resolvable": _key_resolvable(key_env, ad)})
        m("brain", bool(env.get("BRAIN_MODEL")) and brain_info["key_resolvable"],
          "BRAIN_MODEL set + key resolvable" if env.get("BRAIN_MODEL") and brain_info["key_resolvable"]
          else "BRAIN_MODEL unset or key unresolvable → ticks will DEFER (loud-fail, L-310)")
    else:
        brain_info["model"] = model or "(claude default)"
        m("brain", True, f"BRAIN={brain}")

    m("preflight", True, "always runs (all brains, 2026-07-20)",
      requires=[x.strip() for x in env.get("REQUIRES", "").split(",") if x.strip()],
      gate=env.get("PREFLIGHT_GATE", "0") == "1")

    router_on = env.get("ROUTER", "off") == "on"
    nullified = router_on and routine and routine == model
    m("router", router_on and not nullified,
      "ROUTER=on" if router_on and not nullified else
      ("NULLIFIED: MODEL_ROUTINE == MODEL — routine ticks NOT downgraded" if nullified else "ROUTER!=on"),
      model=model, routine=routine)

    m("guard", guard_hook.is_file(),
      "hook present" if guard_hook.is_file() else f"guard hook MISSING at {guard_hook}",
      egress_enforce=env.get("GUARD_EGRESS_ENFORCE", "0") == "1")

    m("supervisor", brain == "local" or bool(env.get("SUPERVISE")),
      "SUPERVISE/BRAIN=local" if (brain == "local" or env.get("SUPERVISE")) else "not configured (claude/api pods run without co-supervisor)")

    m("delivery", bool(env.get("DELIVERY_MARKER")),
      "DELIVERY_MARKER set (preflight verifies pipe)" if env.get("DELIVERY_MARKER")
      else "no DELIVERY_MARKER — output pipeline not declared/verified",
      marker=env.get("DELIVERY_MARKER", ""))

    cap = env.get("API_BUDGET_WEEKLY_USD", env.get("API_BUDGET_USD", "15"))
    m("spend_cap", cap not in ("", "0"), f"weekly cap ${cap}" if cap not in ("", "0") else "cap DISABLED (0)",
      weekly_usd=cap)

    m("decision_capture", brain in ("api", "local"),
      "finish-contract (runtime-written decisions.jsonl)" if brain in ("api", "local")
      else "claude path: convention only (no structural capture yet)")

    m("warm_session", env.get("WARM_SESSION", "1") != "0",
      "session persists across ticks" if env.get("WARM_SESSION", "1") != "0" else "WARM_SESSION=0 (fresh each tick)")

    prov_vars = ["BRAIN", "MODEL", "MODEL_ROUTINE", "ROUTER", "BRAIN_MODEL", "BRAIN_API_BASE",
                 "BRAIN_API_KEY_ENV", "REQUIRES", "PREFLIGHT_GATE", "DELIVERY_MARKER",
                 "API_BUDGET_WEEKLY_USD", "INTERVAL_SECONDS", "WARM_SESSION", "SUPERVISE",
                 "GUARD_EGRESS_ENFORCE", "LOCAL_MAX_STEPS", "MAX_TURNS"]
    return {"_ts": int(time.time()), "agent": env.get("AGENT_ID", ad.name),
            "brain": brain_info, "mechanisms": mech,
            "provenance": {v: provenance(v, aenv) for v in prov_vars if v in os.environ or v in aenv}}


def main():
    ad = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("AGENT_DIR", "/agent"))
    out = ad / "state" / "effective-config.json"
    rec = collect(ad)

    # Diff vs the previous report: an ACTIVE→INACTIVE flip is the silent death this file exists to catch.
    flips = []
    try:
        prev = json.loads(out.read_text())
        for name, cur in rec["mechanisms"].items():
            was = prev.get("mechanisms", {}).get(name, {}).get("active")
            if was is True and cur["active"] is False:
                flips.append(f"{name} ({cur['why']})")
    except Exception:
        pass

    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, indent=2))
    except OSError as e:
        print(f"effective-config: write failed ({e})")
        return 0

    inactive = [n for n, v in rec["mechanisms"].items() if not v["active"]]
    line = (f"config: {len(rec['mechanisms']) - len(inactive)} mechanisms active"
            + (f", INACTIVE: {', '.join(inactive)}" if inactive else ""))
    if flips:
        line += f" | ⚠ WENT INACTIVE since last tick: {'; '.join(flips)}"
        try:
            with (ad / "state" / "escalations.log").open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} WARN :: [config] "
                         f"{rec['agent']} — mechanism(s) flipped ACTIVE→INACTIVE: {'; '.join(flips)}. "
                         f"See state/effective-config.json.\n")
        except OSError:
            pass
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
