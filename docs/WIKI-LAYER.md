# Wiki layer — the portable, zero-infra default knowledge base

Enclave's default knowledge store is an **LLM-maintained markdown wiki** (Karpathy-style). It needs
no database, no embeddings, no GPU, no host service — it's a folder of markdown the agent reads and
writes. That makes it the **cross-platform floor** that runs in any container on any OS; semantic
engines (qmd / LanceDB / Cognee) are opt-in accelerators *on top* of it (see `MEMORY-PROVIDERS.md`).

## Why a wiki instead of pure RAG
- **Traceable**: every claim links back to an immutable `raw/` source ("manual Graph RAG").
- **Human-readable + Git-diffable**: open it in Obsidian, review it, version it.
- **Stateful**: knowledge compounds across sessions instead of being re-derived per query.
- **Zero infra**: a folder + an LLM. Nothing to install or security-review.
- Sweet spot: personal/team scale (~hundreds of sources); past that, add a retrieval accelerator.

## Layout (in the agent's home, `home/knowledge/`)
```
raw/                    immutable sources (read, never edit)
wiki/concepts/*.md      one idea per page
wiki/entities/*.md      people / orgs / systems
wiki/sources/*.md       one summary per ingested raw source (→ back to raw/)
wiki/syntheses/*.md     cross-cutting rollups
index.md                generated catalog — read FIRST to navigate
log.md                  append-only operation log
WIKI.md                 the schema (written by `wiki.py init`)
```
Each page carries frontmatter: `title, type, created, updated, sources[], related[[..]], confidence, status`.
Pages link with `[[page-stem]]`.

## Division of labor
- **The LLM does the semantic work** — ingest (summarize a source, cascade updates to concept/entity
  pages, link them), query (read `index.md` → follow `[[links]]` → answer with citations).
- **`wiki.py` does the mechanical, verifiable work** — so the model can't drift:
  - `wiki.py init [dir]` — scaffold the layout + schema
  - `wiki.py index [dir]` — rebuild `index.md`
  - `wiki.py new [dir] --type concept --title "…" [--sources raw/x]` — scaffold a page with frontmatter
  - `wiki.py lint [dir]` — report broken `[[links]]`, orphans, stale pages, bad/missing frontmatter (exit 1 only on broken frontmatter)
  - `wiki.py log [dir] "…"` — append to the log
  - `wiki.py graph <op> [page] [--brain]` — traverse the link graph: `backlinks`/`neighbors`/`khop`/`path`/`hubs`/`stats`

`wiki.py` is baked into the image at `/workspace/platform/agentd/wiki.py` (stdlib + pyyaml, cross-platform).

## One linked vault — knowledge + operational memory are ONE graph
The wiki is not a separate silo from the agent's operational memory. Durable knowledge lives in two
complementary substrates under the home, **both** markdown, **both** linked by `[[wikilinks]]`:
- **`knowledge/`** — the curated wiki (concepts/entities/sources/syntheses): the "encyclopedia."
- **`memory/`** + **`skills/`** (via `memory.py`) — operational/episodic memory (facts, decisions,
  lessons) and learned procedures: the "working memory + playbook."

They are **one graph**, not two piles. `memory.py remember … --related <page-stem>` (and `memory.py
link <mem> <page-stem>`) write `related: [[…]]` so a lesson/decision is a **first-class graph node**
that links into `knowledge/`. `wiki.py graph --brain` then traverses **wiki + memory + skills** as a
single vault — `backlinks` finds every memory that cites a concept, `khop` pulls a topic's whole
neighborhood across both, `stats` surfaces orphaned memories to link. This makes the brain navigable
as a graph (not just qmd-searchable), and keeps the "index points, the markdown stores, the accelerator
is disposable" discipline across the *whole* memory, not just the wiki half.

## Durability — secret-safe by design
Memory survives a machine wipe because the vault is **its own git repo** (created by `enclave init`,
independent of the product repo). But free-form agent memory CAN contain a token someone pasted into a
lesson or that landed in `inbox.md`, and **git history is forever** — so durability is **scan-gated**:
- `home/.gitignore` never tracks `secrets/` (the structured cred store), `state/`, `logs/`, `uploads/`.
- **Saved by default, continuously**: the runtime **auto-snapshots after every tick** (`runtime.sh`
  → `vault_snapshot.py`, isolated so it can never abort the loop; set `VAULT_SNAPSHOT=0` to disable).
  `enclave snapshot ["msg"]` does it on demand. Each: stage → **scan for credential patterns** →
  commit only if clean; a hit **blocks the commit** (fail-closed) and names the file to redact. The
  agent can't `git` at all (guard-blocked) — the runtime owns the commit (as the master owns commits).
- A **pre-commit hook** in the vault repo blocks even a *manual* `git commit` containing a credential.
- **Encryption at rest (stronger option)**: `enclave vault-encrypt` writes an AES-256+PBKDF2 archive
  of the brain (key in `secrets/vault.key`, never committed) so an **off-machine copy is ciphertext**,
  not plaintext — covering a scanner miss. `enclave vault-decrypt <blob>` restores it. (Drop-in
  upgrades when installed: `age` / `git-crypt` for transparent per-file at-rest encryption.)

The scan-gate and the encryption share one implementation — `platform/agentd/vault_snapshot.py` — so
the secret patterns have a single source of truth across the CLI and the runtime. Net: memory is saved
by default, a leaked secret can never reach history, and the off-machine copy can be ciphertext.

## How an agent uses it
The agent's `CLAUDE.md` points it at `knowledge/` and the workflow. On a new source: `wiki.py new …`,
write the page, link related pages, `wiki.py index`, `wiki.py log`. On a question: read `index.md`,
follow links, answer with `raw/` citations. Periodically: `wiki.py lint` and fix.
