# WASM tool sandbox (defense-in-depth) — scope + first increment

Status: **scoped + flagged hook landed; the WASM runtime is gated.** This is roadmap item #7
(inspired by ruflo's WASM tool isolation). We already shipped the cheap, high-value slice of that
idea — the loader-hijack env denylist in `platform/agentd/hooks/guard.py`. This doc scopes the larger piece and
records what landed now vs what stays gated.

## Why (threat model)
Enclave already isolates with **two layers**: a per-agent container (`cap_drop: ALL`,
`no-new-privileges`, read-only scoped secret mount) and the **PreToolUse guard** (`platform/agentd/hooks/guard.py`,
fires even under `--dangerously-skip-permissions`). Both are coarse: the guard makes an allow/deny
decision on a *whole* tool call, and the container is the blast radius for *everything* the agent
runs. The residual risk is a tool call the guard *allows* but whose payload still does something
unwanted **inside** the container — e.g. an agent-authored script that reaches the network, reads
broadly across the workspace, or burns CPU. A WASM sandbox adds a **third, fine-grained layer**:
run the risky tool classes inside a capability-scoped WASM runtime (explicit, per-call grants for
filesystem preopens + no ambient network/syscalls), so even an allowed call executes with least
privilege.

## Which tool classes benefit (the scope)
| Tool class | Sandbox value | Why |
|---|---|---|
| **`Bash` / shell** | **HIGH** | Arbitrary code + network + fs; the main escape surface. Primary target. |
| **code-exec** (agent runs `python3 x.py` / `node y.js`) | **HIGH** | Same as Bash — untrusted agent-authored code. |
| `Write`/`Edit` (fs writes) | MEDIUM | Already path-checked by the guard; WASM preopens would scope *where* writes land. |
| `Read`/`glob`/`grep` | LOW | Read-only, already mount-scoped; little marginal gain. |
| `qmd`/`rlm`/`escalate` | LOW | Structured, our own code, network already scoped. |

**Decision: target the two HIGH classes (Bash + code-exec) first.** The others are adequately
covered by the existing container + guard + path checks; sandboxing them is lower priority.

## What landed now (this increment)
- **`platform/agentd/hooks/sandbox_policy.py`** — a pure, deterministic classifier:
  `classify(tool_name, tool_input)` → a sandbox tier (`exec` / `fs-write` / `passthrough`) and
  `route(...)` → a routing decision dict (`tier`, `code_exec`, `would_sandbox`, `runtime`, `note`).
  This is the policy the future WASM executor consumes; it is real and runs today.
- **A flagged, non-blocking hook in `platform/agentd/hooks/guard.py`**: when `ENCLAVE_WASM_SANDBOX=1`, the guard records
  the routing decision for HIGH-class calls to `state/sandbox-routing.log` (observability — "what
  *would* be sandboxed") **without** changing allow/deny. Off by default → zero behavior change.

This lets a deployment turn the flag on and *measure* what its agent would route to a sandbox
before any runtime is wired — and gives the WASM executor a single, tested integration point.

## What stays gated (NOT wired)
The WASM **runtime itself** (e.g. `wasmtime`/`wasmer` + a WASI shim, or ruflo's approach) is an
external dependency. Per the hard rule it does not get baked in until a full security pass:
provenance + pinned version + CVE scan + read the actual code. Likely path, once vetted: a
`run_sandboxed(tool_name, tool_input)` executor that compiles the call into a WASI module with
explicit fs preopens and no network, behind the same `ENCLAVE_WASM_SANDBOX` flag — at which point
the flag flips from "log the decision" to "enforce it." The policy/hook above do not change; only
the executor is added.

## Open questions for the runtime pass
- WASI maturity for general `bash` (vs compiling specific interpreters to WASM) — may need a
  restricted exec surface rather than arbitrary shell.
- Performance overhead per call; cache compiled modules.
- How fs preopens map onto the agent's `/agent` home + `/workspace` layout.
