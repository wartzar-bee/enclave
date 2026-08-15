#!/usr/bin/env python3
"""
test_fleet_guardian.py — the guardian must not resurrect a pod the operator deliberately stopped.

Regression for the parked-agent incident: a `watch: true` pod stopped via console/CLI `down` gets
`state/.operator-stopped` written, but the guardian only checked `.guardian-off` and brought the pod
back within 60s. The guardian now honours `.operator-stopped` (the same signal the monitor uses).

Standalone, no deps; drives the real module with its globals pointed at a temp fleet.
"""
import json, os, pathlib, sys, tempfile, importlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
guardian = importlib.import_module("fleet_guardian")

fails = []
def ck(n, c):
    if not c: fails.append(n)


def setup(tmp):
    fleet_root = pathlib.Path(tmp) / "fleet"
    for aid in ("alive", "stopped", "guardoff", "unwatched"):
        (fleet_root / aid / "home" / "state").mkdir(parents=True, exist_ok=True)
    (fleet_root / "stopped" / "home" / "state" / ".operator-stopped").write_text("2026-08-15T00:00:00Z")
    (fleet_root / "guardoff" / ".guardian-off").write_text("manual")
    manifest = pathlib.Path(tmp) / "fleet.json"
    manifest.write_text(json.dumps({"agents": {
        "alive": {"watch": True}, "stopped": {"watch": True},
        "guardoff": {"watch": True}, "unwatched": {"watch": False},
    }}))
    guardian.FLEET_ROOT = str(fleet_root)
    guardian.MANIFEST = str(manifest)


with tempfile.TemporaryDirectory() as tmp:
    setup(tmp)
    watched = set(guardian._watched_pods())
    ck("alive-watched", "alive" in watched)
    ck("operator-stopped-excluded", "stopped" not in watched)
    ck("guardian-off-excluded", "guardoff" not in watched)
    ck("unwatched-excluded", "unwatched" not in watched)
    ck("excluded-helper-operator-stopped", guardian._excluded_from_watch("stopped") is True)
    ck("excluded-helper-alive-false", guardian._excluded_from_watch("alive") is False)
    # clearing the flag (as `up`/`restart` does) re-arms supervision
    os.unlink(os.path.join(guardian.FLEET_ROOT, "stopped", "home", "state", ".operator-stopped"))
    ck("re-armed-after-clear", "stopped" in set(guardian._watched_pods()))

if fails:
    print("test_fleet_guardian FAIL: " + ", ".join(fails))
    sys.exit(1)
print("test_fleet_guardian OK (operator-stopped pods are not resurrected)")
