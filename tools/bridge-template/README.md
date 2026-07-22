# bridge-template — copy this to build a bridge

A **working** bridge that exposes one trivial capability (`/sysinfo`). Install it, call it from a
container, confirm the whole path, then replace the capability with your own.

Full pattern, contract, port registry and security rules: **[`docs/BRIDGES.md`](../../docs/BRIDGES.md)**.

## Try it

```bash
# host, once — generates a token, installs a KeepAlive launchd agent, smoke-tests /health
bash tools/bridge-template/host-bridge-setup.sh

# container
python3 tools/bridge-template/bridge.py --health
python3 tools/bridge-template/bridge.py --sysinfo --echo "hello"
```

To run it in the foreground without installing anything:

```bash
TEMPLATE_BRIDGE_PORT=18199 TEMPLATE_BRIDGE_TOKEN=dev python3 tools/bridge-template/host-bridge.py
```

## Make it yours

1. Pick a name and a free port — check the registry in `docs/BRIDGES.md` first.
2. Replace `_sysinfo()` with your capability and `/sysinfo` with your endpoint.
3. Make `/health` report whether the capability is **usable** (model present? CLI installed? login
   valid?), not just whether the process is alive. This is the most common mistake and the one that
   wastes the most agent time: an agent trusts `/health`, and a bridge that reports healthy while
   broken will be called over and over.
4. Copy `host-bridge-setup.sh`, adjust label/port/paths, install your dependencies in step 2 of that
   script and fail loudly if they are missing.
5. Write a README saying what it does, what it costs, and what it sends off-box.

## The three things that will bite you

- **Path traversal.** If you accept a path, use `_safe_host_path()`. `os.path.join(ROOT, p)` accepts
  `../../.ssh/id_rsa` quite happily. Resolve first, then compare against the root — checking the
  string before resolution misses symlinks.
- **Silent death.** Without a process manager the bridge dies with the terminal or the reboot, and
  the agent concludes the capability does not exist and routes around it.
- **Unhelpful failure.** When the bridge is down, the client must print the exact host command that
  fixes it. "Connection refused" makes an agent retry; a named fix makes it escalate correctly.

## What the token is for

`host-bridge-setup.sh` writes `.secrets/template-bridge.env` (chmod 600, gitignored). The container
reads the same file through its read-only `.secrets` mount, which is why both halves agree without
the token ever being baked into an image or passed on a command line.

An agent that has not been granted that file cannot call the bridge — **capability is granted, not
discovered.** See "Granting a bridge to an agent" in `docs/BRIDGES.md`.
