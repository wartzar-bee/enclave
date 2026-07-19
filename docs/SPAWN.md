# Spawning sub-agents (manager → sub-agent)

Enclave lets a **manager agent** create new sub-agents on its own, safely, without ever touching docker.
The mechanism is a **file-based graduation queue** plus a host-side **spawn watcher** — and the
authorization ("only the manager may spawn") is enforced by **mount topology**, not by a network ACL.

## The pieces

- **Queue dir** — `<queue>/{incoming,processed,failed}/`. The manager agent has this mounted **writable**
  (e.g. at `/graduation`); no other agent mounts it, so no other agent can enqueue.
- **Spec** — a declarative agent definition (`<name>.yaml` or `.json`) the manager writes to
  `incoming/`. See the format below (same one `enclave new --spec` consumes).
- **Spawn watcher** — `enclave fleet watch <queue>` runs a host daemon that watches `incoming/`, and for
  each spec runs `enclave new --image-only --spec` + `enclave run`, then moves the spec to `processed/`
  (or `failed/` with a `.error`). It runs on the host, so it may use docker; the manager never does.

```
manager agent                host                          new sub-agent
  writes  ───────────────▶  <queue>/incoming/x.yaml
                              spawn_watcher picks it up
                              enclave new --image-only --spec x.yaml
                              enclave run --no-build      ───────────▶  x  (running, shared image)
                              spec → processed/
```

## Why it's safe
- The manager can't run docker/git/`enclave` (guard-blocked inside its container). Its ONLY spawn channel
  is writing a file to the queue it has mounted.
- The watcher validates every spec: the name must match `^[a-z0-9][a-z0-9_-]*$`, the target must resolve
  directly under the stacks root (no path escape), and an existing target is refused.
- Every spawn is appended to `~/.config/enclave/fleet-audit.log`.

## Spec format
```yaml
name: my-new-agent           # kebab-case; becomes agent id + folder + container
template: venture            # starter brain: venture | autonomous | orchestrator | ops | analyst | support
brain: claude                # claude | api | local | optimize
model: claude-sonnet-4-6
interval_seconds: 10800
mission: |
  What this agent exists to do, and how progress/"done" is judged.
kpi: the single measurable success signal
term_sheet:                  # REQUIRED for venture-class specs (template/class: venture) — a pod is
  kpi: one measurable signal #   born governed, never governed by retrofitted prose. Materialized at
  kill_line: 2026-12-31      #   spawn as state/term-sheet.json (read by the monitor's mechanical
  budget_usd_weekly: 15      #   kill_line playbook) + state/directives.json (injected every tick).
work_dir: /abs/host/path     # optional: project tree mounted rw at /work
secrets:                     # optional: scoped credential placeholders (operator fills them)
  - { name: example, key: EXAMPLE_API_KEY }
guardrails: [cloud_readonly] # optional profile knobs
allow_git: false             # optional: let this agent `git push` (default false). When true, fill its
                             # secrets/git.env (GIT_USERNAME/GIT_TOKEN) — a credential helper lets it push
                             # without ever reading the raw token; the guard still blocks direct reads.
```

### `allow_git` — letting an agent push
By default agents are guard-blocked from git (the operator owns commits). `allow_git: true` (or
`enclave init --allow-git`) flips a per-agent opt-in: it sets `GUARD_ALLOW_GIT=1`, writes a `.gitconfig`
+ credential helper, and creates `secrets/git.env`. The agent can then `git push`, authenticating through
the helper — but `guard.py` keeps `.secrets/git.env` and the helper on its denylist, so the agent can't
read or print the token. Fill `secrets/git.env` with a scoped, write-limited token. This is a real trust
boundary: a prompt-injected agent with git can rewrite/force-push the repos it can reach — enable it only
for agents you trust (e.g. an orchestrator that owns its own repos).

## Running the watcher
On the host (or via launchd/systemd so it survives reboots):
```bash
ENCLAVE_STACKS_ROOTS=/path/to/fleet enclave fleet watch /path/to/fleet/_queue
# one-shot (process current specs and exit):
enclave fleet watch /path/to/fleet/_queue --once
```
New deployments are created under `--stacks-root` (default: first entry of `$ENCLAVE_STACKS_ROOTS`, else
`~/Dev`) as lightweight shared-image deployments, so they inherit the framework image automatically.

## Wiring a manager
Use the `orchestrator` template for the manager, and mount the queue **writable into that deployment
only**:
```yaml
# manager's docker-compose.override.yml
services:
  agent:
    volumes:
      - /path/to/fleet/_queue:/graduation:rw
```
Then the manager graduates a sub-agent by writing `/graduation/incoming/<name>.yaml`.
