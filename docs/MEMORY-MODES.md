# Enclave — Memory Modes (design note)
## 2026-06-15 · `MEMORY=embedded | shared` + the scoped MCP gateway

Status: **decided + built (both modes).** Shared-mode gateway + the **containerized embedded mode**
(`Dockerfile.qmd` + the `qmd` compose profile, CPU default) both ship. The same scoped-gateway pattern
now also fronts **codegraph** as a network HTTP bridge (`Dockerfile.codegraph`). See `docs/CODE-MEMORY.md`.

---

## The decision

Memory is a **deploy-time switch**, the analog of `BRAIN=claude|local|api`:

```yaml
# agent.yaml
memory:
  mode: embedded                 # embedded | shared
  collections: [knowledge, runbooks]   # embedded → what to index · shared → what this agent is ALLOWED
  shared_url: ""                 # set only when mode: shared (host:port of the scoped gateway)
```

### `MEMORY=embedded` (standalone)
qmd runs **inside the agent's container** (or a companion in its compose), indexing **only that agent's own mounted knowledge**.
- Self-contained, air-gappable, portable — the knowledge ships in the box (`docker compose up`). No host, no network.
- **No allowlist needed** — physical isolation: the index only holds this agent's data.
- Runs **CPU-only** in-container (no Metal). Fine for a focused KB: models are tiny (embeddinggemma-300M + Qwen3-reranker-0.6B). Metal only matters at large-corpus scale / frequent re-embed.
- This is the mode that makes Enclave **shippable** — a customer gets one box: own brain + own memory.

### `MEMORY=shared` (networked)
Agent connects to an external qmd engine through the **scoped MCP gateway** (below), which enforces a per-agent collection allowlist server-side.
- One engine, one re-embed, shared across a fleet; Metal acceleration on a host.
- Isolation comes from the gateway's allowlist (e.g. a `*-security` collection simply isn't served to a given agent).
- Multi-tenant / fleet mode.

---

## Why a gateway, and why *not* config-scoping

qmd's `collections` query parameter is **advisory** — omit it and the server defaults to *all* collections, and `status`/instructions advertise every collection name. Relying on it = trusting the model.

Config-scoping (pointing the server at a YAML listing fewer collections) **does not work on a shared index**: the `index.sqlite` is *self-contained* (carries its own `store_collections` table), and `search`/`get` resolve a requested collection **by name against the DB**, not the YAML. An explicit `collections:['security']` resolves straight out of the DB → leak.

→ On a shared index, **the only real control is code-level enforcement in the MCP server**. We do it without forking qmd: a thin wrapper that imports qmd's public `createStore()` API and registers the same tools with the allowlist applied. qmd stays pinned + unmodified.

## The gateway — `platform/agentd/qmd_gateway.mjs`

A drop-in replacement for `qmd mcp` that enforces `QMD_ALLOWED_COLLECTIONS` on **every** method (fail-closed):

| Tool | Enforcement |
|------|-------------|
| `query` | requested `collections` ∩ allowlist (allowlist if omitted); empty → no results. `store.search` hard-filters by collection. |
| `get` | resolve doc → deny unless `collectionName` ∈ allowlist (returns not-found; no existence leak). |
| `multi_get` | filter returned docs to allowed collections; skipped/oversize entries mapped by filepath→collection prefix, dropped if unverifiable. |
| `status` | only allowed collections; doc total summed over allowed only (no hidden-collection count leak). |
| instructions | only allowed collection names advertised. |
| anything else | not registered → not exposed. |

Run **one gateway process per agent**, launched with that agent's allowlist:
```bash
QMD_ALLOWED_COLLECTIONS=notes,research node platform/agentd/qmd_gateway.mjs
```
The agent's container reaches only its own gateway socket → **process isolation + env allowlist = the boundary** (the gateway sits *outside* the agent's container, so the agent can't disable it — strictly stronger than an in-container guard hook). One shared index, shared models, one re-embed loop.

In `embedded` mode the same gateway runs with the allowlist = all local collections (a no-op filter), so it's **one component, two modes**.

### Generalizes
The same scoped-MCP-gateway pattern fronts **codegraph** (`platform/agentd/codegraph_gateway.mjs` — an
HTTP MCP bridge over codegraph's stdio server, `Dockerfile.codegraph`) and any future shared MCP service,
enforcing per-agent ACLs. This is the control plane the hosted/multi-tenant Enclave tier needs.

### Follow-ups
- ~~Containerize qmd for true `embedded` mode~~ → **done** (`Dockerfile.qmd`, `--profile qmd`, CPU default).
- Optionally upstream a `--collections` / `QMD_ALLOWED_COLLECTIONS` scoping flag to qmd so the wrapper becomes unnecessary.
- Add query audit logging + per-agent rate limits to the gateway (multi-tenant tier).
