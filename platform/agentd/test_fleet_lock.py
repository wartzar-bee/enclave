#!/usr/bin/env python3
"""
test_fleet_lock.py — the per-agent lifecycle lock must actually serialize same-pod ops and NOT
block different pods, and the guardian must share the exact same lock convention.
"""
import fcntl, importlib, pathlib, sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
fleet = importlib.import_module("fleet")

fails = []
def ck(n, c):
    if not c: fails.append(n)

# 1) while _agent_lock holds pod "alpha", a raw LOCK_NB on the SAME path is refused (mutual exclusion)
with fleet._agent_lock("alpha", wait=1):
    other = open(fleet._lock_path("alpha"), "w")
    try:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ck("same-pod-excludes", False)   # should not reach — acquisition should have raised
        fcntl.flock(other, fcntl.LOCK_UN)
    except OSError:
        ck("same-pod-excludes", True)
    finally:
        other.close()

    # 2) a DIFFERENT pod's lock is independent — acquirable while "alpha" is held
    beta = open(fleet._lock_path("beta"), "w")
    try:
        fcntl.flock(beta, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ck("different-pod-independent", True)
        fcntl.flock(beta, fcntl.LOCK_UN)
    except OSError:
        ck("different-pod-independent", False)
    finally:
        beta.close()

# 3) after release, the lock is re-acquirable
with fleet._agent_lock("alpha", wait=1):
    ck("reacquirable-after-release", True)

# 4) the guardian shares the SAME lock path convention (interlocks across the two modules)
guardian = importlib.import_module("fleet_guardian")
ck("guardian-imported-real-lock", guardian._agent_lock is fleet._agent_lock)

# 5) sanitised path is stable + filesystem-safe
p = fleet._lock_path("weird/../id name")
ck("path-sanitised", "/" not in p.name and " " not in p.name)

if fails:
    print("test_fleet_lock FAIL: " + ", ".join(fails))
    sys.exit(1)
print("test_fleet_lock OK (same-pod serialized, cross-pod independent, guardian shares the lock)")
