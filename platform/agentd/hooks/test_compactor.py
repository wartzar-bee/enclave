#!/usr/bin/env python3
"""Hermetic tests for compactor.py — the PreToolUse context-guard.

Runs the hook as a subprocess (the real protocol: JSON on stdin, exit code out) in both report-only
and enforce modes. No network, no Claude. `python3 test_compactor.py`.
"""
import os, sys, json, subprocess, tempfile, pathlib

HOOK = str(pathlib.Path(__file__).with_name("compactor.py"))


def run(ev, enforce=False, env_extra=None, agent_dir=None):
    env = dict(os.environ)
    env.pop("COMPACT_ENFORCE", None)
    if enforce:
        env["COMPACT_ENFORCE"] = "1"
    if agent_dir:
        env["AGENT_DIR"] = str(agent_dir)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(ev).encode(),
                       capture_output=True, env=env)
    return p.returncode, p.stderr.decode()


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def read(fp, limit=None):
    ti = {"file_path": fp}
    if limit is not None:
        ti["limit"] = limit
    return {"tool_name": "Read", "tool_input": ti}


PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def main():
    with tempfile.TemporaryDirectory() as d:
        ad = pathlib.Path(d) / "agent"
        (ad / "state").mkdir(parents=True)
        (ad / ".claude").mkdir()
        big = ad / "big.json"
        big.write_bytes(b"x" * 200_000)
        small = ad / "small.txt"
        small.write_bytes(b"hi")

        # --- ENFORCE mode: context-bombing calls are blocked (exit 2) ---
        for name, ev in [
            ("cat whole file", bash("cat big.json")),
            ("find un-piped", bash("find . -name '*.py'")),
            ("grep -r un-piped", bash("grep -r TODO src/")),
            ("ls -R", bash("ls -R /agent/work")),
            ("rg un-piped", bash("rg pattern")),
            ("tree", bash("tree /agent")),
        ]:
            rc, err = run(ev, enforce=True, agent_dir=ad)
            check(f"enforce blocks: {name}", rc == 2 and "compactor" in err)

        rc, _ = run(read(str(big)), enforce=True, agent_dir=ad)
        check("enforce blocks: Read big file no-limit", rc == 2)

        # --- ENFORCE mode: disciplined / bounded calls are ALLOWED (exit 0) ---
        for name, ev in [
            ("cat | head", bash("cat big.json | head -50")),
            ("find | head", bash("find . -name '*.py' | head -20")),
            ("grep -r | wc", bash("grep -r TODO src/ | wc -l")),
            ("grep -rl", bash("grep -rl TODO src/")),
            ("find -maxdepth 1", bash("find . -maxdepth 1 -name '*.py'")),
            ("find > file", bash("find . -name '*.py' > /tmp/list.txt")),
            ("grep -c", bash("grep -rc TODO src/")),
            ("plain echo", bash("echo hello")),
            ("python script run", bash("python3 analyze.py")),
        ]:
            rc, _ = run(ev, enforce=True, agent_dir=ad)
            check(f"enforce allows: {name}", rc == 0)

        rc, _ = run(read(str(big), limit=200), enforce=True, agent_dir=ad)
        check("enforce allows: Read big file WITH limit", rc == 0)
        rc, _ = run(read(str(small)), enforce=True, agent_dir=ad)
        check("enforce allows: Read small file no-limit", rc == 0)
        rc, _ = run(read(str(ad / "missing.txt")), enforce=True, agent_dir=ad)
        check("enforce allows: Read missing file (fail-open)", rc == 0)

        # --- REPORT-ONLY mode: never blocks, but LOGS the gate ---
        logp = ad / "state" / "compact.log"
        logp.unlink(missing_ok=True)
        rc, _ = run(bash("cat big.json"), enforce=False, agent_dir=ad)
        check("report-only allows (exit 0)", rc == 0)
        check("report-only wrote compact.log", logp.exists())
        if logp.exists():
            rec = json.loads(logp.read_text().splitlines()[-1])
            check("log record has mode=report", rec["mode"] == "report")
            check("log record names the tool", rec["tool"] == "Bash")

        # --- robustness: malformed input fails open ---
        p = subprocess.run([sys.executable, HOOK], input=b"not json", capture_output=True)
        check("malformed stdin → allow", p.returncode == 0)
        rc, _ = run({"tool_name": "Edit", "tool_input": {}}, enforce=True, agent_dir=ad)
        check("unrelated tool → allow", rc == 0)
        rc, _ = run({"tool_name": "Read", "tool_input": {}}, enforce=True, agent_dir=ad)
        check("Read with no file_path → allow", rc == 0)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
