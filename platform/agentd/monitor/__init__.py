"""monitor — the enclave fleet health monitor ("Agent SRE").

Detects when an agent is off (reusing diagnostics.py), troubleshoots to a root cause via a runbook
of deterministic playbooks, and — per a configurable per-agent policy — alerts, suggests, or
auto-applies a fix. Runs OFF-OPUS (host-side daemon, pure stdlib + a cheap LLM only for the novel
fallback). The daemon DETECTS and ENQUEUES; control_watcher is the only thing that ACTS.
"""
