"""Unit tests for spawn_watcher.py — the host-side executor that turns a dropped agent-creation spec into
`enclave new --image-only --spec` + `enclave run` (the manager-spawns-agents privilege-separated actor).

Hermetic: NEVER runs docker / `enclave` / fleet.py. The execution seam is `spawn_watcher.subprocess.run`,
monkeypatched with a recorder that captures the argv and returns a fake completed-process. The audit log
is redirected to a temp file. Queue + stacks-root are temp dirs, so it is safe to run on a host with a
live fleet — no real deployment is touched.

Covers: name parsing (_load_name + stem fallback), the full _process round-trip (build the `enclave new`
then `enclave run` argv from a valid spec, drain the queue to processed/), unsafe-name rejection,
path-escape / existing-target guards, the two-stage failure handling (new fails -> run not called; run
fails -> spec failed), and the secrets-staging side effect (staged *.env copied to target/secrets then
the staging dir consumed).

Run: python3 test_spawn_watcher.py
"""
import json
import pathlib
import sys
import tempfile
import types

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F
import spawn_watcher as SW

check = F.Check()


class Recorder:
    """Stand-in for subprocess.run: records argv, returns a fake CompletedProcess. `rcs` lets the new vs
    run calls return different return codes (popped in order)."""

    def __init__(self, rcs=None, returncode=0, stderr="", stdout=""):
        self.calls = []
        self.rcs = list(rcs) if rcs is not None else None
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        rc = self.rcs.pop(0) if self.rcs else self.returncode
        return types.SimpleNamespace(returncode=rc, stderr=self.stderr, stdout=self.stdout)

    def subcmd(self, i):
        """The enclave subcommand of the i-th call (argv == [python, enclave_path, <subcmd>, ...])."""
        return self.calls[i][2] if len(self.calls) > i and len(self.calls[i]) > 2 else None


def _mk_queue(tmp):
    q = pathlib.Path(tmp)
    for sub in ("incoming", "processed", "failed"):
        (q / sub).mkdir(parents=True, exist_ok=True)
    return q


def _drop(queue, fname, payload):
    p = queue / "incoming" / fname
    p.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    return p


