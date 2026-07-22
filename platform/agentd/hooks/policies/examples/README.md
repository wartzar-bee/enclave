# Egress examples — opt in, do not merge wholesale

`../default-egress.json` is deliberately minimal: model APIs, package registries, source, images.
Everything an agent needs to *think* and to install its own dependencies, and nothing else.

Each file here is a capability you may want. **Copy the entries you need into your own policy** (or
point `GUARD_EGRESS_POLICY` at a merged file). Adding a host is a decision — the guard is only worth
having if the allowlist reflects what this deployment is actually for.

| file | grants | think before adding |
|---|---|---|
| `egress-publishing.json` | blogs, social, fiction platforms | an agent that can publish can publish a mistake, at your name |
| `egress-google.json` | Gmail/Drive/Sheets + OAuth | broad: `*.googleapis.com` is most of Google |
| `egress-search.json` | search APIs | metered — each call costs |
| `egress-cloudflare.json` | Cloudflare API, Pages, Workers | write access to live sites |
| `egress-proxy.json` | an outbound proxy vendor | read the file first; it changes who your traffic appears to be |
| `egress-diagnostics.json` | "what is my egress IP" services | harmless, but it is still egress |
