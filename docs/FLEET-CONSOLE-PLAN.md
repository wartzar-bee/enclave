# Enclave Fleet Console — plan v2 (post-critique, 2026-06-18)

> **v2 supersedes v1 (below).** Three critiques (external GPT-5.5, internal adversarial-on-code,
> operator) converged: v1 over-claimed reuse and under-weighted security, scale, and a missing
> port-allocation layer. The studio dashboard is **NOT proven** (operator: untested/weak) — we reuse the
> *working substrate* (comms bridge, web_chat JSON API, agentloop, state files) and **build the console
> fresh**, NOT port the dashboard UI.

## The ONE load-bearing decision (settle before any code)
**A single background "fleet snapshot" thread is the ONLY reader of agent state.** It builds one cached
dict from *direct disk reads* (per-agent state files + comms JSONL, mtime-gated, `since=` cursor) + TCP
probe results + `docker compose ps` states + the port-allocation map. **Every** SSE/poll/CLI/rail read
serves that cache — no request thread ever calls the old `fleet()`/`_comms_events` or touches a backend.
This fixes the O(N)-per-push collapse, homes port + probe state, removes the comms bridge as a hot path,
and makes backpressure trivial (one producer, many cheap consumers).

## Blockers the critiques found (all real, all in v1's blind spots)
- **B1 — SSE collapse at ~20–30 agents:** v1 said "reuse `fleet()` verbatim," but `_stream` calls
  `fleet()` every 2s *per browser* with no cache, and each `_agent_state` does ~10 file reads **+ a
  synchronous comms HTTP call returning the agent's ENTIRE event log** (`since=0`). 100 agents = ~100
  blocking HTTP + ~1000 file reads every 2s per tab. → fixed by the snapshot thread above + `since=`
  cursors + reading comms JSONL off disk.
