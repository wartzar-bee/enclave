# Bridges — giving an agent a capability the container cannot have

An Enclave agent runs in a container: no GPU, no microphone, no logged-in browser, no keychain, no
access to the host's applications. That isolation is the point — it is what makes an autonomous agent
safe to leave running. But an agent that can only think is not much use, and the answer is not to
weaken the container.

A **bridge** is the answer. It is a small HTTP service that runs on the **host**, exposing exactly
one capability, reachable from the container at `host.docker.internal:<port>` and gated by a shared
token. The container gets the capability; it does not get the host.

```
  CONTAINER                                    HOST
  ┌──────────────────────┐                     ┌────────────────────────────────┐
  │  bridge.py           │  ── HTTP + token ── │  host-bridge.py :1819x          │
  │  (stdlib client)     │                     │   └─ the real capability        │
  │  the agent calls this│                     │      (GPU / app / OS tool)      │
  └──────────────────────┘                     └────────────────────────────────┘
        reads .secrets/<name>-bridge.env              same file, written by setup
```

This is not theoretical: Enclave itself ships `qmd` (semantic search), `codegraph` (code memory) and
`gcloud` this way, and six more — browser automation, local speech-to-text, text-to-speech, 3D
rendering — run in the deployment where Enclave is developed. **The pattern below is extracted from
those nine, not designed in advance.**

---

## When a bridge is the right answer

Build one when the capability **genuinely cannot live in the container**:

| Reason | Example |
|---|---|
| Needs hardware the container has no access to | GPU inference, Metal/CUDA, audio devices |
| Needs a real, logged-in application | a browser with warm sessions and cookies |
| Needs to be the human's user | keychain, an authenticated CLI (`gcloud`, `az`) |
| Needs to survive the container | a persistent index or model cache too big to rebuild per run |

**Do NOT build a bridge** when a library, a CLI in the image, or an MCP server would do. A bridge is
the most privileged extension point in this system; reach for it last. If your capability is pure
computation with no host dependency, put it in the image and skip all of this.

---

## The contract

Five things. All nine existing bridges satisfy them, and consumers rely on it.

**1. `GET /health`, unauthenticated, cheap.** Returns `{"ok": true, ...}` plus whether the capability
is *actually usable* — not merely whether the process is alive. Report the dependency: is the model
downloaded, is the CLI installed, is the login still valid. A bridge that reports healthy while its
model file is missing teaches the agent the capability works, and it will keep calling and keep
failing. Probes and dashboards read this endpoint on every agent boot, so keep it fast.

**2. Everything else is POST, JSON in, JSON out, token-gated.** `Authorization: Bearer <token>`.

**3. Errors come back as data.** `{"ok": false, "error": "..."}` with a real status code. Never let an
exception escape to a dropped socket — an agent holding a task cannot distinguish that from a hang,
and it will retry forever.

**4. A container-side client (`bridge.py`), stdlib only.** It runs inside an agent image whose
dependency list you do not control; a client that needs `pip install` is a client that silently does
not work. When the bridge is down it must exit non-zero printing *the exact one-time host command* —
an agent that reads "connection refused" retries; an agent that reads "run this on the host"
escalates correctly and stops burning ticks.

**5. A setup script that installs a process manager.** launchd on macOS (`KeepAlive=true`), a
`systemd --user` unit on Linux (`Restart=always`). A bridge started by hand dies with the terminal or
the reboot, and the agent concludes the capability does not exist. KeepAlive is not polish; it is
what makes the capability real.

### Files

```
tools/<name>/
  host-bridge.py         the service (host)
  bridge.py              the client (container)
  host-bridge-setup.sh   token + process manager + smoke test (host, run once)
  README.md              what it does, how to install, what it costs
```

Start from **`tools/bridge-template/`**. It is a working bridge — install it, call it, confirm the
whole path, then replace the one capability function.

```bash
bash tools/bridge-template/host-bridge-setup.sh   # host, once
python3 tools/bridge-template/bridge.py --health  # container
```

