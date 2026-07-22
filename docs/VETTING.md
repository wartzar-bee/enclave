# External-dependency vetting log

Every external dependency baked into an image or setup script gets a security pass first
(provenance + pinned version + CVE/exfil scan + read the actual code) per the hard rule in
`docs/ROADMAP.md`. This file records the verdicts.

## `@tobilu/qmd` — 2.5.3 — VERDICT: SAFE (2026-06-16)
Baked into `Dockerfile.qmd` (PINNED to 2.5.3).

| Check | Result |
|---|---|
| Provenance | Tobias Lütke (`tobi`), repo `github.com/tobi/qmd`, **26.6k★**, MIT |
| Install hooks | No `preinstall`/`postinstall`. Only `prepare`, which is git-guarded (`[ -d .git ]`) *and* points at a path absent from the published tarball → no-op on `npm install` |
| Network egress | Only `huggingface.co` (model weights; `hf-mirror.com` fallback). The single `fetch` is a HEAD liveness check before a model download. No telemetry, no unknown hosts |
| Code execution | No `eval` / `new Function` / `child_process` / `spawn` / `execSync`. Every `exec` is sqlite SQL (`db.exec`) |
| Secret access | None — env reads are all `QMD_*` config knobs; zero reads of `.secrets`/`.ssh`/`.aws`/`.npmrc`/tokens |
| Obfuscation | None — no base64/atob/`fromCharCode` |
| Integrity | tarball sha512 `c1429ce2…ccc9e04`; npm dist integrity `sha512-wUKc4pSP…bMyeBA==` |

Method: pulled the registry tarball, inspected the shipped `dist/*.js` + `package.json` directly
(not just the README). Pin stays at 2.5.3; re-vet on bump.

## `@colbymchenry/codegraph` — 1.0.1 — VERDICT: ACCEPTABLE w/ telemetry-OFF (2026-06-16)
Opt-in code-memory accelerator (`Dockerfile.agent` `--build-arg INSTALL_CODEGRAPH=1`; see
`docs/CODE-MEMORY.md`). The agent image bakes `DO_NOT_TRACK=1`.

| Check | Result |
|---|---|
| Provenance / license | `colbymchenry/codegraph`, 33.6k★; npm single-maintainer; ships per-platform bundles (own node + tree-sitter wasm grammars) |
| Install hooks | **None** (no pre/postinstall); shim pulls the platform bundle from npm optionalDeps / GitHub Releases |
| **Telemetry** | **ON by default** upstream → `telemetry.getcodegraph.com/v1/events`, but **counts only** (machine UUID + per-day tool-call counts + *bucketed* file counts + install-target names) — **no source, no paths, no secrets**. Baked **OFF** here via `DO_NOT_TRACK=1` (also `codegraph telemetry off`). |
| Egress (other) | GitHub Releases (bundle) + npm + an upgrade check. No analytics SaaS. |
| Local artifact | `.codegraph/codegraph.db` stores symbol metadata (paths/signatures/docstrings, **not** full source). Keep gitignored over secret-bearing corpora. |

Tested in-container: install → `codegraph init` indexes → `query`/`callers` resolve; telemetry
reports disabled via `DO_NOT_TRACK`. Pin 1.0.1; re-vet on bump (esp. the downloaded bundle).

