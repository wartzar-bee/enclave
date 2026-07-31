# enclave plugins — the contract

A **plugin** is an installable add-on of one of enclave's existing extension types. It packages
something you'd otherwise wire in by hand into a unit others can install, pin and share:

| type | what it is | existing enclave pattern |
|------|------------|--------------------------|
| `bridge`   | a host-capability bridge (browser / voice / gcloud …) | `tools/bridge-template`, `docs/BRIDGES.md` |
| `tool`     | a tool the agent can call | `tools/` |
| `template` | an agent template surfaced in `enclave init` | `templates/`, `enclave init --template` |
| `policy`   | a policy pack merged into the guard | `policies/`, `platform/agentd/policy.json` |

This is deliverable **#1** of the plugin system (manifest + contract + vetting gate). The
`enclave plugin add|list|remove` CLI (#2) and the runtime loader (#3) build on it.

## The manifest: `plugin.yaml`

Every plugin ships a `plugin.yaml` (or `plugin.json` — same convention `bin/enclave --spec` uses)
at its root:

```yaml
name: sysinfo-bridge          # lowercase slug (a-z 0-9 -), 2-64 chars
version: 0.1.0                 # EXACT pinned semver — no ranges / ^ / ~ / "latest"
type: bridge                  # bridge | tool | template | policy
entrypoint: bridge.py         # relative path to the plugin's main file (must stay inside the dir)
description: >
  One honest sentence about what it does.
author: wartzar-bee
license: Apache-2.0
requires:
  enclave: ">=0.2.0"          # advisory enclave version constraint
security:                     # REQUIRED — declare the plugin's real capabilities (see below)
  network: []                 # list of hostnames it may reach; [] = no egress
  secrets: false              # true iff it reads .secrets / credentials (and why, in description)
  exec: false                 # true iff it runs subprocesses / eval / exec
  # install_script: setup.sh  # optional; enclave NEVER auto-runs it — a maintainer reads + runs it
```

## The vetting gate (a lint that forces an honest manifest)

> **This is a lint, not a sandbox.** It does **not** make an untrusted plugin safe to install. A
> plugin runs with the agent's full privileges (it can reach `.secrets` and host bridges), and the
> scan is a heuristic — a determined author can evade any static check (`getattr`, string-splitting,
> a non-scanned language). What the gate *guarantees* is that a plugin's **manifest is honest about
> the capabilities its code visibly uses**: it can't silently declare `network: []` while its code
> curls a host. **You must still read a plugin's code before trusting it.** For anything that isn't
> first-party, treat a clean gate as "the manifest matches the obvious code," not "this is safe."

Installing a plugin is an **install surface** — the same risk class as baking a dependency into an
image, which enclave already gates (`docs/VETTING.md`). So `enclave plugin add` runs
`tools/plugin/validate.py` first and **refuses to install on any error**:

1. **Pinned version** — `version` must be an exact semver. A floating install isn't reproducible.
2. **Declared contract** — `name`, `type`, `entrypoint` present and well-formed; entrypoint exists
   and stays inside the plugin dir.
3. **Declared-vs-actual** — a static scan reads **every source file the plugin ships** (`.py`, `.js`,
   `.ts`, `.sh`, …, recursively — not just the entrypoint, since `plugin add` copies the whole dir and
   the entrypoint imports its siblings at load time). It **always** reads the declared entrypoint (even
   a suffixless `start`) and any `#!`-shebang script. Each finding names the file that triggered it:
   - reads a secret store / credential env, but `secrets: false` → **FAIL** (undeclared-secrets)
   - reaches a host not in `network` — whether via an in-process call (`urlopen`, `requests`, a socket)
     **or by shelling out** (`curl`/`wget`/`nc`) — or opens egress with `network: []` → **FAIL**
     (undeclared-egress)
   - runs a subprocess / `eval` / `exec`, but `exec: false` → **FAIL** (undeclared-exec)
   - a source file too large to scan, unreadable, or a **symlink escaping the plugin dir** → **FAIL**
     (fail-closed — enclave refuses to install code it could not read)
   - decode-then-run shapes (base64/atob) → **WARN** (a human must read it)
   - declares an `install_script` → **WARN** (enclave never auto-runs it)

The scan is a **heuristic gate, not a proof of safety**. It is fail-closed: an *undeclared* capability,
or any file it cannot fully scan, blocks the install rather than warning.

> **`policy` plugins** merge rules into the security guard itself, so a malicious one is privilege
> escalation, not just data exfil. Do not install a third-party `policy` plugin on a clean gate alone —
> a maintainer must read every rule it adds. (A dedicated policy-review path is tracked as future work.)

```bash
python3 tools/plugin/validate.py path/to/plugin        # exit 0 = clean, exit 2 = rejected
python3 tools/plugin/validate.py path/to/plugin --json  # machine-readable findings
```

## Reference plugin

`examples/plugins/sysinfo-bridge/` repackages the bridge-template's read-only `/sysinfo` capability
as a real plugin — the smallest honest example, and the fixture the test suite validates clean.
`examples/plugins/_bad-example/` is a deliberately lying manifest the suite proves is rejected.

## Build your first plugin

1. `enclave plugin init <name> --type tool` scaffolds a skeleton (`plugin.yaml` + `main.py` stub) that
   already passes the vetting gate — a clean, honest starting point instead of a blank file. (Or copy an
   `examples/plugins/` pattern by hand.)
2. Edit `main.py`; keep `plugin.yaml`'s `security` capabilities declared honestly.
3. `python3 tools/plugin/validate.py .` until it's clean.
4. `enclave plugin add .` to validate-and-install it locally (`enclave plugin list` / `remove`), then
   share the repo. `add` refuses to install anything the vetting gate rejects.
