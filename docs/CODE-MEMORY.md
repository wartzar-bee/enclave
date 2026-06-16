# Code memory — codegraph (opt-in)

The GRAPH-(code) axis of the memory stack (`docs/MEMORY-PROVIDERS.md`): a symbol / call / dependency
graph over a **source-repo** corpus, so the agent answers "who calls this?", "what breaks if I change
X?", "show me this area's code + call paths" without grep-looping. Provided by **codegraph**
(`@colbymchenry/codegraph`, vetted in `docs/VETTING.md`). Opt-in — only useful when the agent works
over code. Telemetry is baked **OFF** everywhere (`DO_NOT_TRACK=1`).

## The sharing constraint (why it's not a drop-in qmd clone)
The codegraph index *is* a shareable asset — build once over a corpus, many agents read it. But unlike
qmd it has **no network transport**: `codegraph serve --mcp` is **stdio-only**, and its SQLite index
**needs write access** to the index dir (a read-only mount fails — SQLite WAL must manage `-wal`/`-shm`).
So "shared" comes in two shapes, plus a local mode:

| Mode | Where codegraph runs | Agent needs | Best for |
|---|---|---|---|
| **Local** | in each agent (stdio MCP) | image built with `INSTALL_CODEGRAPH=1` | one agent over its own code; auto-syncs as it edits |
| **Shared index** | in each agent, reading one shared **writable** index volume (`serve --mcp --no-watch`) | image built with `INSTALL_CODEGRAPH=1` | a few agents on the same host sharing a corpus; one indexer, N readers |
| **Shared bridge** | one `enclave-codegraph` container serving HTTP MCP to all | **nothing** — just `.mcp.json` → the bridge URL | the qmd-style shared service; agents on any host; cleanest separation |

---

## Local mode
codegraph's MCP is stdio (the client spawns `codegraph serve --mcp` as a child), so it lives **in the
agent image**:
1. Build: `docker compose build --build-arg INSTALL_CODEGRAPH=1 agent` (or `INSTALL_CODEGRAPH=1` in `.env`).
2. Mount your code corpus at the **same path** inside the container as on the host (so the index's
   paths resolve), e.g. a `docker-compose.override.yml` adding `/abs/repos:/abs/repos`.
3. Build the index once: `docker compose exec agent codegraph init /abs/repos` (later: `codegraph sync`).
4. `.mcp.json` (already in `templates/ops/`): `{"type":"stdio","command":"codegraph","args":["serve","--mcp"]}`.

## Shared-index mode (one index, many in-agent readers)
The expensive part (the 342 MB-class index + the indexing CPU) is built **once** on a **writable**
shared volume; each agent runs a query-only reader against it:
1. Build the agent image with `INSTALL_CODEGRAPH=1` (as above).
2. Mount the **same writable** corpus volume into every agent + the indexer at the same path.
3. Index once (single writer — never two indexers at once):
   `docker compose run --rm -e CODEGRAPH_MODE=reembed codegraph` (or any one agent runs `codegraph init`).
4. Each agent's `.mcp.json` reader is **query-only** (no watcher, so it never writes/re-syncs):
   `{"type":"stdio","command":"codegraph","args":["serve","--mcp","--no-watch"]}`.
   SQLite allows many concurrent readers on a writable volume; keeping writers to the single indexer
   avoids contention.

## Shared-bridge mode (qmd-style — one HTTP service, agents need nothing)
`Dockerfile.codegraph` fronts codegraph's stdio MCP with an **HTTP MCP bridge**
(`platform/agentd/codegraph_gateway.mjs`, reusing the same Streamable-HTTP transport as the qmd
gateway). One container serves the whole corpus over the network:
1. Set `CODE_CORPUS=/abs/path/to/repos` in `.env`.
2. Build the shared index: `docker compose run --rm -e CODEGRAPH_MODE=reembed codegraph`.
3. Serve: `docker compose --profile codegraph up -d --build codegraph` (listens on `:18184`).
4. Point **any** agent at it — nothing baked into the agent image:
   ```json
   "codegraph": { "type": "http", "url": "http://codegraph:18184/mcp" }
   ```
   (host/launchd shape: `http://host.docker.internal:18184/mcp`, same as qmd.)

## What the agent gets
MCP tools (all read-only): `codegraph_search`, `codegraph_explore`, `codegraph_node`,
`codegraph_callers`, `codegraph_callees`, `codegraph_impact`, `codegraph_files`, `codegraph_status`.

- **BRAIN=claude** — Claude Code reads `.mcp.json` and uses the tools automatically (stdio spawn in
  local/shared-index modes, or HTTP in bridge mode).
- **BRAIN=local / api** — `local_agent.py` doesn't auto-spawn stdio MCP servers; in the local/shared
  modes the `codegraph` CLI is on PATH, so the agent uses the same intelligence via `bash`:
  `codegraph explore <q>`, `codegraph callers <sym>`, `codegraph impact <sym>`, …

## Notes
- The `.codegraph/codegraph.db` holds symbol metadata (paths/signatures/docstrings, **not** full
  source). Over a secret-bearing corpus, keep `.codegraph/` gitignored and never ship the DB.
- Pinned `@colbymchenry/codegraph@1.0.1`. Re-vet on bump (esp. the downloaded platform bundle) per
  `docs/VETTING.md`.
