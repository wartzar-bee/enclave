#!/usr/bin/env python3
"""Hermetic tests for compactor.py — the PreToolUse context-guard.

Runs the hook as a subprocess (the real protocol: JSON on stdin, exit code out) in both report-only
and enforce modes. No network, no Claude. `python3 test_compactor.py`.
"""
import os, sys, json, subprocess, tempfile, pathlib

HOOK = str(pathlib.Path(__file__).with_name("compactor.py"))


def run(ev, enforce=False, env_extra=None, agent_dir=None, mode=None):
    env = dict(os.environ)
    env.pop("COMPACT_ENFORCE", None)
    env.pop("COMPACT_MODE", None)
    if enforce:
        env["COMPACT_ENFORCE"] = "1"
    if mode:
        env["COMPACT_MODE"] = mode
    if agent_dir:
        env["AGENT_DIR"] = str(agent_dir)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(ev).encode(),
                       capture_output=True, env=env)
    return p.returncode, p.stderr.decode()


def run_out(ev, agent_dir=None, mode=None, env_extra=None):
    """Same protocol, but hand back stdout — spill mode answers with a JSON body."""
    env = dict(os.environ)
    env.pop("COMPACT_ENFORCE", None)
    env.pop("COMPACT_MODE", None)
    if mode:
        env["COMPACT_MODE"] = mode
    if agent_dir:
        env["AGENT_DIR"] = str(agent_dir)
    if env_extra:
        env.update(env_extra)
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(ev).encode(),
                       capture_output=True, env=env)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


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

        # --- SPILL mode: the call is RESHAPED, not refused (Tier 2, docs/CONTEXT-COMPACTOR.md) ---
        rc, out, _ = run_out(bash("cat big.json"), agent_dir=ad, mode="spill")
        check("spill allows (exit 0)", rc == 0)
        body = json.loads(out) if out.strip() else {}
        hso = body.get("hookSpecificOutput", {})
        check("spill returns PreToolUse updatedInput", hso.get("hookEventName") == "PreToolUse"
              and "updatedInput" in hso)
        check("spill decides allow", hso.get("permissionDecision") == "allow")
        newcmd = hso.get("updatedInput", {}).get("command", "")
        check("spill keeps the original command", "cat big.json" in newcmd)
        check("spill redirects into state/.compact", "/state/.compact/" in newcmd)
        check("spill preserves the exit status", "(exit $__rc)" in newcmd)
        check("spill names the locator to the model", "Nothing was lost" in newcmd)

        rc2, out2, _ = run_out(bash("cat big.json"), agent_dir=ad, mode="spill")
        cmd2 = json.loads(out2)["hookSpecificOutput"]["updatedInput"]["command"]
        check("spill paths never collide", cmd2 != newcmd)

        # the rewrite must actually WORK in a real shell: full output on disk, bounded preview
        # on stdout, original exit status preserved.
        r = subprocess.run(["bash", "-c", newcmd], capture_output=True, cwd=str(ad),
                           env={**os.environ, "PATH": os.environ.get("PATH", "")})
        check("rewritten command succeeds", r.returncode == 0)
        stdout = r.stdout.decode(errors="replace")
        check("preview is bounded", len(stdout) < 6000)
        check("preview carries the elision marker", "[compactor]" in stdout and "bytes) is in" in stdout)
        spills = sorted((ad / "state" / ".compact").glob("*.txt"))
        check("spill file holds the FULL output", any(f.stat().st_size == 200_000 for f in spills))

        # a failing command keeps its non-zero status through the wrapper
        rc3, out3, _ = run_out(bash("cat /nonexistent-xyz"), agent_dir=ad, mode="spill")
        failcmd = json.loads(out3)["hookSpecificOutput"]["updatedInput"]["command"]
        r3 = subprocess.run(["bash", "-c", failcmd], capture_output=True, cwd=str(ad))
        check("rewritten command preserves failure status", r3.returncode != 0)

        # a trailing comment must not swallow the wrapper
        rc4, out4, _ = run_out(bash("cat big.json  # look at the whole thing"), agent_dir=ad, mode="spill")
        c4 = json.loads(out4)["hookSpecificOutput"]["updatedInput"]["command"]
        r4 = subprocess.run(["bash", "-c", c4], capture_output=True, cwd=str(ad))
        check("trailing comment survives the rewrite", r4.returncode == 0 and b"[compactor]" in r4.stdout)

        # a large no-limit Read becomes a bounded Read
        rc5, out5, _ = run_out(read(str(big)), agent_dir=ad, mode="spill")
        ui = json.loads(out5)["hookSpecificOutput"]["updatedInput"]
        check("spill bounds a large Read", ui.get("limit") == 400 and ui.get("file_path") == str(big))

        # a backgrounded command has no safe rewrite → spill falls back to enforce, never to allow
        rc6, out6, err6 = run_out(bash("find / -name '*.py' &"), agent_dir=ad, mode="spill")
        check("spill falls back to block when it cannot rewrite", rc6 == 2 and "compactor" in err6)

        # bounded calls are still untouched in spill mode
        rc7, out7, _ = run_out(bash("cat big.json | head -50"), agent_dir=ad, mode="spill")
        check("spill leaves disciplined calls alone", rc7 == 0 and out7.strip() == "")

        # spill mode is recorded as itself in the log
        rec = json.loads((ad / "state" / "compact.log").read_text().splitlines()[-1])
        check("log record has mode=spill", rec["mode"] == "spill")

        # legacy spelling still selects enforce
        rc8, _ = run(bash("cat big.json"), enforce=True, agent_dir=ad)
        check("COMPACT_ENFORCE=1 still enforces", rc8 == 2)

        # --- spill files are pruned by age, so days-long pods do not fill their disk ---
        sd = ad / "state" / ".compact"
        sd.mkdir(parents=True, exist_ok=True)
        stale = sd / "stale.txt"; stale.write_text("old")
        os.utime(stale, (0, 0))  # epoch 0 => far older than any TTL
        fresh = sd / "fresh.txt"; fresh.write_text("new")
        run_out(bash("cat big.json"), agent_dir=ad, mode="spill")
        check("prunes a stale spill file", not stale.exists())
        check("keeps a fresh spill file", fresh.exists())
        stale2 = sd / "stale2.txt"; stale2.write_text("old"); os.utime(stale2, (0, 0))
        run_out(bash("cat big.json"), agent_dir=ad, mode="spill",
                env_extra={"COMPACT_SPILL_TTL_DAYS": "0"})
        check("TTL=0 disables pruning", stale2.exists())

        # --- visual reads are NEVER gated: byte size is not their context cost ---
        png = ad / "shot.png"
        png.write_bytes(b"\x89PNG" + b"x" * 600_000)
        jpg = ad / "ref.JPG"
        jpg.write_bytes(b"x" * 900_000)
        for name, f in [("png", png), ("uppercase .JPG", jpg)]:
            rc, _ = run(read(str(f)), enforce=True, agent_dir=ad)
            check(f"enforce never blocks a visual read: {name}", rc == 0)
            rcs, outs, _ = run_out(read(str(f)), agent_dir=ad, mode="spill")
            check(f"spill never rewrites a visual read: {name}", rcs == 0 and outs.strip() == "")
        rc, _ = run(read(str(big)), enforce=True, agent_dir=ad)
        check("a large TEXT read is still gated", rc == 2)

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
