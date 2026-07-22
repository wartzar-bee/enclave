#!/usr/bin/env python3
"""framework_version.py — is this process still running the framework that is on disk?

THE PROBLEM THIS EXISTS FOR. A Python process imports its modules once, at start. Every long-lived
part of this framework — the agent loop, the fleet monitor, the console — therefore keeps running
whatever code it booted with, no matter what changes underneath it. Editing a bind-mounted file or
rebuilding an image changes the DISK, not the RUNNING PROCESS.

On 2026-07-22 that cost three separate rounds of confusion in a single session:
  * agentloop had 4h29m of stale code, so a loop fix appeared to have no effect;
  * fleet_monitor was still running the old runbook, so a new detector never fired;
  * the console had been up 22h59m, so the dashboard showed the operator a fleet that looked idle or
    down while every agent was in fact working — it was rendering yesterday's code against today's
    state, and that was reported as an agent problem when it was a staleness problem.
Each time the fix was already "committed" and even bind-mounted. None of it was LIVE. "Done" has to
mean the running system changed, and nothing here enforced that.

WHAT THIS DOES. fingerprint() hashes the framework's own source files. A process records its boot
fingerprint; `is_stale()` says whether disk has moved since. A long-lived process calls
`restart_if_stale()` at a point where dying is SAFE — between ticks, between cycles — and re-execs
itself, picking up the new code with the same argv.

Deliberately NOT a filesystem watcher: no dependency, no thread, no event queue. A cheap stat() of
~40 files at a natural idle point is enough, and it can only ever act where the caller says it is safe.
"""
import hashlib
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
# Restarting on a change to a TEST file would bounce the fleet during development for no benefit.
SKIP_PREFIX = ("test_", "tests_")
SKIP_DIRS = {"__pycache__", "hooks"}      # hooks run per-invocation; they are never held in memory


def source_files(root=None):
    """Every framework source file whose content a long-lived process could be holding in memory."""
    root = pathlib.Path(root or HERE)
    out = []
    for p in sorted(root.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name.startswith(SKIP_PREFIX):
            continue
        out.append(p)
    return out


def fingerprint(root=None):
    """A short stable digest of the framework source on disk.

    Uses (relative path, size, mtime-ns) rather than file contents: it is one stat() per file instead
    of reading ~1MB every cycle, and it changes on exactly the events we care about — an edit, a
    bind-mount swap, an image rebuild. Content hashing would also flag a touch with no edit as a
    change; that is a false positive that costs a restart, so size+mtime is the more conservative
    choice here, not the lazier one."""
    root = pathlib.Path(root or HERE)
    h = hashlib.sha256()
    for p in source_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())
    return h.hexdigest()[:16]


class StaleCheck:
    """Records the fingerprint at boot and reports when disk has moved past it.

    min_interval_s throttles the stat sweep so a tight loop cannot spend itself checking; it is a
    performance guard, never a correctness one — staleness does not expire."""

    def __init__(self, root=None, min_interval_s=30):
        self.root = root
        self.boot = fingerprint(root)
        self.min_interval_s = min_interval_s
        self._last_check = 0.0
        self._cached = False

    def is_stale(self, now=None):
        now = now if now is not None else time.time()
        if now - self._last_check < self.min_interval_s:
            return self._cached
        self._last_check = now
        self._cached = fingerprint(self.root) != self.boot
        return self._cached

    def restart_if_stale(self, log=print, what="process", now=None, _exec=None):
        """Re-exec this process IF the framework changed. Call ONLY where dying is safe.

        Returns False when nothing changed. On a real restart it does not return at all — execv
        replaces the process image, keeping the same argv, so the caller needs no teardown."""
        if not self.is_stale(now=now):
            return False
        log(f"framework changed on disk since this {what} started "
            f"({self.boot} -> {fingerprint(self.root)}) — re-exec'ing to run the new code. "
            f"A file edit or image rebuild does NOT reach an already-running process.")
        sys.stdout.flush()
        sys.stderr.flush()
        (_exec or os.execv)(sys.executable, [sys.executable] + sys.argv)
        return True     # only reached when _exec is a test double


def _selftest():
    import tempfile, json
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")

    with tempfile.TemporaryDirectory() as td:
        d = pathlib.Path(td)
        (d / "a.py").write_text("x = 1\n")
        (d / "test_a.py").write_text("x = 1\n")
        (d / "__pycache__").mkdir(); (d / "__pycache__" / "junk.py").write_text("x\n")
        f1 = fingerprint(d)
        check("fingerprint is stable when nothing changes", fingerprint(d) == f1)

        names = {p.name for p in source_files(d)}
        check("test files are excluded (editing a test must not bounce the fleet)",
              "test_a.py" not in names and "a.py" in names)
        check("__pycache__ is excluded", "junk.py" not in names)

        time.sleep(0.01)
        (d / "a.py").write_text("x = 2\n")
        check("fingerprint changes on an edit", fingerprint(d) != f1)

        sc = StaleCheck(d, min_interval_s=0)
        check("not stale at boot", sc.is_stale() is False)
        time.sleep(0.01)
        (d / "a.py").write_text("x = 3\n")
        check("stale after the framework changes underneath it", sc.is_stale() is True)

        # restart_if_stale must be a NO-OP when nothing moved — a spurious restart mid-fleet is worse
        # than staleness, because it can drop work.
        calls = []
        sc2 = StaleCheck(d, min_interval_s=0)
        check("restart_if_stale does nothing when current",
              sc2.restart_if_stale(log=lambda *_: None, _exec=lambda *a: calls.append(a)) is False
              and not calls)
        time.sleep(0.01)
        (d / "a.py").write_text("x = 4\n")
        sc2.restart_if_stale(log=lambda *_: None, _exec=lambda *a: calls.append(a))
        check("restart_if_stale re-execs when stale", len(calls) == 1)

        # The throttle is a performance guard, not a correctness one: a change is still detected,
        # just not on every single call.
        sc3 = StaleCheck(d, min_interval_s=10_000)
        check("first check always sweeps (never start out throttled)", sc3.is_stale() is False)
        time.sleep(0.01)
        (d / "a.py").write_text("x = 5\n")
        check("throttle suppresses the sweep within the interval", sc3.is_stale() is False)
        check("…and reports the change once the interval passes",
              sc3.is_stale(now=time.time() + 20_000) is True)

    print("\nSELFTEST OK" if ok else "\nSELFTEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print(fingerprint())
