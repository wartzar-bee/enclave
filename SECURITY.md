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
| Knowledge access is scoped | per-agent MCP gateway enforces a collection allowlist server-side (not a model-trusted param) | `platform/agentd/qmd_gateway.mjs` |
| Cloud access is read-only + per-agent isolated | the gcloud bridge maps each agent token → its own credential config + a read-only allowlist | `tools/gcloud/host-bridge.py` |

## What the guard blocks (default + opt-in)
- Always: `git`, reads of foreign secret stores (`~/.ssh`, `.aws/credentials`, other agents' secrets), publish/sales/bio go-live, and **dynamic-loader / interpreter env injection** (`LD_PRELOAD`, `DYLD_*`, `NODE_OPTIONS`, `BASH_ENV`, `GIT_SSH_COMMAND`, `PERL5LIB`, `PYTHONSTARTUP`) — these smuggle code into the next process and would otherwise bypass the guard.
- `GUARD_CLOUD_READONLY=1` (cloud profile): `gcloud`/`gsutil`/`bq` **writes** (deploy, IAM, load, DML) — fail-safe allowlist of reads; write-mutations to a configured API host (`GUARD_GRAPHQL_HOSTS`) gated to read queries + `login`. Deployment-specific exceptions are env-configured, not baked in (`GUARD_BQ_WRITE_TABLE`, `GUARD_GRAPHQL_HOSTS`).

It is **defense-in-depth**, not the only control: the scoped read-only mount is the primary secret boundary; the guard blocks the obvious foot-guns a tool call could attempt. It **fails open** on unparseable input (never wedges the agent) and **fails closed** on a deny match.

## Credential model
Credentials never live in the image and (for cloud) never live in the container — the gcloud bridge keeps them host-side, isolated per agent by token. Prefer **view-only identities** (e.g. an impersonated read-only service account) so writes fail at IAM too, not just at the guard.

### Cloud credentials are provisioned by DevOps, not self-service
A teammate setting up Enclave does **not** mint their own cloud access. To use the cloud bridge they **request credentials from the DevOps team**, who provision a **scoped, read-only identity** (a least-privilege / impersonated service account) for that agent and hand back the bridge config. The local app credential (the model token via `enclave init`) is the teammate's to set; **cloud authority stays gated through DevOps.** This keeps a least-privilege boundary at the source — even a fully compromised agent only ever holds the read-only identity DevOps granted, and access can be revoked centrally.

## Known limitations (be honest)
- A teammate who can edit the compose/guard config can widen access — this protects against a *compromised/injected agent*, not against a malicious operator with repo write access.
- Semantic-search portability still depends on a host engine in shared mode (see README "Known gaps").
- Report issues internally before any external disclosure.
