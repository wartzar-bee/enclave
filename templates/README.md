# Agent templates

A starting point for an agent's **home** dir (what mounts to `/agent`). Copy one to `./home`
and edit, or use `./bin/enclave init <name>` to scaffold a fresh one.

An agent home contains:
- `CLAUDE.md`   — the agent's mission + operating rules (loaded as its system prompt)
- `tick.txt`    — what it does each wake (read inbox → act → reply)
- `agent.env`   — runtime config (BRAIN, MODEL, PERMISSION, guard flags). No inline comments after values.
- `.mcp.json`   — MCP servers it may use (scoped `qmd` gateway; `ops` also wires the `codegraph` stdio server)
- `.claude/settings.json` — wires the PreToolUse guard hook
- `knowledge/`  — the markdown wiki; with `memory/` + `skills/` it forms ONE linked brain (`wiki.py graph --brain`)
- `inbox.md`, `work.json`, `state/`, `logs/` — runtime I/O (created/used at run time)

`enclave init` makes the home its own **scan-gated git vault** (durable memory; secrets excluded). See `docs/WIKI-LAYER.md`.

Templates here:
- `ops/` — a generic operations agent: answers questions from its knowledge + read-only live queries.
- `support/` — a customer/user support agent: answers from a knowledge base and drafts replies for
  human approval; never sends, refunds, or mutates anything.
- `analyst/` — a research/data analyst: investigates a question, synthesizes an evidence-based brief
  with citations, and may run read-only cloud/data queries (ships `GUARD_CLOUD_READONLY=1`).

A template's `agent.env` may declare extra runtime knobs (e.g. the read-only cloud profile);
`enclave init` merges those on top of the core config it generates.
