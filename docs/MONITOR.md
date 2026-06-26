# Fleet Health Monitor (Agent SRE)

An off-Opus host daemon that watches every agent, detects when one is off, troubleshoots to a root
cause, and — per a configurable per-agent policy — **alerts**, **suggests**, or **auto-fixes**.

It's the active layer on top of the Diagnostics tab: Diagnostics is passive (you open it); the
monitor comes to you. It re-uses the diagnostics anomaly engine for detection, so most of the work
is already done.

## How it runs

`enclave fleet monitor [<control-queue>] [--interval S] [--once] [--dry-run]` — a poll loop
(`platform/agentd/fleet_monitor.py`). Off-Opus by construction: pure-stdlib `diagnostics` + a runbook
of deterministic playbooks; the (D2) novel-error fallback uses a cheap/local LLM only. Install as a
launchd job from `deploy/launchd/org.enclave.monitor.plist`.

**Privilege separation (the keystone):** the monitor DETECTS and ENQUEUES; it never touches docker.
When policy permits an auto-fix it writes a control-spec into the SAME queue `control_watcher` drains;
`control_watcher` (the only docker-capable actor) re-validates and executes. Detect ≠ act.

**Alerts ride the existing channel:** a line is appended to an agent's `state/escalations.log`, which
the dashboard's "⚠ Needs your decision" inbox already renders. No console changes.

## Per-agent policy — `MONITOR_MODE`

Set in `agent.env` (or the dashboard Config tab), per agent, with a fleet default in
`policies/monitor.json`:

| mode | behaviour |
|---|---|
| `off` | not monitored |
| `observe` | record health to state only — no notifications |
| `alert` | write to escalations.log → the inbox (**fleet default**) |
| `suggest` | alert + (D2) a one-click Apply for the recommended fix |
| `autofix` | auto-apply **allowlisted** safe remedies; alert the rest |

A noisy/expected-down agent → `observe` or `off`; a critical one → `alert`; a throwaway → `autofix`.

## Tuning — `policies/monitor.json` (env `ENCLAVE_MONITOR_POLICY`)

Data, not code: per-playbook enable, the `autofix_allowlist`, the fleet `default_mode`, thresholds
(no-tick hours, rate limit). Ships conservative (alert-only, empty allowlist). The studio keeps its
own tuned copy. Host bridges to probe come from `ENCLAVE_DOCTOR_BRIDGES` (same env `/api/doctor` uses).

**Auto-fix safety rule:** autofix is allowed ONLY for liveness/restart actions that return to a
known-good state and are reversible. Anything that changes behaviour, policy, or brain content is
suggest-only — a human decides. Gated by three ANDs (mode=autofix · playbook.safe_to_autofix ·
key∈allowlist) plus never-fix-twice and a rate limit.

## The runbook (D1 seed playbooks)

`platform/agentd/monitor/playbooks.py` — each matches a diagnostics anomaly + a deterministic
signature (grep `runner.log`, check a file/config) and asserts a cause with confidence, gated on
*current* state (not stale logs):

- `memory_path_broken` — `/agent/bin/memory.py` shim missing → recall fails every tick (suggest)
- `delegation_loop` — guard blocking edits while the worker fails verify (suggest `DELEGATION_ENFORCE=off`)
- `context_bloat` — context explosion / prompt creep (suggest trim/compact)
- `container_down` — container exited unexpectedly (restart-capable)
- `up_but_unreachable` — up but chat port dead (restart-capable)
- `stalled` — autonomous agent with no recent tick (restart-capable)
- `bridge_down` — a host bridge is unreachable (fleet-level alert)

## Roadmap

- **D1** (now): deterministic detection + alerts + `MONITOR_MODE` config + dedup/recovery. Alert/suggest only.
- **D2:** cheap-LLM cause/fix for novel anomalies; push (telegram) on crit; one-click Apply (`/api/monitor/apply`).
- **D3:** lifecycle autofix allowlist; learned playbooks (operator-confirmed, human-promoted); a Monitor dashboard tab.

Start with `--dry-run` (or `MONITOR_DRYRUN=1`) to observe with zero side effects while you tune the policy.
