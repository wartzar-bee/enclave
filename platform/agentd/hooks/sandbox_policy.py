#!/usr/bin/env python3
"""
sandbox_policy.py — classify a tool call into a WASM-sandbox tier (roadmap #7, first increment).

This is the PURE, unit-tested policy the (gated) WASM executor will consume. It does NOT run any
sandbox — the WASM runtime is an external dep held behind a security pass (see docs/WASM-SANDBOX.md).
Its job today: decide which tool classes are HIGH-value to sandbox (Bash + agent code-exec) and
emit a routing decision, so a deployment can flip ENCLAVE_WASM_SANDBOX=1 and *measure* what would be
sandboxed before any runtime is wired. The guard consumes `route()` in a non-blocking way.

Tiers:
  exec        Bash / code-exec (python3/node/etc run via Bash) — PRIMARY sandbox target
  fs-write    Write/Edit — would get scoped fs preopens (lower priority; guard already path-checks)
  passthrough everything else (reads, structured tools) — no marginal isolation gain
"""
import os, re

# Bash commands that invoke an interpreter on agent-authored code = code-exec (HIGH value to sandbox)
_CODE_EXEC_RE = re.compile(r"\b(python3?|node|deno|bun|ruby|perl|php|bash|sh|zsh)\b")
_EXEC_TOOLS = {"Bash"}
_FS_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}

HIGH_VALUE_TIERS = ("exec",)   # what we target first (docs/WASM-SANDBOX.md)


def classify(tool_name, tool_input):
    """Return the sandbox tier for a tool call. Pure; no side effects."""
    tool_input = tool_input or {}
    if tool_name in _EXEC_TOOLS:
        return "exec"            # all Bash is the main escape surface (incl. code-exec sub-cases)
    if tool_name in _FS_WRITE_TOOLS:
        return "fs-write"
    return "passthrough"


def is_code_exec(tool_input):
    """True if a Bash command invokes a language interpreter on (likely agent-authored) code."""
    cmd = (tool_input or {}).get("command") or ""
    return bool(_CODE_EXEC_RE.search(cmd))


def route(tool_name, tool_input, enabled=None):
    """The routing decision the WASM executor will act on.

    Returns a dict: {tier, would_sandbox, runtime, note}. `would_sandbox` is True only for HIGH-value
    tiers AND when the flag is enabled. `runtime` is 'none' until a vetted WASM runtime is wired —
    so today this is a measurement/observability decision, never an enforcement one.
    """
    if enabled is None:
        enabled = os.environ.get("ENCLAVE_WASM_SANDBOX", "0") == "1"
    tier = classify(tool_name, tool_input)
    high = tier in HIGH_VALUE_TIERS
    would = bool(enabled and high)
    if not enabled:
        note = "sandbox flag off — passthrough"
    elif high:
        note = ("WOULD route to WASM sandbox (runtime gated on security pass — currently logged, "
                "not enforced; see docs/WASM-SANDBOX.md)")
    else:
        note = "tier not high-value — covered by container + guard + path checks"
    return {"tier": tier, "code_exec": is_code_exec(tool_input) if tier == "exec" else False,
            "would_sandbox": would, "runtime": "none", "note": note}
