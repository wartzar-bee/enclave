# Enclave — roadmap / backlog

Shipped so far: hardened runtime + guard (incl. loader-hijack denylist), `enclave init` wizard +
`run`/`publish`, lean images, wiki memory layer, claude.ai-style chat (image attach, voice in/out,
live model switch), real-time chat plane (`chat_responder`), server-side voice proxy, support/analyst
templates. This file tracks what's next.

> **Hard rule:** never bake an external dependency (npm/pip/repo) into the image or a setup script
> without a security pass first — provenance + pinned version + CVE scan + read the actual code for
> exfil. If a thing is too sprawling to fully review, author our own from the distilled idea instead.
> The items below that pull external code are gated on that pass; they are **designed, not yet wired**.

## #2 — publish prebuilt images to ghcr  (BLOCKED on a token)
Plumbing is done and proven: `enclave publish --registry ghcr.io/<owner>` builds both images, and
`docker login ghcr.io` succeeds with the wartzar-bee token. The **push 403s** —
`permission_denied: token does not match expected scopes`: the available tokens have repo + login but
**not `write:packages`**.
- **Unblock:** devops provisions a token with `write:packages` (classic) or a fine-grained token with
  *Packages: write* for `wartzar-bee`. Then: `echo $TOK | docker login ghcr.io -u wartzar-bee
  --password-stdin && enclave publish --registry ghcr.io/wartzar-bee`.
- Teammates then set `ENCLAVE_AGENT_IMAGE`/`ENCLAVE_CHAT_IMAGE` in `.env` and `enclave run --pull`.
- (Same trust model as cloud creds: packages-write authority is devops-provisioned, not self-service.)

## #4 — containerize qmd (Phase 2c, optional accelerator)
The wiki is the default memory and needs no infra, so qmd is an **accelerator, not a blocker**.
- **Design:** `Dockerfile.qmd` installs `@tobilu/qmd` (PINNED) + builds `better-sqlite3` *in-container*
  (sidesteps the host node-26 ABI gotcha — it's built for the container's node). Runs
  `qmd_gateway.mjs` in HTTP mode (`QMD_GW_HTTP_PORT=18182`, `QMD_DB=/index/index.sqlite`,
  `QMD_ALLOWED_COLLECTIONS` per agent). Compose `qmd` profile, off by default; CPU default + a `gpu`
  profile (`--gpus all`, CUDA on Linux+NVIDIA). Mac containers can't reach Metal → keep shared host qmd.
  Mount a corpus volume + an index volume; a re-embed job rebuilds the index.
- **Gate:** vet `@tobilu/qmd` (npm provenance, pin a version, scan for exfil) before adding the install.
- **First increment (post-vet):** the `Dockerfile.qmd` + compose `qmd` profile, agent `.mcp.json`
  pointed at the in-network gateway.

## #5 — RLM big-context tool (alexzhang13/rlm)
Query-time reasoning over huge context (context-as-variable + recursive sub-calls) exposed as a tool
the agent can call; fits the guard (it's just another tool call).
- **Gate:** vet the repo. Likely **re-implement the distilled pattern ourselves** (like we did for
  `comic-creator` vs the external comic repos) rather than install a research repo wholesale.
- **First increment:** a small `rlm`-style tool in `platform/agentd/` that chunks a large input,
  maps sub-queries to cheap-model calls, and reduces — no external dep.

## #6 — Cognee graph-memory provider
A graph layer behind the existing pluggable memory contract (`docs/MEMORY-PROVIDERS.md`). Opt-in
accelerator only — the wiki stays the default; never a core dependency.
- **Gate:** heavy dependency tree — full security pass; pin; isolate. Avoid as core (AGPL/telemetry
  concerns flagged previously for some memory backends — confirm Cognee's license/telemetry).
- **First increment:** a `MemoryProvider` adapter stub implementing the contract, off by default.

## #7 — ruflo WASM tool sandbox (stronger isolation)
Run tool execution inside a WASM sandbox for defense beyond the container + guard. We already shipped
the cheap, high-value slice (the loader-hijack env denylist in `hooks/guard.py`).
- **Gate:** large; vet ruflo's approach and the WASM runtime dep.
- **First increment:** scope which tool classes benefit (bash/code-exec) and prototype one behind a flag.

## Open decisions
- Per-teammate cloud-cred model → **resolved:** devops provisions a scoped read-only identity on
  request (see `SECURITY.md`).
