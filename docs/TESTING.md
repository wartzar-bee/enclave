# Testing the Enclave framework

The suite is **dependency-free** — plain `python3` scripts, no pytest (the baked agent image ships
python3 with no test deps). Every `test_*.py` exits non-zero on failure. One runner ties them together
and CI runs the same thing.

## Run everything

```bash
bash platform/agentd/run_tests.sh            # all suites; non-zero exit if any fail
bash platform/agentd/run_tests.sh -k console # only suites whose filename matches "console"
ENCLAVE_E2E=1 bash platform/agentd/run_tests.sh   # force the Playwright E2E (else it self-skips)
```

A single suite: `python3 platform/agentd/test_<name>.py`.

CI: `.github/workflows/tests.yml` runs the runner on push/PR (Python + Node for `node --check`;
the browser E2E self-skips when no browser is present, so it never blocks CI).

## What's covered

| Suite | Surface |
|---|---|
| `test_console_helpers.py` | console.py pure helpers (uptime parse, cap read, alert thresholds, fleet graph, per-agent activity, recent-commits, operator-stopped marker) |
| `test_console_api.py` | the dashboard's HTTP API — every GET endpoint shape, bad-id/traversal 400s, token gate, POST CSRF/origin/validation, create-queue contract |
| `test_console_e2e.py` | Playwright headless: renders every nav view + per-agent tab + create-modal brain/provider switching, asserts zero console errors (self-skips w/o browser) |
| `test_frontend_static.py` | `node --check` on the embedded dashboard JS + structural anchors + **frontend→backend endpoint drift** check |
| `test_chat_responder.py` | the chat plane — guards the two regressions (`CHAT_SYSTEM` defined; `BRAIN_API_KEY_ENV` key resolution) + CLAUDE.md injection + pure helpers |
| `test_fleet.py` | fleet.py disk/pure surface (`_SAFE`, `_env`, `_scan_deployments` skip-rules, `snapshot()` down-marking + kind classification) |
| `test_control_watcher.py` | control-spec parse/dispatch, malformed-spec rejection, queue lifecycle (executor mocked — never touches docker) |
| `test_spawn_watcher.py` | agent-create spec → build/run argv, unsafe-name refusal, secrets staging |
| `test_live_lifecycle.py` | **opt-in live E2E** (`ENCLAVE_LIVE=1`) — drives the REAL running console + REAL docker: create a throwaway agent from scratch → spawn-watcher builds + starts it → config edit force-recreates → down → up → restart → teardown, asserting docker + the operator-stopped marker at each step. Self-skips in CI / default runs. |
| `test_diagnostics.py` `test_monitor.py` `test_fleet_config.py` `test_route_tier.py` `test_local_agent.py` `test_memory_skill_loop.py` `hooks/test_compactor.py` | pre-existing module units |

A coverage guard: every `/api/*` path in `console.py`'s `do_GET`/`do_POST` is referenced by at least one
suite (audit: `comm -23 <handled> <referenced>` is empty). Re-run it when you add an endpoint.

## Writing a new test

Use the shared harness `tests_fixtures.py` (do not bake env-specific assumptions into a suite):

```python
import tests_fixtures as F
check = F.Check()

root = F.build_fleet(specs=[{"id": "alpha"}, {"id": "beta", "brain": "api", "model": "qwen"}])
con, base, stop = F.boot_console(root)          # real console on an ephemeral port, NO docker needed
try:
    code, body = F.get(base, "/api/fleet")
    check("fleet ok", code == 200 and set(body["agents"]) == {"alpha", "beta"})
finally:
    stop()
raise SystemExit(check.report())
```

Design rules that keep the suite trustworthy:
- **Hermetic.** `boot_console(hermetic=True)` neutralizes the docker CLI so the fixture deployments are
  the *only* agents discovered — the suite gives the same result on a laptop with a live fleet and on a
  bare CI runner. Watcher tests monkeypatch the `subprocess.run` execution seam: a test must **never**
  start/stop a real container.
- **Fixtures mirror production shape.** e.g. `usage.jsonl` timestamps are ISO-8601 `Z` strings (what
  agents write and what `usage._parse_ts` / the dashboard JS expect) — a numeric epoch would silently
  parse to nothing.
- **Tests document behavior; they don't change it.** If a test reveals a real bug, fix the bug in a
  separate change — don't weaken the assertion.

## Bug found by live testing (fixed)

`enclave fleet watch` was refactored to dispatch to `spawn_watcher.py` (create-specs) with a separate
`control-watch` for lifecycle, but the host's `org.enclave.spawn` launchd daemon was still running the
*pre-refactor* process (old `fleet.py watch`, control-watcher semantics). So **every dashboard
agent-creation was silently rejected** (`unknown action ''`) and the spec moved to `failed/`. The
hermetic suite can't catch this — it mocks the executor — which is exactly why `test_live_lifecycle.py`
exists. Fix was operational: `launchctl kickstart -k gui/$UID/org.enclave.spawn` so the daemon reloads
current code. **Lesson:** after changing `bin/enclave`/watcher code, restart the spawn + control launchd
jobs (or they keep serving stale logic). Worth a healthcheck that asserts the running watcher is the
current `spawn_watcher`.

## Known finding (not a bug)

The operator-stopped safety gate (`state/.operator-stopped`, which must stop autofix from restarting a
deliberately-stopped pod) is enforced **only at enqueue time in `fleet_monitor._maybe_autofix`** — the
`control_watcher` executor itself does not re-check it. A spec reaching the control queue from any other
producer would be executed. Single-point, not defense-in-depth; a second check in
`control_watcher._process` would make the invariant hold regardless of producer.