def main():
    root = pathlib.Path(tempfile.mkdtemp(prefix="sw-test-"))
    SW.AUDIT = root / "audit.log"      # never touch the real ~/.config/enclave audit log

    # ---------------------------------------------------------------- pure helpers: SAFE / _load_name
    check("SAFE accepts a normal name", bool(SW.SAFE.match("data-worker")))
    check("SAFE rejects leading dash", not SW.SAFE.match("-x"))
    check("SAFE rejects slash / path-escape", not SW.SAFE.match("a/b") and not SW.SAFE.match("../x"))
    check("SAFE rejects spaces", not SW.SAFE.match("bad name"))
    check("SAFE rejects empty", not SW.SAFE.match(""))

    q = _mk_queue(root / "q_name")
    p = _drop(q, "filestem.json", {"name": "real-name", "image": "enclave-agent"})
    check.eq("_load_name reads the spec name", SW._load_name(p), "real-name")
    p = _drop(q, "stemonly.json", {"image": "enclave-agent"})   # no name key
    check.eq("_load_name falls back to file stem", SW._load_name(p), "stemonly")
    p = _drop(q, "badjson.json", "{not : json")
    check.eq("_load_name bad json -> stem (no crash)", SW._load_name(p), "badjson")
    p = _drop(q, "listspec.json", [1, 2])
    check.eq("_load_name non-dict json -> stem", SW._load_name(p), "listspec")

    # ---------------------------------------------------------------- _process: happy path
    q = _mk_queue(root / "q_ok")
    stacks = root / "stacks_ok"; stacks.mkdir()
    rec = Recorder(returncode=0); SW.subprocess.run = rec
    sp = _drop(q, "newagent.json", {"name": "newagent", "image": "enclave-agent"})
    SW._process(sp, stacks, q)
    check("process ok: two executor calls (new + run)", len(rec.calls) == 2)
    check("process ok: first call is `enclave new`", rec.subcmd(0) == "new")
    check("process ok: second call is `enclave run`", rec.subcmd(1) == "run")
    target = (stacks / "newagent").resolve()
    check("process ok: `new` targets <stacks>/<name>",
          "--dir" in rec.calls[0] and rec.calls[0][rec.calls[0].index("--dir") + 1] == str(target))
    check("process ok: `new` passes name + --image-only + --spec + --yes",
          "newagent" in rec.calls[0] and "--image-only" in rec.calls[0]
          and "--spec" in rec.calls[0] and "--yes" in rec.calls[0])
    check("process ok: `new --spec` points at the dropped spec (in processed by now? check arg recorded)",
          str(sp) in rec.calls[0] or any(a.endswith("newagent.json") for a in rec.calls[0]))
    check("process ok: `run` is --no-build --no-open against --dir target",
          "--no-build" in rec.calls[1] and "--no-open" in rec.calls[1]
          and "--dir" in rec.calls[1] and rec.calls[1][rec.calls[1].index("--dir") + 1] == str(target))
    check("process ok: incoming drained", list((q / "incoming").glob("*")) == [])
    check("process ok: spec moved to processed/", len(list((q / "processed").glob("*newagent.json"))) == 1)
    check("process ok: no failed artifacts", list((q / "failed").glob("*")) == [])

    # ---------------------------------------------------------------- _process: unsafe name -> refused
    q = _mk_queue(root / "q_unsafe")
    stacks = root / "stacks_unsafe"; stacks.mkdir()
    rec = Recorder(returncode=0); SW.subprocess.run = rec
    sp = _drop(q, "Bad Name.json", {"name": "Bad Name"})
    SW._process(sp, stacks, q)
    check("process unsafe-name: executor NOT called", rec.calls == [])
    check("process unsafe-name: moved to failed/", len(list((q / "failed").glob("*"))) >= 1)
    check("process unsafe-name: .error written", len(list((q / "failed").glob("*.error"))) == 1)

    # name that escapes the stacks root via the spec body (passes nothing — SAFE blocks it first)
    rec = Recorder(returncode=0); SW.subprocess.run = rec
    sp = _drop(q, "escape.json", {"name": "../evil"})
    SW._process(sp, stacks, q)
    check("process path-escape name: executor NOT called", rec.calls == [])

    # ---------------------------------------------------------------- existing non-empty target -> refused
    q = _mk_queue(root / "q_exists")
    stacks = root / "stacks_exists"; stacks.mkdir()
    (stacks / "taken").mkdir()
    (stacks / "taken" / "something").write_text("already here")
    rec = Recorder(returncode=0); SW.subprocess.run = rec
    sp = _drop(q, "taken.json", {"name": "taken"})
    SW._process(sp, stacks, q)
    check("process existing-target: executor NOT called", rec.calls == [])
    check("process existing-target: moved to failed/", len(list((q / "failed").glob("*taken.json"))) == 1)

    # ---------------------------------------------------------------- `enclave new` fails -> run NOT called
    q = _mk_queue(root / "q_newfail")
    stacks = root / "stacks_newfail"; stacks.mkdir()
    rec = Recorder(rcs=[3, 0], stderr="build broke"); SW.subprocess.run = rec
    sp = _drop(q, "buildbad.json", {"name": "buildbad"})
    SW._process(sp, stacks, q)
    check("process new-fail: only the `new` call happened (run skipped)",
          len(rec.calls) == 1 and rec.subcmd(0) == "new")
    check("process new-fail: spec moved to failed/", len(list((q / "failed").glob("*buildbad.json"))) == 1)
    check("process new-fail: .error captures stderr",
          any("build broke" in f.read_text() for f in (q / "failed").glob("*.error")))

    # ---------------------------------------------------------------- `enclave run` fails -> spec failed
    q = _mk_queue(root / "q_runfail")
    stacks = root / "stacks_runfail"; stacks.mkdir()
    rec = Recorder(rcs=[0, 5], stderr="run broke"); SW.subprocess.run = rec
    sp = _drop(q, "runbad.json", {"name": "runbad"})
    SW._process(sp, stacks, q)
    check("process run-fail: both calls happened", len(rec.calls) == 2)
    check("process run-fail: spec moved to failed/", len(list((q / "failed").glob("*runbad.json"))) == 1)
    check("process run-fail: NOT in processed/", list((q / "processed").glob("*")) == [])

    # ---------------------------------------------------------------- secrets staging side effect
    q = _mk_queue(root / "q_secrets")
    stacks = root / "stacks_secrets"; stacks.mkdir()
    staged = q / "secrets-staging" / "secagent"
    staged.mkdir(parents=True)
    (staged / "openai.env").write_text("OPENAI_API_KEY=sk-test\n")
    (staged / "extra.env").write_text("FOO=bar\n")
    rec = Recorder(returncode=0); SW.subprocess.run = rec
    sp = _drop(q, "secagent.json", {"name": "secagent"})
    SW._process(sp, stacks, q)
    target = (stacks / "secagent").resolve()
    check("process secrets: staged env files copied to target/secrets",
          (target / "secrets" / "openai.env").exists() and (target / "secrets" / "extra.env").exists())
    check("process secrets: copied content preserved",
          (target / "secrets" / "openai.env").read_text() == "OPENAI_API_KEY=sk-test\n")
    check("process secrets: staging dir consumed (removed)", not staged.exists())
    check("process secrets: copied files are chmod 600 (best-effort)",
          ((target / "secrets" / "openai.env").stat().st_mode & 0o777) in (0o600, 0o644))
    check("process secrets: spec still processed", len(list((q / "processed").glob("*secagent.json"))) == 1)

    # ------------------------------------------ structural guardrail: payment/legal cred is REFUSED
    # The control on the operator's money is the ABSENCE of the capability — a payment credential must
    # never be mounted into a pod. Staging one quarantines it (outside the pod) + escalates.
    q = _mk_queue(root / "q_paycred")
    stacks = root / "stacks_paycred"; stacks.mkdir()
    staged = q / "secrets-staging" / "paybot"
    staged.mkdir(parents=True)
    (staged / "nvidia.env").write_text("NVIDIA_API_KEY=nvapi-ok\n")     # normal → mounted
    (staged / "stripe.env").write_text("STRIPE_SECRET_KEY=sk_live_x\n")  # payment → refused
    rec = Recorder(returncode=0); SW.subprocess.run = rec
    sp = _drop(q, "paybot.json", {"name": "paybot"})
    SW._process(sp, stacks, q)
    target = (stacks / "paybot").resolve()
    check("paycred: normal cred IS mounted", (target / "secrets" / "nvidia.env").exists())
    check("paycred: payment cred NOT mounted into the pod",
          not (target / "secrets" / "stripe.env").exists())
    check("paycred: payment cred quarantined outside the pod",
          (q / "secrets-refused" / "paybot" / "stripe.env").exists())
    check("paycred: operator escalation written",
          (target / "home" / "state" / "escalations.log").exists()
          and "payment" in (target / "home" / "state" / "escalations.log").read_text().lower())
    check("paycred: pod still created + started (runs WITHOUT the refused cred)",
          len(list((q / "processed").glob("*paybot.json"))) == 1)

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