---

## Port registry

Host bridges live in `1818x`. Check before you claim one; two bridges on a port is a confusing
failure — the second dies at boot and the first answers questions meant for it.

| Port | Bridge | Where |
|---|---|---|
| 18181 | qmd (semantic search) | Enclave |
| 18182 | qmd scoped gateway | Enclave |
| 18183 | 3D render | studio |
| 18184 | browser automation | studio |
| 18185 | transcribe (speech-to-text) | studio |
| 18186 | voice (TTS) | studio |
| 18187 | gcloud | Enclave |
| 18188 | slot engine | studio |
| 18190+ | **available** | — |

`codegraph` also uses 18184, but *inside the agent's private compose network* — never published to
the host, so there is no clash. Container-internal services are a different namespace.

---

## Granting a bridge to an agent

**A bridge existing is not the same as an agent having it, and this distinction is the whole
security model.** Reachability is granted deliberately, per agent, by whoever operates the fleet —
never by the bridge and never by the agent.

Two things must both be true:

1. **The client is mounted.** The agent's `docker-compose.override.yml` mounts the directory holding
   `bridge.py` (read-only).
2. **The token is present.** `<deployment>/secrets/<name>-bridge.env` exists for that agent.

Miss either and the agent cannot use the bridge, whatever the bridge can do. That is the intended
default: **capability is granted, not discovered.** An agent should never be able to reach a
capability because it guessed a port.

The corollary is easy to forget: once you grant it, *tell the agent it exists*. A capability nobody
documented is one an agent will escalate to a human as missing — we watched an agent ask the operator
to buy a commercial service while holding a valid token for a bridge that already did the job. Put it
in the bridge's README (mounted with the client) or the agent's own instructions.

---

## Security contract

**A bridge is a deliberate hole in the container boundary.** Code here runs on the host, as the user,
outside every sandbox the agent is under. Anyone reviewing a bridge — especially one they did not
write — should check these:

- **Token-gate every mutating endpoint.** `/health` may be open; nothing else.
- **Validate every path.** `{"path": "../../.ssh/id_rsa"}` is the entire exploit. Resolve the path
  *first*, then require it to sit under a declared root — a check against the unresolved string
  misses symlinks. `_safe_host_path()` in the template does this; copy it.
- **Never shell out to a caller-controlled string.** Build `argv` lists. No `shell=True`.
- **Read only your own secret.** A bridge reads `.secrets/<name>-bridge.env` for its token. Reaching
  into other credential files is how one capability quietly becomes every capability.
- **Declare your scope in the README** — what it touches, what it sends off-box, what it costs.
- **No surprise egress.** If the capability calls a third-party API, say so plainly. A "local
  transcription" bridge that uploads audio is a betrayal of the label, not an implementation detail.

**Installing a bridge is a trust decision, and it belongs to the operator.** There is no registry and
no auto-install, deliberately. Read the code before you run it — the same standard you would apply to
any program you are about to run as yourself, because that is exactly what this is.

---

## Contributing a bridge

Bridges are the natural contribution surface for Enclave: self-contained, no core changes, and each
one makes every agent more capable. Good candidates are capabilities that are broadly useful and
genuinely host-bound — OCR, local image generation, a document/CAD application, a hardware device,
a platform CLI that holds its own login.

If you want to send one:

1. Start from `tools/bridge-template/`. Keep the five contract points.
2. Pick a free port, add it to the registry table above in the same PR.
3. Include the README: what it does, one-time setup, dependencies, cost, and what it sends off-box.
4. Make `/health` prove the *capability*, not the process. This is the single most common mistake.
5. State the platform. Most existing bridges are macOS-first (launchd, Metal); a Linux-capable bridge
   is more valuable, and a macOS-only one should say so rather than fail confusingly.

Keep it to one capability per bridge. The value of this pattern is that a reviewer can read a bridge
end to end in a sitting and decide whether to trust it — a 2000-line bridge doing six things is one
nobody can audit, and an unauditable bridge is one no careful operator will install.
