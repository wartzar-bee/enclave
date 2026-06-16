# Pluggable memory providers

Memory in Enclave is a **stack of complementary layers behind one interface**, not a single tool.
The wiki is the always-on source of truth; everything else is an opt-in accelerator that *indexes
into* the wiki (never replaces it as the source of truth).

## The three axes (complementary, not interchangeable)
| Layer | Question it answers | Default | Opt-in options |
|---|---|---|---|
| **Store** | "what do we know?" (persistent, traceable) | **Wiki** (markdown) — always on | — |
| **Retrieve** | "find the relevant text fast" | — | **qmd** (hybrid+rerank, local) · LanceDB (embedded vectors) |
| **Graph (knowledge)** | "traverse page/entity relationships" | **wiki-graph** (`wiki.py graph` over the `[[wikilinks]]` — ours, stdlib, no dep) | — |
| **Graph (code)** | "traverse a codebase's symbols/calls/deps" | — | **codegraph** (colbymchenry; symbol/dep/call graph → SQLite, Claude-Code native, 20+ langs) — for source-repo corpora |
| **Reason over huge context** | "chew through a 200k-token blob without context rot" | — | **rlm** (`rlm.py` — context-as-variable map-reduce, ours, no dep) |

> The graph axis is **two distinct corpora**, not one engine: the **knowledge graph** is the wiki's
> `[[wikilinks]]` (served by our own `wiki.py graph` — backlinks/neighbors/k-hop/path/hubs); the
> **code graph** is symbols/calls/deps over source repos (codegraph — a focused, validated tool we
> don't reinvent). **Cognee was evaluated and REJECTED** (see `docs/VETTING.md`: 127 deps + telemetry
> on by default); a generic graph engine isn't needed when these two focused layers cover the real needs.

Cross-session user/preference modeling (Mem0/Honcho) is a further opt-in, **lower priority** — and
flagged: Mem0 has a broken telemetry opt-out, Honcho is AGPL + needs Postgres. Both default to cloud
LLM/embedding egress; only adopt pinned to a local endpoint, after a security review.

## The interface (one contract, swappable backends)
Providers are exposed to the agent through the **scoped MCP gateway** (`platform/agentd/qmd_gateway.mjs`
is the first implementation). The contract:
```
query(searches, collections, limit)   # find: wiki=index-nav · qmd/lance=hybrid+rerank · cognee=graph-walk
get(path|docid) / multi_get(pattern)  # ALWAYS return the canonical markdown (wiki is source of truth)
ingest(source)                        # wiki: wiki.py new + cascade + index ; engines: (re)embed/cognify
lint()                                # wiki-native (wiki.py lint) ; engines: index-health
status()                              # collections + health (allowlist-scoped)
```
Rules:
- **`get`/`multi_get` always return the wiki markdown** — accelerators hold disposable, rebuildable
  indexes that *point into* the wiki, so the wiki stays authoritative and any engine can be dropped.
- The gateway enforces a **per-agent collection allowlist** server-side (see the scoped-gateway design
  in `MEMORY-MODES.md`) regardless of backend.
- **Config-selected provider chain**, e.g.:
  ```yaml
  memory:
    store: wiki              # always on (home/knowledge/)
    retrieve: [qmd]          # opt-in accelerator(s)
    graph: []                # e.g. [cognee] when relationship traversal is needed
  ```
  Default install = wiki only (no deps to review). Each accelerator is an explicitly-enabled,
  separately-security-reviewed plugin.

## Adding a provider
1. Implement the contract as an MCP server (mirror `qmd_gateway.mjs`: same tools, same allowlist enforcement).
2. Security-review it (provenance, code read, exfil surface — especially any cloud embedding/LLM egress).
3. Register it in the agent's `.mcp.json` + the `memory:` config. The wiki stays the source of truth.

## Shipped adapters
- **qmd** — `platform/agentd/qmd_gateway.mjs` (host or `Dockerfile.qmd` container, `--profile qmd`).
  Hybrid BM25 + vector + rerank, local. The reference contract implementation.
- **cognee** (graph) — `platform/agentd/providers/cognee_provider.py`, an **adapter STUB, off by
  default**. Implements the full contract (query/get/multi_get/ingest/lint/status) with the same
  per-agent allowlist; `get`/`multi_get` already return the wiki markdown. The graph *engine* is
  NOT installed. **Security pass ran (2026-06-16, see `docs/VETTING.md`) → verdict: DO NOT bake in
  Cognee** — Apache-2.0 (fine) but 42 core / 127 total deps (a web server + its own vector DB),
  default cloud LLM egress, and **telemetry ON BY DEFAULT** (machine-level persistent ID + API-key
  tracking hash, opt-out via `TELEMETRY_DISABLED`). Per the hard rule, if graph memory is genuinely
  needed, author our OWN minimal graph layer (stdlib + networkx + sqlite) behind this same contract.
  `query` returns wiki keyword hits as a floor plus a "graph not provisioned" notice until enabled. The interface
  does not change when the engine is enabled — only the `query` backend does (that is the point of
  the contract). Plug-point: `python3 cognee_provider.py --http <port>` → `.mcp.json`.

See `MEMORY-MODES.md` for embedded-vs-shared deployment (where each provider physically runs) and
`WIKI-LAYER.md` for the default store.
