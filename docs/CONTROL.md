# Controlling pods (manager → lifecycle of an existing sub-agent)

The **lifecycle twin of spawning** (see [SPAWN.md](SPAWN.md)). Spawning *creates* a new sub-agent;
controlling starts/stops/restarts/kicks one that already exists. Same trust model — a **file-based queue**
plus a host-side **control watcher**, with authorization ("only the manager may control") enforced by
**mount topology**, not a network ACL.

Why it exists: without it, only the operator can start/stop/restart a pod. So when a manager finds a
sub-agent wedged (a broken hook, a crash loop, a stale tick), it has to escalate and *wait*. With the
control queue, the manager drops a one-line spec and the host acts in seconds — no operator round-trip.

## The pieces

- **Queue dir** — `<fleet>/_control/{incoming,processed,failed}/`. The manager has this mounted
  **writable** (e.g. at `/control`); no other agent mounts it, so no other agent can enqueue.
- **Control spec** — a tiny YAML/JSON file the manager writes to `incoming/` naming a target agent and an
  action. Format below.
- **Control watcher** — `enclave fleet control-watch <fleet>/_control` runs a host daemon that watches
  `incoming/`, and for each spec runs the requested verb via `enclave fleet <verb> <id>`, then moves the
  spec to `processed/` (or `failed/` with a `.error`). It runs on the host, so it may use docker; the
  manager never does.

```
manager agent                 host                                 target sub-agent
  writes  ───────────────▶  <fleet>/_control/incoming/x.yaml
                              control_watcher picks it up
                              enclave fleet <action> x   ───────────▶  x  (started/stopped/restarted/kicked)
                              spec → processed/
```

## Actions

| action    | what it does                                                          | maps to |
|-----------|-----------------------------------------------------------------------|---------|
| `up`      | start the pod (`docker compose up -d`)                                | `enclave fleet up <id>` |
| `down`    | stop the pod (`docker compose stop`)                                  | `enclave fleet down <id>` |
| `restart` | bounce the **whole stack** (agent + web-chat + relay)                | `enclave fleet restart <id>` |
| `kick`    | restart **only the `agent` service** → the brain re-enters its loop and ticks now, leaving web-chat/relay up. The light "wake it" verb. | `enclave fleet kick <id>` |
| `logs`    | print the pod's recent logs (diagnostic; output lands in the watcher log) | `enclave fleet logs <id>` |
| `send`    | deliver an operator-style directive to the agent (comms bridge → inbox fallback) | `enclave fleet send <id> "<text>"` |

`restart` vs `kick`: use **kick** to make a wedged or idle agent pick up its inbox / re-read state
*immediately* without tearing down its web surface; use **restart** when a config/image change needs the
whole stack recreated.

## Spec format
```yaml
agent: stoneforge          # target agent id (or omit and name the file <id>.yaml)
action: kick               # up | down | restart | kick | logs | send
text: "resume the swap"    # required ONLY for action: send
requested_by: studio       # optional provenance, recorded in the audit log
```
Drop **one action per file**. Minimal example — wake stoneforge by filename alone:
`incoming/stoneforge.yaml` containing just `action: kick`.

## Why it's safe
- The manager can't run docker/`enclave` (guard-blocked inside its container). Its ONLY control channel is
  writing a file to the queue it has mounted — identical to the spawn queue.
- The watcher validates every spec: agent id must match `^[a-z0-9][a-z0-9_-]*$` and action must be in the
  allowlist `{up, down, restart, kick, logs, send}`. The underlying `enclave fleet` verb then re-validates
  that the agent exists and its compose file resolves **under an allowlisted stacks root**
  (`ENCLAVE_STACKS_ROOTS`) before touching docker — no path escape, no arbitrary compose files.
- Every action is appended to `~/.config/enclave/fleet-audit.log` (the same log spawn_watcher and fleet.py
  write), tagged `"who": "control_watcher"`.
- A control spec can only act on pods that **already exist**; it cannot create, delete, or reconfigure
  one. Creation stays the spawn queue's job; deletion stays the operator's.

## Running the watcher
On the host (or via launchd/systemd so it survives reboots):
```bash
ENCLAVE_STACKS_ROOTS=/path/to/fleet enclave fleet control-watch /path/to/fleet/_control
# one-shot (process current specs and exit):
enclave fleet control-watch /path/to/fleet/_control --once
```
A ready-to-edit launchd template ships at `deploy/launchd/org.enclave.control.plist` (its spawn twin is
`deploy/launchd/org.enclave.spawn.plist`) — fill the `__PLACEHOLDER__` paths and
`launchctl load -w` it.

## Wiring a manager
Mount the control queue **writable into the manager deployment only**:
```yaml
# manager's docker-compose.override.yml
services:
  agent:
    volumes:
      - /path/to/fleet/_control:/control:rw
```
Then the manager controls a sub-agent by writing `/control/incoming/<id>.yaml`. Confirm an action landed
by re-reading the queue (your spec should have moved out of `incoming/` into `processed/`).