## `cognee` (#6 graph engine) — 1.1.2 — VERDICT: DO NOT BAKE IN (2026-06-16, re-verified 2026-06-18)
Security pass ran; the adapter stub (`providers/cognee_provider.py`) stays, the engine stays out.
Re-verify (2026-06-18) confirmed + sharpened: latest stable **1.1.2** (do NOT pin `1.2.0.dev0`); the
telemetry endpoint is the vendor host **`https://test.prometh.ai`** (not PostHog), and the API-key
fingerprint is PBKDF2-HMAC-SHA256 (the key isn't sent, but a stable hash of it is). €7.5M-seed, funded,
maintained — legit, but the verdict stands and strengthens. Verdict unchanged: **KEEP GATED.**

| Check | Result |
|---|---|
| Provenance / license | `topoteretes/cognee`, 17.8k★, **Apache-2.0** (the earlier "AGPL" worry was wrong — license is fine) |
| CVEs (OSV, 1.1.2) | 0 known |
| **Dependency sprawl** | **42 core deps, 127 with extras** — pulls a web server (fastapi/gunicorn/starlette), its own vector DB (lancedb/pylance), `instructor`, `rdflib`, `alembic`, `sqlalchemy`, … Far past the "fully reviewable" bar. |
| **Default cloud egress** | Core deps `openai` + `litellm` → defaults to cloud LLM/embedding calls unless explicitly pinned to a local endpoint. |
| **Telemetry — ON BY DEFAULT** | `cognee/shared/utils.py:send_telemetry()` fires on every event **unless `TELEMETRY_DISABLED` is set** (opt-OUT). Sends a **machine-level `persistent_id`** (`~/.cognee/.persistent_id`, explicitly "survives data deletion, reinstalls") + an **API-key-derived tracking hash** + anonymous_id. This is exactly the phone-home a security-first runtime must not ship by default. |

**Decision (per the hard rule — "too sprawling to fully review → author our own from the distilled
idea"):** do NOT install Cognee as an accelerator. The wiki already covers memory; the cognee stub
holds the contract slot. If graph traversal is genuinely needed, **author our own minimal graph
layer** (stdlib + `networkx` + sqlite — each individually reviewable) behind the same contract,
matching the comic-creator precedent. If someone still insists on Cognee, it must run **isolated in
its own container** with `TELEMETRY_DISABLED=1`, `ENV=dev`, a pinned local LLM endpoint, and egress
firewalled — a heavy lift for an opt-in the wiki already replaces.

## `wasmtime` (wasmtime-py, #7 WASM runtime) — 45.0.0 — VERDICT: PROCEED WITH CONDITIONS (2026-06-16, re-verified 2026-06-18)
| Check | Result |
|---|---|
| Provenance / license | `bytecodealliance/wasmtime-py` — **Bytecode Alliance** (the WASI reference org; Mozilla/Fastly/Intel). **Apache-2.0 WITH LLVM-exception** |
| **Runtime dependencies** | **ZERO** (the only 4 `requires_dist` are `testing` extras). Minimal attack surface. |
| Install footprint | Ships **prebuilt platform wheels** (manylinux/musl x86_64+aarch64, macOS, Windows, + pure `py3-none-any`) — **no source compile at install**; native runtime bundled by the org. |
| Telemetry | None observed / not documented (native runtime bundled in the wheel — no install/runtime fetch). |
| CVEs | **0 open against 45.0.0** (2026-05-26). But wasmtime is a high-churn sandbox target: the **2026-04-09 batch had TWO critical (9.0) escapes** — CVE-2026-34987 (Winch) and **CVE-2026-34971 (aarch64 Cranelift — our Apple-Silicon host)**, both fixed by 45.0.0. |

**Decision — PROCEED WITH CONDITIONS** (the *dependency* is cleared; these are live obligations):
1. **Pin `wasmtime==45.0.0`** and **track Bytecode-Alliance advisories** — bump promptly on each batch (the 2026-04-09 double-escape shows staying-current IS the control).
2. **Default Cranelift backend only — never enable Winch** (the worst recent escapes were Winch-specific).
3. **Defense-in-depth only** — keep it nested *inside* the `cap_drop:ALL` container + the guard; grant no network capability, minimal fs preopens. An escape then still lands in the container, not the host.
4. The `run_sandboxed()` executor is the next BUILD step (gated by engineering, not security): per
   `docs/WASM-SANDBOX.md`, WASI can't run arbitrary `bash`, so it needs a restricted exec surface (or an
   interpreter compiled to WASM), not a drop-in shell wrapper.

## `ECC` ("Everything Claude Code", github.com/affaan-m/ECC) — v2.0.0 — VERDICT: BORROW FILE-BY-FILE, DO NOT INSTALL (2026-06-28)
Not a baked dependency — a large MIT framework of agentic-coding methodology/skills/agents/hooks we
mined for ideas. We **re-authored** (did NOT copy) 8 starter skills + verifier subagents from it into
`skills/` + `agents/` (seeded by `bin/enclave init`; the current `agents/` set is code-reviewer,
security-reviewer, silent-failure-hunter, test-writer, ui-reviewer). Precedent: re-author rather than
install — distil the idea, own the file.

| Check | Result |
|---|---|
| Provenance / license | Affaan Mustafa (single maintainer + ~230 contributors), **MIT**, ecc.tools, real upstream (2000+ issues/PRs), mature `SECURITY.md` + a supply-chain IOC scanner |
| Install mechanism | `install.sh`→`scripts/install-apply.js` = file-copy into `~/.claude` only; `npm i` pulls 3 reputable deps (`@iarna/toml`/`ajv`/`sql.js`); ECC postinstall is a harmless `echo`. No remote-code download, no `eval` |
| Exfil / phone-home | **None in core.** All network is opt-in tooling gated on user-supplied tokens (Discord bot, LLM providers, optional MCPs). `ecc_dashboard.py` = local Tkinter, no sockets. No reads of `.ssh`/`.secrets`/keychain to send anywhere |
| Instruction-injection | Clean — scanned 271 skills/67 agents; hits are *defensive* (skills that treat plan/file content as untrusted data). No malicious prompts |
| **Risks (why NOT install wholesale)** | (1) `hooks/hooks.json` = ~25 auto-running, full-priv hooks re-trusted on every upstream pull — poor fit for our no-timer/no-auto rules; (2) `scripts/auto-update.js` = `git pull`+reinstall (never cron it); (3) SaaS-wired pieces: `skills/social-publisher` (getsocialclaw.com), `scripts/hooks/insaits-*` (pip `insa-its`), `.mcp.json` `chrome-devtools-mcp@latest` (unpinned) |

**Decision:** safe to READ + to re-author individual `.md` skills/agents after per-file review. **Never** run `install.sh`/`auto-update.js`, adopt the hook bundle, or copy the SaaS-wired files. The borrowed **skills** carry `origin: ECC-distilled` in their frontmatter
(the `agents/` verifiers ship clean `name/description/tools/model` frontmatter, no origin tag).
