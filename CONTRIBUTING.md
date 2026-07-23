# Contributing to Enclave

Thanks for looking at Enclave. It's a **public alpha** (Apache-2.0) — the API and layout still move,
so small, focused changes land fastest. This file tells you where to start, how to run the tests, and
what a good report or PR looks like.

## Where to start

The highest-value contribution is a **bridge** — a host capability (browser automation, transcription,
TTS, search, …) an agent calls through a narrow, audited surface *outside* the container. Enclave ships
the pattern, not the services. Start from [`docs/BRIDGES.md`](docs/BRIDGES.md) and the working
[`tools/bridge-template/`](tools/bridge-template/); keep the surface least-privilege — that's the whole
point.

Not sure where to jump in? Check the [`good first issue`](https://github.com/wartzar-bee/enclave/labels/good%20first%20issue)
and [`help wanted`](https://github.com/wartzar-bee/enclave/labels/help%20wanted) labels. Two open,
well-scoped starters right now: **verifying Enclave on Windows/WSL2** (untested — see the "Known gaps"
in the [README](README.md#known-gaps-honest)) and **wiring the vetted `wasmtime` runtime into a tool
executor** behind the existing flag ([`docs/WASM-SANDBOX.md`](docs/WASM-SANDBOX.md)).

## Set up a dev environment

Requirements (same as running it — see the [README](README.md#requirements)): **Docker** running,
**Python 3**, **Git**, and a brain credential (`BRAIN=claude` → the `claude` CLI + a subscription;
`api` → an OpenAI-compatible key; `local` → a model server on the host).

```bash
git clone https://github.com/wartzar-bee/enclave.git enclave && cd enclave
./bin/enclave init      # wizard: name, brain, model, port, paste your credential
./bin/enclave run       # build + start, opens the chat at http://127.0.0.1:8888/
```

Heads-up on platforms: development happens on **macOS (Apple silicon) and Linux**. Windows is untested
(WSL2 is the likely path, unverified) — if you get it working, a report is a real contribution.

## Run the tests

The suite is **dependency-free** — plain `python3` scripts, no pytest — so it runs anywhere Python does,
and CI (`.github/workflows/tests.yml`) runs the same runner on every push/PR.

```bash
bash platform/agentd/run_tests.sh            # all suites; non-zero exit if any fail
bash platform/agentd/run_tests.sh -k console # only suites whose filename matches "console"
python3 platform/agentd/test_<name>.py       # a single suite
```

Please run the runner before opening a PR and confirm it exits 0. See
[`docs/TESTING.md`](docs/TESTING.md) for the full suite map and the opt-in live/E2E flags.

## Filing a good bug report

Include, whenever you can:

- The output of **`./bin/enclave status`**
- The relevant lines from **`home/logs/runner.log`**
- Exact steps and the exact error

A report with `enclave status` + a log excerpt is worth ten without them.

## Opening a pull request

- Keep it focused — one change per PR; small diffs review fastest on a moving alpha.
- Match the surrounding code's style and idiom.
- Add or update a test when you change behaviour; make sure `run_tests.sh` exits 0.
- Update the relevant doc (`README.md` / `docs/`) when you change a command, path, or flag — a doc that
  describes something the code no longer does is worse than a gap.
- **Security-sensitive** work (the guard, egress policy, secrets, the sandbox boundary): read
  [`SECURITY.md`](SECURITY.md) first, and if you think you've found a vulnerability, follow the
  disclosure process there rather than opening a public issue.

## Licence

Enclave is Apache-2.0 (see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE)). By contributing, you agree your
contributions are licensed under the same terms.
