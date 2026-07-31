# Build your first enclave plugin

A **plugin** is an installable add-on of one of enclave's extension types (`bridge`, `tool`,
`template`, `policy`) — see [`PLUGINS.md`](./PLUGINS.md) for the contract. This is a hands-on
tutorial: you'll build a real `tool` plugin from an empty directory to a validated, installable
unit in five steps. The finished result is checked in at
[`examples/plugins/tokencount-tool/`](../examples/plugins/tokencount-tool/) — build along, or read
it as the answer key.

The plugin we build — **`tokencount-tool`** — estimates the token cost of a text file, so a run can
budget-check a prompt before spending it. It's the smallest honest example of the `tool` type (the
bridge examples cover the other common type).

## 1. Pick a type and scaffold the directory

Every plugin is a directory with a manifest and an entrypoint:

```bash
mkdir tokencount-tool && cd tokencount-tool
```

A `tool` is code the agent can call. (For a host-capability bridge you'd copy `tools/bridge-template`
instead — see the `sysinfo-bridge` / `gcloud-bridge` examples.)

## 2. Write the entrypoint

Keep it pure-stdlib and single-purpose. `tool.py`:

```python
CHARS_PER_TOKEN = 4  # coarse heuristic; good enough for a pre-spend budget check

def estimate(text: str) -> dict:
    chars = len(text)
    return {"chars": chars, "words": len(text.split()),
            "est_tokens": (chars + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN}
```

It reads only the path it's handed, opens no socket, and runs no subprocess. That honesty is what
lets the manifest declare an empty capability set — and what makes it trivially reviewable: the gate
confirms the code matches that empty declaration. (The gate is a lint, not a sandbox — see PLUGINS.md;
it never makes an *untrusted* plugin safe to install, it only forces an honest manifest.)

## 3. Declare the contract — honestly

`plugin.yaml` is the whole trust surface. Two rules the vetting gate enforces hard:

- **Pin the version** — an exact `1.2.0`, never a range / `^` / `~` / `latest`. An install surface
  must be reproducible.
- **Declare your real capabilities** in `security` — network hosts, secret access, code-exec. The
  validator statically scans your entrypoint and **fails the install** if the code does something
  the manifest didn't declare (undeclared egress / secrets / exec). You can't under-declare your way
  past it; you can only be honest.

```yaml
name: tokencount-tool
version: 0.1.0
type: tool
entrypoint: tool.py
description: >
  A tool that estimates the token cost of a text file (~4 chars/token).
author: wartzar-bee
license: Apache-2.0
requires:
  enclave: ">=0.2.0"
security:
  network: []        # no egress
  secrets: false     # reads only the path you pass it
  exec: false        # no subprocess / eval
```

## 4. Validate until clean

```bash
python3 tools/plugin/validate.py examples/plugins/tokencount-tool
# → "✓ tokencount-tool 0.1.0 (tool): clean — no findings"   (exit 0)
```

If you'd lied — say `network: []` while the code called an API — you'd get a non-zero exit and an
`undeclared-egress` error naming the host. Fix the code or fix the manifest; the gate won't budge.
Use `--json` for a machine-readable report.

## 5. Install it

```bash
enclave plugin add examples/plugins/tokencount-tool   # validates first, refuses if rejected
enclave plugin list                                   # tokencount-tool 0.1.0 (tool)
enclave plugin remove tokencount-tool
```

`add` re-runs the vetting gate before it copies anything and never auto-runs an `install_script`.
That's the whole point: **installing a plugin is as safe as the manifest is honest, and the gate
makes dishonesty fail closed.**

## Next

- Ship a `bridge` instead → copy `tools/bridge-template`, model on `examples/plugins/sysinfo-bridge`.
- A capable plugin that *does* need network + secrets is fine — just declare them, like
  `examples/plugins/gcloud-bridge`. Capability isn't the risk; an undeclared capability is.
- The manifest contract, every `security` field, and the gate's rules: [`PLUGINS.md`](./PLUGINS.md).
