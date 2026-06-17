# Working folder (`WORK_DIR` → `/work`)

Every Enclave deployment answers one question explicitly: **where does the agent's work actually get
saved, and how does it stay searchable?** Two mounts, two jobs:

| Mount | In-container | Role |
|-------|-------------|------|
| `./home` | `/agent` | The agent's **brain** — `CLAUDE.md`, `memory/`, `skills/`, `inbox.md`, `work.json`, `state/`, `logs/`. A scan-gated git vault. |
| `WORK_DIR` | `/work` | The **project working folder** — the real tree the agent operates on and saves output into (rw). |

Keep them separate: the brain is *how* the agent thinks; the working folder is *what* it works on.

## Configure it

In `.env`:

```sh
WORK_DIR=/Users/you/Dev/your-project    # host path → mounted rw at /work
```

- **Blank / unset** → defaults to `./home/work` (a folder inside the vault). Fine for a self-contained
  agent that just produces documents.
- **A host path** → that real tree *becomes* the agent's working folder. The agent and you see the
  same files; its writes land on the host immediately.

`enclave init` prompts for this (`--work-dir /abs/path` non-interactively) and writes it to `.env`.
Changing it later: edit `.env`, then `docker compose up -d` to recreate the agent with the new mount.

## What the agent can and can't do in `/work`

- **Write / edit files** — yes. Saves persist to the host bind mount instantly. No commit needed for
  the file to exist on disk.
- **`git`** — no. The guard blocks `git` for agents (see `SECURITY.md`); the operator owns commits.
  This is intentional: the agent's writes survive on their own, and you stay in control of history.
- **Read** — yes, the whole tree, unless your secrets hygiene says otherwise (the working tree is the
  agent's, but it's still subject to the guard's foreign-secrets block for `.secrets/`-style paths).

> ⚠ **Mounting a real repo rw exposes whatever is in it to the agent process** — including any
> credentials checked into that tree. That's a property of the repo, not Enclave. If the tree holds
> live secrets, treat the deployment as trusted, scrub the tree, or use a read-only reference mount
> plus a separate writable output folder instead.

## Continuous indexing (fresh recall)

Saved work is only useful if the agent can find it again. Index `/work` so new and changed files
become searchable:

- **Host qmd gateway (this deployment's pattern):** the working tree is registered as one or more
  collections in `~/.config/qmd/index.yml`, and a launchd timer (`org.qmd.reembed`, every 15 min)
  runs `qmd update && qmd embed` to re-embed changes incrementally. The agent reaches it through the
  scoped gateway in `home/.mcp.json`. New files the agent writes are searchable on the next pass.
  Files in a directory not covered by a collection pattern stay invisible (fail-safe) — extend the
  patterns to index a new area.
- **Containerized qmd (portable):** point the compose `qmd` profile's corpus at `/work` and run
  `QMD_MODE=reembed` on a schedule. See `docs/MEMORY-PROVIDERS.md`.
- **codegraph (code structure):** for a code repo, the `codegraph` service indexes symbols/refs over
  the same corpus. See `docs/CODE-MEMORY.md`.

The invariant: **work is saved in `/work`; the index points at `/work`; recall stays fresh
automatically.** No manual re-index step in the agent's loop.
