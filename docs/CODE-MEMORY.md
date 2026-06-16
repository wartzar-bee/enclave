# Code memory — codegraph (opt-in)

The GRAPH-(code) axis of the memory stack (`docs/MEMORY-PROVIDERS.md`): a symbol / call / dependency
graph over a **source-repo** corpus, so the agent answers "who calls this?", "what breaks if I change
X?", "show me this area's code + call paths" without grep-looping. Provided by **codegraph**
(`@colbymchenry/codegraph`, vetted in `docs/VETTING.md`). Opt-in — only useful when the agent works
over code; the base image stays lean without it.

## Why it's baked into the agent image (not a sidecar like qmd)
codegraph's MCP server is **stdio** (`codegraph serve --mcp`) — the MCP client (Claude Code, or your
agent) spawns it as a child process and talks over stdin/stdout. A stdio server can't live in a
separate network container the way qmd's HTTP gateway does, so it must be installed **in the agent
image**. Telemetry is **baked OFF** (`DO_NOT_TRACK=1` in `Dockerfile.agent`; codegraph's telemetry is
counts-only but on-by-default upstream — see `docs/VETTING.md`).

## Enable
1. **Build the agent image with codegraph:**
   ```bash
   # .env:  INSTALL_CODEGRAPH=1     (or)
   docker compose build --build-arg INSTALL_CODEGRAPH=1 agent
   ```
2. **Mount your code corpus** into the agent (read-only is fine for querying). Mount it at the **same
   path inside the container as on the host** so the index's absolute paths resolve:
   ```yaml
   # docker-compose.override.yml
   services:
     agent:
       volumes:
         - /abs/path/to/your/repos:/abs/path/to/your/repos:ro
   ```
3. **Build the index** once (writes `.codegraph/codegraph.db` next to the corpus):
   ```bash
   docker compose exec agent codegraph index /abs/path/to/your/repos
   # incremental updates later:  codegraph sync /abs/path/to/your/repos
   ```
4. **Wire the MCP server.** `templates/ops/.mcp.json` already includes the entry:
   ```json
   "codegraph": { "type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"] }
   ```
   (Other templates omit it; add it if that deployment works over code.)

## What the agent gets
MCP tools: `codegraph_search`, `codegraph_explore`, `codegraph_node`, `codegraph_callers`,
`codegraph_callees`, `codegraph_impact`, `codegraph_files`, `codegraph_status`.

- **BRAIN=claude** — Claude Code reads `.mcp.json` and spawns the stdio server automatically; the
  `mcp__codegraph__*` tools appear.
- **BRAIN=local / api** — `local_agent.py` doesn't auto-spawn stdio MCP servers, but the `codegraph`
  CLI is on PATH, so the agent uses the same intelligence via `bash`:
  `codegraph explore <query>`, `codegraph query <symbol>`, `codegraph callers <symbol>`,
  `codegraph impact <symbol>` (each mirrors the matching MCP tool's output).

## Notes
- The `.codegraph/codegraph.db` is a **local at-rest artifact** holding symbol metadata (paths,
  signatures, docstrings — not full source bodies). If the corpus carries secrets, keep `.codegraph/`
  gitignored and never ship the DB.
- Pinned to `@colbymchenry/codegraph@1.0.1`. Re-vet on bump (especially the downloaded platform
  bundle) per `docs/VETTING.md`.
