# Quickstart — your first sandboxed agent in 5 minutes

Enclave runs an autonomous agent inside a Docker container with scoped credentials, a
local web chat, and a guard that blocks writes/deploys/foreign secrets it wasn't given.
This walks you from a fresh clone to a running, purpose-built agent you can talk to.

> **Time-to-first-run is the point.** Every command below is real (`./bin/enclave <cmd> --help`).
> The first `run` builds two small images (~5 min once); after that, start/stop is seconds.

## 0. Prerequisites (30s)

- **Docker** (Desktop or Engine) — **running**. `enclave run`/`console` check this and tell you if not.
- **macOS (Apple silicon) or Linux.** Docker-dependent. (See [Known gaps](../README.md#known-gaps-honest).)
- A **model credential** — an Anthropic API key or a Claude CLI OAuth token. You paste it once;
  it lands in `secrets/` (read-only to the container, never committed).

```bash
git clone https://github.com/wartzar-bee/enclave.git
cd enclave
```

## 1. Pick a template (30s)

Don't start from a blank agent — start from one built for a job. Nine ship in-box:

| Template        | What it does                                                              | Put in `inbox.md` |
|-----------------|--------------------------------------------------------------------------|-------------------|
| `research`      | Deep web research: fan-out search → primary sources → adversarial verify → cited report. **Reports; never acts.** | a research question |
| `code-review`   | Read-only reviewer: gathers a diff, fans out security/correctness/quality/perf, gates every finding to a `file:line`. **Reports; never edits.** | a repo path or diff to review |
| `data-pipeline` | Recurring ETL: extract → validate at the boundary → transform → **stage** the load (`PREPARE→FIRE`, never a destructive cloud write). | the pipeline spec (source/dest/transform) |
| `analyst`       | Research/data analyst with **read-only** cloud/data queries; evidence-based brief with citations. | a question to investigate |
| `ops`           | Generic operations agent: answers from its knowledge + read-only live queries. | an ops question |
| `support`       | Customer/user support: answers from a knowledge base, **drafts** replies for human approval; never sends. | a user message |
| `autonomous`    | Self-driving worker: each tick reconstructs state and advances a `{MISSION}` you set — no human in the loop. | the mission / next task |
| `orchestrator`  | Manager: runs its own mission AND can graduate new sub-agents into their own deployments. | the mission |
| `venture`       | Solo-venture operator: advances ONE venture toward a KPI, tick after tick. | the venture goal |

See each template's `CLAUDE.md` for its full operating rules: [`templates/`](../templates/README.md).

## 2. Scaffold it (30s)

Interactive wizard (asks name, brain, model, port, pastes your credential):

```bash
./bin/enclave init --template research
```

…or one non-interactive line (CI-friendly). Every flag below is real; this exits `0`:

```bash
./bin/enclave init --template research --yes \
  --name my-researcher --brain claude --model claude-sonnet-4-6 --cred "$ANTHROPIC_TOKEN"
```

`init` populates `./home` (the agent's mounted `/agent`), `./secrets` (read-only creds), and
`./.env`, and makes `./home` its own **scan-gated git vault** so the agent's memory survives a
machine wipe (secrets are excluded, fail-closed).

## 3. Give it a task (30s)

The agent reads `home/inbox.md` on each wake. For the research agent, that's your question:

```bash
echo "What are the token-cost tradeoffs of vector RAG vs. long-context prompting in 2026?" \
  > home/inbox.md
```

(Or use `./bin/enclave send "<your question>"` once it's running.)

## 4. Run it & talk to it (2 min first time, seconds after)

```bash
./bin/enclave run     # builds the images if needed, starts the container, opens the chat
```

The chat opens in your browser (claude.ai-style). You can also drive it from the terminal:

```bash
./bin/enclave chat        # interactive terminal chat
./bin/enclave status      # health + recent activity
./bin/enclave logs        # tail the runner log
./bin/enclave send "..."  # push another message to its inbox
./bin/enclave stop        # stop the containers (memory persists)
```

The agent works its task, writes a reply to `state/chat-reply.md` (surfaced in the chat), and
records durable learnings into its `home/` brain. A research agent, for example, returns an
answer-first, cited report with a Sources section — and it only ever made read-only HTTP GETs;
the guard blocks everything else.

## 5. Run more than one

Each deployment is a folder. Scaffold siblings that share the built image (no rebuild):

```bash
./bin/enclave new my-reviewer --image-only   # → ../my-reviewer/, its own free port
cd ../my-reviewer && ./bin/enclave init --template code-review --yes --name my-reviewer \
  --brain claude --model claude-sonnet-4-6 --cred "$ANTHROPIC_TOKEN" && ./bin/enclave run
```

See the whole fleet at a glance:

```bash
./bin/enclave fleet list     # status / brain / model / port / open-work / liveness
./bin/enclave console        # web console: all agents in one panel
```

---

## Why it's safe (verifiable, not trusted)

Every agent runs behind a `PreToolUse` guard hook that blocks `git` writes, foreign secrets, and
destructive/cloud-write ops — read the code, don't trust us:
[Why it's safe](../README.md#why-its-safe-verifiable-by-reading-code-not-trusting-us).
The `research`, `code-review`, `support`, and `analyst` templates are **report-only** by
construction; `data-pipeline` **stages** external loads as a ready-to-fire artifact rather than
executing them (`PREPARE→FIRE`).

## Where to go next

- **[Templates gallery](../templates/README.md)** — every template's mission + operating rules.
- **[Cost discipline](../README.md#cost-discipline-run-a-fleet-without-burning-the-model-cap)** — run a fleet without burning your model cap.
- **[Memory](../README.md#memory--one-linked-durable-secret-safe-brain)** — the one linked, durable, secret-safe brain.
- **Part of the [wartzar-bee toolkit](../README.md#part-of-the-wartzar-bee-toolkit)** — `tokenscope` (measure agent token cost) + `ci-guardrail` (gate cost regressions in CI).
