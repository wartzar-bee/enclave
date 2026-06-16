# Enclave — roadmap / backlog

Shipped: hardened runtime + guard (incl. loader-hijack denylist), `enclave init` wizard +
`run`/`publish`, lean images, **prebuilt images on ghcr**, wiki memory layer, claude.ai-style chat
(image attach, voice in/out, live model switch), real-time chat plane (`chat_responder`), server-side
voice proxy, support/analyst templates, **containerized qmd accelerator**, **RLM big-context tool**,
**Cognee graph-provider adapter stub**, **WASM-sandbox scope + flagged routing hook**.

> **Hard rule:** never bake an external dependency (npm/pip/repo) into the image or a setup script
> without a security pass first — provenance + pinned version + CVE scan + read the actual code for
> exfil. If a thing is too sprawling to fully review, author our own from the distilled idea instead.

## ✅ #2 — publish prebuilt images to ghcr  (DONE 2026-06-16)
`enclave publish --registry ghcr.io/wartzar-bee` builds + pushes both images. The block was a
token *type*: ghcr only accepts a **classic** PAT with `write:packages` (fine-grained PATs can't
push packages at all). Operator minted the `studio-3rdparty` classic token (scopes `repo,
write:packages`); stored at `.secrets/ghcr.env` (in the studio, gitignored). Live:
- `ghcr.io/wartzar-bee/enclave-agent:latest` · `ghcr.io/wartzar-bee/enclave-chat:latest` (private).
- Teammates: set `ENCLAVE_AGENT_IMAGE`/`ENCLAVE_CHAT_IMAGE` in `.env`, then `enclave run --pull`
  (needs `read:packages`, or flip the packages public on their GitHub pages).
- **Follow-up:** images are `linux/arm64` only (built on the Mac). For x86 teammates, add a
  multi-arch buildx push (`--platform linux/amd64,linux/arm64`). Pending operator OK on build time.

## ✅ #4 — containerize qmd  (DONE 2026-06-16)
`Dockerfile.qmd` installs PINNED **`@tobilu/qmd@2.5.3`** (vetted — see `docs/VETTING.md`) and builds
`better-sqlite3` *in-container* against the image's node, so the host node-26 ABI gotcha can't bite.
Runs `qmd_gateway.mjs` in HTTP mode on `:18182` (fail-closed on `QMD_ALLOWED_COLLECTIONS`, bind host
made configurable via `QMD_GW_HTTP_HOST`). Compose `qmd` profile (off by default) mounts the wiki as
read-only corpus + a named index volume; `QMD_MODE=reembed` rebuilds the index; CPU default
(`QMD_FORCE_CPU=1`) with a GPU path for Linux+NVIDIA. Tested: fail-closed + `/health` + serve.
- Enable: `docker compose --profile qmd up -d --build qmd` → point `home/.mcp.json` at
  `http://qmd:18182/mcp`. (Mac containers can't reach Metal → host qmd stays an option.)

## ✅ #5 — RLM big-context tool  (DONE 2026-06-16)
`platform/agentd/rlm.py` — our own impl (no external dep): chunk a huge input → MAP a sub-query over
each chunk on the cheap local brain → recursively tree-REDUCE to one answer. Wired as the `rlm` tool
in `local_agent` (EXECUTORS + HARNESS). Degrades to extractive keyword hits when offline. Tested on a
189k-char input (17 chunks → planted facts surfaced + stitched).

## ✅ #6 — Cognee graph-memory provider  (DONE 2026-06-16 — adapter stub, off by default)
`platform/agentd/providers/cognee_provider.py` — implements the full memory contract
(query/get/multi_get/ingest/lint/status) with the same per-agent allowlist; `get`/`multi_get` return
the wiki markdown today; `query` returns wiki keyword hits + a "graph not provisioned" notice. JSON-RPC
plug-point (`--http`) for `.mcp.json`. Registered in `docs/MEMORY-PROVIDERS.md`. Tested (contract +
allowlist deny + fail-closed + HTTP). **The Cognee engine stays gated** (license/telemetry + heavy
tree) — flip `COGNEE_ENABLED=1` only after a security pass; the interface won't change, only `query`.

## ✅ #7 — WASM tool sandbox  (DONE 2026-06-16 — scope + flagged hook; runtime gated)
`docs/WASM-SANDBOX.md` scopes the threat model + tool classes (Bash/code-exec = HIGH value).
`platform/agentd/hooks/sandbox_policy.py` is the pure, tested classifier; `guard.py` consumes it
behind `ENCLAVE_WASM_SANDBOX=1` to **log** what would be sandboxed (non-blocking, off by default).
The WASM **runtime** dep stays gated — when vetted, a `run_sandboxed()` executor flips the flag from
log to enforce; the policy/hook don't change. Tested (classifier + flag on/off + denials still deny).

## Open decisions / follow-ups
- Multi-arch image push for #2 (x86 teammates) — pending operator OK on build time.
- WASM runtime security pass (#7) and Cognee engine security pass (#6) — both gated on vetting.
- Per-teammate cloud-cred model → **resolved:** devops provisions a scoped read-only identity (see
  `SECURITY.md`).
