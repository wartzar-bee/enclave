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

## Gated (NOT installed — security pass required before wiring)
- **Cognee** (#6 graph engine) — heavy dep tree + license/telemetry questions. The adapter stub
  (`providers/cognee_provider.py`) is wired; the engine is not. Confirm license + telemetry, pin,
  isolate before `COGNEE_ENABLED=1`.
- **WASM runtime** (#7, e.g. wasmtime/wasmer + WASI shim) — the policy/hook are wired; the runtime
  dep is not. Vet provenance + the runtime before a `run_sandboxed()` executor.
