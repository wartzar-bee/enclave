# Kernel-level egress enforcement (optional)

The guard's egress allowlist is command-text matching — bypassable by design (SECURITY.md). This
overlay adds the real wall: a sidecar that owns the agent's network namespace and enforces a
**default-deny DNS + nftables allowlist in the kernel**. DNS names not in the policy get NXDOMAIN;
connections to IPs that didn't come from an allowed name are dropped. `U=$host; curl $U`, `user@host`
URLs, custom resolvers (`dig @8.8.8.8`), and direct-IP connects all fail.

Sidecar: [OpenSandbox egress](https://github.com/opensandbox-group/OpenSandbox/tree/main/components/egress)
(Apache-2.0). Source-reviewed and behavior-verified 2026-08-15 against the four probes in *Verify*
below (reproduce them yourself before trusting it); pinned by image digest in
`docker-compose.egress.yml` and cosign-verifiable against the upstream signature.

## Enable (per deployment)

Requires **Docker Compose v2.24+** — the agent service uses the `!reset` override tag
(`extra_hosts: !reset []`); older Compose fails the merge with an opaque YAML error.

```sh
cd <deployment>                                        # the dir with docker-compose.yml + .env
cp <framework>/templates/egress-policy.json ./egress-policy.json   # edit: ONLY what this agent needs
echo "EGRESS_TOKEN=$(openssl rand -hex 24)" >> .env
cp <framework>/docker-compose.egress.yml ./docker-compose.override.yml
enclave fleet restart <id>          # fleet.py auto-merges docker-compose.override.yml
```

If the deployment already has a `docker-compose.override.yml`, append the overlay's services to it.

## Verify (run all four after every enable)

```sh
docker exec <AGENT_ID> curl -sS -o /dev/null -w '%{http_code}\n' -m 10 https://api.anthropic.com   # 2xx/4xx = path open
docker exec <AGENT_ID> bash -c 'U=https://example.org; curl -m 5 $U'        # must FAIL (NXDOMAIN)
docker exec <AGENT_ID> curl -m 5 https://1.1.1.1                            # must FAIL (nft drop)
docker exec <AGENT_ID> dig +short +time=3 @8.8.8.8 example.org              # must be EMPTY (proxied+denied)
```

Then watch one full tick and check `home/state/` for anything the agent could no longer reach;
add the specific host to `egress-policy.json` and restart the egress service.

## Invariants (do not weaken)

- **`OPENSANDBOX_EGRESS_MODE=dns+nft`, never `dns`.** dns-only has zero IP-layer enforcement.
- **Policy file at the deployment root**, never inside `./home` — the agent owns home rw and must
  not author its own boundary. Policy changes = edit file, then restart **both** egress and agent
  (`enclave fleet restart <id>`): a restarted sidecar gets a fresh netns, and an agent left in the
  old one loses all connectivity (fail-closed) until it restarts and rejoins (verified).
- **`EGRESS_TOKEN` only in `.env`**, never in `./secrets` (mounted into the agent). The policy API
  on `127.0.0.1:18080` is reachable from the agent's netns; the token is what stops a compromised
  agent from PATCHing its own policy (verified: 401 without it).
- **Wildcards span dots and don't match the apex.** Never allow a wildcard whose subdomains are
  attacker-nameable (`*.amazonaws.com` ⇒ `evil-bucket.s3.amazonaws.com` — the shipped default
  dropped it for this reason).
- Fail-closed: sidecar unhealthy ⇒ agent doesn't start (`depends_on: service_healthy`); sidecar
  crash ⇒ netns keeps the drop rules until restart.

## Known limits

- **Compose service-DNS is unavailable to the agent** (the pair runs on the default bridge —
  Docker's embedded DNS DNATs port-53 before the sidecar's redirect, so the project network can't
  be used). In-compose `qmd`/`codegraph` profiles are unreachable under egress mode; host bridges
  via `host.docker.internal` keep working (allow the host CIDR — Docker Desktop `192.168.65.0/24`,
  Linux `172.17.0.1/32`). `web-chat`/`telegram-relay` share `./home` files, not network: unaffected.
- IPv6 is disabled in the shared netns (policy is v4; v6 would race happy-eyeballs into the drop).
- The guard's text-matching egress stays on as an audit layer (it logs *commands*; the sidecar
  logs *connections*) — they compose.

## Phase 2 — credential vault (optional; live on the finance pod 2026-08-15)

The same sidecar can hold a credential in memory and inject it into outbound HTTPS at a
transparent mitmproxy, so **the agent never holds the real secret** (its env carries a
placeholder; the vault deletes-and-replaces the Authorization header at the proxy). HTTP/HTTPS
only (postgres/other ports bypass the mitm). Recipe (see fleet/the finance pod for a working
example):
- Sidecar: add `OPENSANDBOX_EGRESS_MITMPROXY_TRANSPARENT=true`, a shared volume on
  `/opt/opensandbox` (it exports `mitmproxy-ca-cert.pem` there per generation), and caps
  `CHOWN,SETUID,SETGID` on top of `NET_ADMIN` (socket-dir chown + mitmdump's cap-based uid drop;
  no-new-privileges stays on).
- Agent: mount that volume ro and point every TLS client at the CA:
  `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `SSL_CERT_FILE`, `CURL_CA_BUNDLE`,
  `GIT_SSL_CAINFO`, `PIP_CERT` → `/opt/opensandbox/mitmproxy-ca-cert.pem`.
- Seed: `POST /credential-vault` (one CredentialVaultCreateRequest: inline credential + bearer
  binding to the destination host, which must have an explicit allow in the egress policy) via
  `docker exec` into the sidecar — pattern: `fleet/<id>/egress-vault-init.sh`. Keep the real
  secret in a host-only dir (e.g. `secrets-host/`), NEVER in the agent-mounted `./secrets`.
- **The vault is memory-only: re-run the seed script after every egress restart** — until then
  the placeholder 401s (visible, fail-closed). Restarting only the agent keeps the vault.
- Verify injection: call the destination with a garbage bearer from the agent — a *scope* error
  (real token, wrong scope) instead of an *authentication* error proves replacement.
