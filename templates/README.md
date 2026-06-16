# Agent templates

A starting point for an agent's **home** dir (what mounts to `/agent`). Copy one to `./home`
and edit, or use `./bin/enclave init <name>` to scaffold a fresh one.

An agent home contains:
- `CLAUDE.md`   — the agent's mission + operating rules (loaded as its system prompt)
- `tick.txt`    — what it does each wake (read inbox → act → reply)
- `agent.env`   — runtime config (BRAIN, MODEL, PERMISSION, guard flags). No inline comments after values.
- `.mcp.json`   — MCP servers it may use (e.g. a scoped qmd knowledge gateway)
- `.claude/settings.json` — wires the PreToolUse guard hook
- `inbox.md`, `work.json`, `state/`, `logs/` — runtime I/O (created/used at run time)

Templates here:
- `ops/` — a generic operations agent: answers questions from its knowledge + read-only live queries.