- **B2 — no port allocation exists:** every deployment defaults `WEB_CHAT_BIND=127.0.0.1:8888`; the
  proxy's "→ :port" premise assumes an allocation scheme **not in the codebase**. → console/`enclave
  fleet up`/init must own a port pool (e.g. 8900–9100) in `ports.json`, write `WEB_CHAT_BIND` before
  `up`, re-derive the map from `docker port` at load. **Net-new, P1.**
- **B3 — the proxy is not a security boundary as drawn:** web_chat serves its HTML page with NO auth and
  binds 0.0.0.0 with `127.0.0.1:<port>` published, so loopback bypasses the console; also it uses
  `X-Chat-Token`, not `Authorization`. → either **unpublish per-agent ports** (internal network; console
  is the ONLY path) or explicitly accept "loopback == trusted" and drop the boundary pretense. Inject the
  correct `X-Chat-Token`. **Decide the trust boundary explicitly.**

## Other required changes
- **Privilege split (external blocker):** running `docker compose` ≈ root. Split an *unprivileged web
  process* from a narrow **`fleetctl` control helper** (Unix socket/subprocess) that owns Docker and
  exposes only allowlisted `start/stop/restart/send_directive(id)` by manifest id — refuses unknown ids +
  arbitrary compose files, **serializes** lifecycle ops (no 100-concurrent-build storm), logs every action.
- **Identity = manifest, discovery = observed state:** an intent **manifest** (`id → compose_file,
  project, dir, home, allocated_port, allowed_actions`) is identity; `docker compose ls/ps --format json`
  is observed state; unknown projects = "unmanaged," not controllable until enrolled. De-dupe by resolved
  ConfigFile path (two folders can both set `name: foo`).
- **Lifecycle addressing (M1):** `docker compose -p <id>` does NOT address these stacks (project name is
  `name:` in the file; stacks live in different folders). Use `docker compose -f <ConfigFile>
  --project-directory <dir> <verb>`; validate the path under an allowlisted stacks root.
- **Status ≠ chat port (M2):** a stack is 2–5 containers; web-chat up ≠ agent ticking. Status = `compose
  ps` service states ∪ agentloop liveness (working/stale from `runner.log`+`events`). TCP probe = proxy
  reachability only. Budget 3–5× containers.
- **Don't proxy full UIs — proxy narrow JSON only:** reverse-proxy ONLY web_chat's `/api/*` JSON endpoints
  (correct `X-Chat-Token`, body-size + socket timeouts + per-agent & global concurrency caps, loopback
  only); render the rail + chat in the console itself. **v1 chat = short-poll** (not proxied 600s
  long-poll); **one console SSE for fleet status only**, with `: ping` heartbeats every ~15s + BrokenPipe
  teardown.
- **Comms bridge bounding (M3):** cursor reads (never `since=0`), retention cap (last ~500/agent) +
  JSONL compaction, console tails JSONL off disk for the rail, only `POST /send` through the bridge.
  Per-agent HMAC identity is the scale-correct hardening (P3 on a single trusted host, but flagged).
- **uid/ownership (M4):** pin a consistent image uid, enforce host-dir ownership in `enclave init`,
  surface write failures (web_chat currently swallows them silently).
- **Audit the proxied path (m3/m4):** proxied `/agent/<id>/api/send` bypasses `send_directive`/audit —
  apply the same Origin + custom-header gate AND audit log to proxied mutating verbs.
- **Backpressure:** `ThreadingHTTPServer` spawns unbounded threads — add a max-inflight semaphore → 503;
  bound log streaming (`logs --tail` + kill on disconnect, no unbounded `-f`); plan disk rotation.

## Revised phasing (security + the missing primitives move EARLIER)
- **P1 — CLI control plane + load-bearing primitives:** manifest, **port allocation**, **stack-file
  addressing**, the **single snapshot thread**, the **`fleetctl` privilege helper**, serialized
  lifecycle. `enclave fleet` list/up/down/restart/logs/send. No web yet.
- **P2 — web console, loopback-enforced + session auth FROM THE START** (auth is NOT deferred): rail +
  cached-snapshot SSE + short-poll JSON-proxied chat + directive box + audit. Bind 127.0.0.1 refused-if-
  non-loopback in code.
- **P3 — peer-messaging timeline/network view, bulk ops (with confirm + target count), comms HMAC
  identity, disk rotation, polish.**

## Operator decisions — RESOLVED (2026-06-18)
1. **Trust boundary → NOT one door.** There's a **management hierarchy**: a *manager* agent (studio-agent
   pattern) steers a few sub-agents, so agents must be reachable by the human console AND their manager
   agent. Keep the **comms bridge as the multi-party steering plane** (human + manager agents → agents;
   agent↔agent); do NOT unpublish ports to force a single door. Console binds 127.0.0.1 (enforced),
   loopback-trusted. The console must **represent the hierarchy** (manager → its sub-agents as a group/
   tree), not just a flat fleet. → first-class concept: an agent's manifest entry may name a `manager`;
   the rail groups sub-agents under their manager; a manager agent can be given authority to direct its
   sub-agents over comms.
2. **UX → rail + detail, NOT a table.** The old dashboard was a table that only ever grew rows/columns —
   rejected as the model. Build the two-pane **agent rail (left) + tabbed detail (right)** from the start.

## (old) Open decisions
1. **Trust boundary (B3):** unpublish per-agent ports so the console is the only door (more secure, a
   compose change to every deployment) — or accept "single trusted host, loopback = trusted" and treat
   the console as convenience-not-perimeter? *(Recommend: loopback-trusted for now + bind-127.0.0.1
   enforced; revisit unpublishing if we ever expose it.)*
2. **What specifically annoyed you about the old dashboard** (layout/speed/what it showed) — so the fresh
   build fixes it by design.

---

# (v1 — original plan, superseded by v2 above)

# Enclave Fleet Console — implementation plan (v1, 2026-06-18)

**Goal:** one web console to manage 20–100 Enclave agents — a left rail of agents (Slack-style),
click one to chat / give a directive; agents run autonomously toward their objectives otherwise;
they can message each other. Start/stop/monitor from the same surface.

**Verdict from research (don't relitigate):** ~80–90% already exists. The studio dashboard
(`platform/dashboard/server.py`, pure stdlib `http.server`+SSE), the **comms bridge** (`:18193`:
`/send /inbox /events /emit /roster`), the **agentloop** (event-driven drain+emit), and the per-agent
**web_chat** plane all port to Enclave. **No framework** (D-052; ruflo/LangGraph already rejected). The
net-new work is: discovery, the reverse-proxy aggregator, the rail UX, compose lifecycle, scale/auth.

---

## Architecture (one stdlib server, two panes)

```
                          ┌─────────────── fleet console (NEW: platform/agentd/fleet.py) ───────────────┐
browser ──auth(session)──▶│  ThreadingHTTPServer, 127.0.0.1   │  left rail: agents+status   │ right: tabs │
                          │  • discovery (compose ls + scan)  │  • reverse-proxy /agent/<id>/* → :port    │
                          │  • bg TCP-probe thread → TTL cache │  • SSE fan-out (one stream/browser)       │
                          └───────────┬───────────────┬───────────────────────┬──────────────────────────┘
                            proxy /agent/<id>/api/* │   │ docker compose up/down/stop (subprocess, allowlisted)
                                                    ▼   ▼
                              each agent's own web_chat (:8888,:8890,…)   +   comms bridge :18193 (peer msgs)
```

- **It aggregates, it does not replace.** Each agent already runs a full `web_chat` (sessions,
  memory-capture, export). The console reverse-proxies `/agent/<id>/api/*` → `127.0.0.1:<port>`,
  reusing every agent's real chat plane. Browser auth terminates at the console; agent tokens never
  reach the browser.
- **Two messaging planes (both exist):** *chat* (real-time Q&A → `chat-inbox.jsonl` → chat_responder)
  for "talk to it"; *directive* (→ comms `/send`, wakes the autonomous tick; inbox.md fallback) for
  "steer it." The rail's composer offers both: a chat box and a one-click "directive" send.
- **Autonomous-otherwise** is already true for `autonomous`-template agents (tick toward `{MISSION}`,
  directive overrides). The console is just the steering surface.

## Components & file plan (all in `businesses/enclave/platform/agentd/`, stdlib only)

1. **`fleet.py`** (new) — the console server. Sections:
   - **Discovery:** `docker compose ls --all --format json` (JSON array of `{Name,Status,ConfigFiles}`)
     ∪ optional folder-scan of a stacks dir (surfaces never-`up`'d deployments). Re-derive every load —
     **no parallel registry to desync** (Dockge's lesson). Per agent, read its `.env` (`WEB_CHAT_BIND`
     port, `BRAIN`, `MODEL`) → an in-memory metadata map.
   - **State:** reuse the studio `_agent_state`/`_thread`/`_comms_events` logic verbatim, repath from
     `platform/agents/<id>` → `<deployment>/home` (mounted at `/agent`; **identical state layout** —
     `state/rollup.md`, `work.json`, `state/events.jsonl`, `logs/runner.log`). Cache per-agent 10–30s.
   - **Reverse proxy:** `_forward()` for all verbs — strip hop-by-hop headers (RFC 7230 §6.1), treat
     `urllib HTTPError` as the response (proxy backend 4xx/5xx transparently), per-event `wfile.flush()`
     for SSE/poll, inject `Authorization` server-side from `.secrets/`. `ThreadingHTTPServer` +
     `daemon_threads` so one slow stream can't starve others.
   - **Live status:** ONE background thread, 3–5s timer, `ThreadPoolExecutor(max_workers=16)`
     TCP-connect sweep (`socket.create_connection(timeout=0.3)`) → TTL-cached snapshot behind a lock.
     **Page loads probe zero backends.** SSE pushes the cached snapshot (one stream/browser) + slow-poll
     fallback. *(Background stdlib loop, no LLM — does not violate the no-Opus-on-a-timer rule.)*
   - **Lifecycle:** `docker compose -p <id> up -d|stop|down` via `subprocess.run([...], timeout=)`,
     **argv list never `shell=True`**, validate `<id>` against `^[a-z0-9][a-z0-9_-]*$`. Slow ops async,
     not inline in a handler. Soft-pause = the existing `state/paused` flag. **Never mount docker.sock.**
   - **Agent↔agent view:** project the comms JSONL (`{ts,from,to,text}`) into timeline / per-pair thread
     / force-graph (vasturiano/force-graph, MIT, canvas — recent-K-events projection, no graph DB).
   - **Auth:** loopback bind; `POST /auth` shared secret from `.secrets/console.env` →
     `secrets.token_urlsafe(32)` session in a process dict; `Set-Cookie HttpOnly; Secure; SameSite=Strict`;
     `hmac.compare_digest`; Origin/Referer check + custom-header requirement on every state-changing POST;
     append-only audit log of every action. **Reject** OAuth/RBAC/JWT/multi-tenant (one trusted operator).
2. **`bin/enclave fleet`** (new CLI subcommand) — `fleet` (table: name/status/brain/model/chat-port/
   last-tick), `fleet up|down|restart|logs|open|send <name|--all>`. The scriptable control plane; the web
   console is the GUI over the same functions.
3. **Frontend** (inlined in `fleet.py`, like `web_chat.py`): two-pane — left rail of agent rows (id +
   cached status dot + unread badge, searchable/filterable), right tabbed detail (Chat / Logs / Status /
   Config / Network). The Chat tab loads the proxied per-agent web_chat UI.
4. **Shared comms bridge** for peer messaging: each agent needs `COMMS_URL` set + `comms-bridge.env`
   mounted (logan-cross already has it). Document a one-bridge-for-the-fleet setup.

## Phasing
- **P1 — CLI control plane** (`enclave fleet` list + up/down/restart/logs/send). Cheap, immediately
  useful, foundation the web reuses. Discovery + lifecycle + state read.
- **P2 — web console**: rail + status dots (bg probe + SSE) + reverse-proxied chat + directive box.
- **P3 — peer-messaging Network view** + bulk ops (multi-select directive) + audit log + auth hardening.

## What reuses vs net-new
| Reuse as-is | Net-new |
|---|---|
| comms bridge, agentloop, chat plane, `_agent_state`/`_thread`, model-picker, approval queue | discovery (compose ls), reverse-proxy `_forward`, the 20–100 rail UX, compose lifecycle, bg-probe+TTL cache, console session-auth |

## Risks / open questions (for critique to attack)
- **Reverse-proxying SSE/long-poll through stdlib** — flush discipline, client-hangup detection; is poll
  simpler than SSE for v1?
- **Scale of state reads** at 100 agents (file I/O per refresh) — caching/pagination sufficient?
- **docker compose ls** misses never-`up`'d deployments (label-derived) — folder-scan needed for v1?
- **Auth** — loopback+SSH-tunnel enough, or is session-cookie+Origin mandatory for v1 given the console
  can stop containers + send directives to credential-holding agents?
- **Comms bridge as a shared single point** for 100 agents — contention/throughput?
- **Container uid vs mounted host dir** writes (logan-cross mount-in-place precedent) at scale.
- **Is the CLI-first phasing right, or does the operator want the web console first?**
