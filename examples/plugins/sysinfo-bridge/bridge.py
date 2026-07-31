#!/usr/bin/env python3
"""sysinfo-bridge — reference enclave plugin (type: bridge).

Read-only host capability: reports uname + load average. No network egress, no secret access,
no subprocess — matches its plugin.yaml security declarations exactly, so the validator passes it.
The real bridge transport lives in tools/bridge-template; this entrypoint is the plugin's shim.
"""
from __future__ import annotations

import os
import platform


def sysinfo() -> dict:
    la = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "load_1m": round(la[0], 2),
    }


if __name__ == "__main__":
    import json
    print(json.dumps(sysinfo(), indent=2))
