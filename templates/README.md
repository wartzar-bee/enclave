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
- `autonomous/` — a self-driving worker: instead of no-op'ing until messaged, each tick it reconstructs
  state from its own memory + `work.json` queue, picks the next highest-value step toward a `{MISSION}`
  the operator sets, does it, records evidence, and updates memory — no human in the loop. Runs as a
  `daemon` on a short heartbeat (`INTERVAL_SECONDS=10800`); `SUPERVISE=auto` enables the in-container
  off-opus supervisor when `BRAIN=local`. Still guard-bounded (no git, scoped secrets).
- `code-review/` — a read-only code reviewer: `inbox.md` names the review target (a repo path or diff);
  it gathers the change with `git diff`, reads the surrounding code (fanning out per-dimension review
  subagents — security/correctness/quality/performance), gates every finding to >80% confidence with an
  exact `file:line`, and writes a `changes-requested`/`approve` verdict to `state/chat-reply.md`. It
  **reports; it never edits.** Wires the `codegraph` MCP server for caller/definition tracing.
- `research/` — a deep web-research agent: `inbox.md` names a research question; it fans out parallel
  per-angle search subagents, fetches the PRIMARY source for every load-bearing claim, spawns an
  adversarial verifier to refute each claim, and writes a cited, fact-checked report (answer-first, with
  a Sources section) to `state/chat-reply.md`. **Reports; never acts on the world.** Web-source variant
  of `analyst` (which targets private/cloud data); no cloud profile needed — read-only HTTP GET only.
- `orchestrator/` — a manager agent: runs its own mission AND can graduate new sub-agents into their own
  solo deployments by writing a `spec` to its graduation queue (a host watcher builds/starts them); it
  never touches docker itself. Carries a `{MISSION}` placeholder.
- `venture/` — a solo-venture operator: a self-driving agent that advances ONE venture tick after tick
  toward a KPI without a human in the loop. Carries a `{MISSION}` placeholder.

A template's `agent.env` may declare extra runtime knobs (e.g. the read-only cloud profile);
`enclave init` merges those on top of the core config it generates.
