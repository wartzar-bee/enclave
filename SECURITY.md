# Enclave — Security Model

The premise: **the agent is constrained by the OS and the container, not by the model's goodwill.**
Every claim below is verifiable by reading a file or running a command.

## Guarantees

| Claim | Enforced by | Verify |
|---|---|---|
| Agent can only read explicitly-given secrets | read-only `./secrets/` mount (no vault, no ambient access) | the `volumes:` in `docker-compose.yml` |
| Agent cannot escalate privileges | `--cap-drop=ALL --security-opt=no-new-privileges` | `Dockerfile.agent` / compose |
| Agent cannot override behavioral guardrails | PreToolUse hook at the harness layer — **fires under `--dangerously-skip-permissions`** | `platform/agentd/hooks/guard.py` |
| No inbound attack surface on the agent | the agent listens on nothing; the web chat is a separate, loopback-bound sidecar | `netstat` in the agent container |
| Network egress is allowlisted | declarative host allowlist, **report-only until `GUARD_EGRESS_ENFORCE=1`** — see the caveat below, this one is OFF by default | `platform/agentd/hooks/policies/default-egress.json` |
| Knowledge access is scoped | per-agent MCP gateway enforces a collection allowlist server-side (not a model-trusted param) | `platform/agentd/qmd_gateway.mjs` |
| Cloud access is read-only + per-agent isolated | the gcloud bridge maps each agent token → its own credential config + a read-only allowlist | `tools/gcloud/host-bridge.py` |
| A secret can't leak into the memory vault's git history | every vault snapshot is **scan-gated, fail-closed** (a credential pattern blocks the commit) + a vault pre-commit hook; `secrets/` is gitignored | `platform/agentd/vault_snapshot.py` |
| Baked tools don't phone home | `DO_NOT_TRACK=1` in the images (e.g. codegraph telemetry is off); external deps are pinned + security-passed | `Dockerfile.agent`, `docs/VETTING.md` |

## What the guard blocks (default + opt-in)
- Always: `git`, reads of foreign secret stores (`~/.ssh`, `.aws/credentials`, other agents' secrets), publish/sales/bio go-live, and **dynamic-loader / interpreter env injection** (`LD_PRELOAD`, `DYLD_*`, `NODE_OPTIONS`, `BASH_ENV`, `GIT_SSH_COMMAND`, `PERL5LIB`, `PYTHONSTARTUP`) — these smuggle code into the next process and would otherwise bypass the guard.
- `GUARD_CLOUD_READONLY=1` (cloud profile): `gcloud`/`gsutil`/`bq` **writes** (deploy, IAM, load, DML) — fail-safe allowlist of reads; write-mutations to a configured API host (`GUARD_GRAPHQL_HOSTS`) gated to read queries + `login`. Deployment-specific exceptions are env-configured, not baked in (`GUARD_BQ_WRITE_TABLE`, `GUARD_GRAPHQL_HOSTS`).

It is **defense-in-depth**, not the only control: the scoped read-only mount is the primary secret boundary; the guard blocks the obvious foot-guns a tool call could attempt. It **fails open** on unparseable input (never wedges the agent) and **fails closed** on a deny match.

> ⚠ **Egress is report-only by default.** The allowlist above **logs** a disallowed host and permits the call until you set `GUARD_EGRESS_ENFORCE=1`. We ship it off so a first run doesn't fail in an opaque way — but that means, out of the box, the *network* boundary is an audit trail, not a wall. Turn it on for any deployment you actually trust with data. Confirm the mode: `grep '"enforce"' home/state/egress-policy.log`. The kernel/container boundary (caps, mounts, no-new-privileges) is always enforced; only the network policy is opt-in.

## Credential model
Credentials never live in the image and (for cloud) never live in the container — the gcloud bridge keeps them host-side, isolated per agent by token. Prefer **view-only identities** (e.g. an impersonated read-only service account) so writes fail at IAM too, not just at the guard.

### Keep cloud credentials least-privilege and separate from setup
The recommended pattern: whoever runs Enclave sets the local model token (`enclave init`), but the
**cloud** identity behind the bridge should be a **scoped, read-only** service account provisioned
separately — ideally by whoever owns your cloud, not self-minted by the agent's operator. That keeps
the least-privilege boundary at the source: even a fully compromised agent only ever holds the
read-only identity it was granted, and access can be revoked centrally. In a team, that means routing
cloud provisioning through whoever owns IAM; solo, it means making a dedicated read-only account
rather than reusing your own.

## Known limitations (be honest)
- A teammate who can edit the compose/guard config can widen access — this protects against a *compromised/injected agent*, not against a malicious operator with repo write access.
- **The working folder (`WORK_DIR` → `/work`) is read-write by design.** If you point it at a real repo, the agent can read AND modify everything in that tree — including any secrets committed into it (this is a property of *that repo*, not Enclave). The guard still blocks `git` and foreign-secret reads, but it does not sandbox writes within the mounted tree. Mitigations: scrub secrets out of the tree, treat the deployment as trusted, or mount a read-only reference copy plus a separate writable output folder. See `docs/WORK-DIR.md`.
- The vault secret-scan is **pattern-based** (high-confidence credential formats); a novel secret format could slip it. `enclave vault-encrypt` (ciphertext at rest) is the defense for that case.
- The memory vault stores plaintext markdown locally; treat the off-machine copy accordingly (push to a private remote, or encrypt it).

## Reporting a vulnerability
Open a GitHub security advisory on the repo (private disclosure), or a regular issue for
non-sensitive hardening suggestions. There is no formal SLA — this is a small project — but security
reports are triaged ahead of features.
