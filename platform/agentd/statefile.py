#!/usr/bin/env python3
"""
statefile.py — durable writes + the ONE canonical work.json reader.

Two defects this consolidates:

1. TORN WRITES. Agent hot state (work.json, capabilities, handoff, memory INDEX) was written with
   truncate-in-place `path.write_text(...)` and no fsync, while both cost-cutoff paths end in
   `pkill -KILL`. A kill landing mid-write leaves a half-written / zero-length file. `write_json`
   writes to a temp file in the same directory, fsyncs it, and `os.replace()`s it into place — an
   atomic swap, so a reader (or a crash) ever sees only the old file or the whole new one.

2. THREE READERS THAT DISAGREED about whether work exists. work.json comes in two shapes in the
   fleet — a bare list of items, and a `{"updated","note","items":[...]}` dict some pods write.
   agentloop normalised the dict; tick_feeder and memory.work_list treated a dict as EMPTY. So a
   dict-shaped backlog read as "no open work" in two of the three, and the agent parked on a full
   queue while the wake-gate thought there was nothing to do. `open_work` is the single normaliser
   all three now read through, so they can never disagree again.

Stdlib only, no deps. Import from any agentd module (same directory).
"""
import json, os, pathlib, tempfile

__all__ = ["write_atomic", "write_json", "read_json", "open_work", "has_open_work"]


def write_atomic(path, text):
    """Atomically replace `path` with `text`: temp file in the same dir → fsync → os.replace.
    A concurrent reader or a SIGKILL sees either the old file whole or the new file whole, never a
    torn one. Raises on a genuine write error (caller decides) — but never leaves a partial target."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="." + p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(p))
        tmp = None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_json(path, obj, indent=2, trailing_newline=True):
    """Atomic JSON write. Defaults (indent=2 + trailing newline) match the existing work.json format
    so a diff of a rewritten file is content-only, not whitespace churn."""
    text = json.dumps(obj, indent=indent)
    if trailing_newline:
        text += "\n"
    write_atomic(path, text)


def read_json(path, default=None):
    """Tolerant read: missing / unreadable / invalid JSON → `default` (never raises)."""
    try:
        return json.loads(pathlib.Path(path).read_text())
    except Exception:
        return default


def open_work(path):
    """Canonical work.json → list of item dicts. Handles BOTH fleet shapes (bare list, or
    {"items"/"tasks":[...]} dict); torn / missing / invalid → []. Non-dict entries are dropped. This
    is the ONE normaliser — agentloop, tick_feeder and memory read through it so they cannot disagree
    about whether a backlog exists."""
    w = read_json(path, default=None)
    if isinstance(w, list):
        items = w
    elif isinstance(w, dict):
        items = w.get("items") or w.get("tasks") or []
    else:
        items = []
    return [i for i in items if isinstance(i, dict)]


def has_open_work(path):
    """True iff any item is not done/dropped. The single definition the wake-gate and the epoch-close
    both use — previously they read the file differently and one could see an empty queue the other
    saw as full."""
    return any(i.get("status") not in ("done", "dropped") for i in open_work(path))


def _selftest():
    import tempfile as _tf
    fails = []
    def ck(n, c):
        if not c: fails.append(n)

    with _tf.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "work.json"

        # atomic round-trip
        write_json(p, [{"id": 1, "text": "a", "status": "todo"}])
        ck("roundtrip", read_json(p)[0]["status"] == "todo")
        ck("trailing-newline", p.read_text().endswith("}\n]\n") or p.read_text().endswith("\n"))
        ck("no-tmp-left", not any(x.name.startswith(".work.json.") for x in pathlib.Path(d).iterdir()))

        # list shape
        ck("list-open", has_open_work(p) is True)
        write_json(p, [{"id": 1, "status": "done"}, {"id": 2, "status": "dropped"}])
        ck("list-all-closed", has_open_work(p) is False)

        # dict shape ({"items":[...]}) — the exact case that read as empty in 2 of 3 readers
        write_json(p, {"updated": "x", "items": [{"id": 1, "status": "todo"}]})
        ck("dict-items-open", has_open_work(p) is True)
        ck("dict-items-count", len(open_work(p)) == 1)
        # dict with "tasks" alias
        write_json(p, {"tasks": [{"id": 9, "status": "doing"}]})
        ck("dict-tasks-open", has_open_work(p) is True)

        # torn / garbage / missing → empty, never raises
        p.write_text('{"items": [')          # half-written (torn)
        ck("torn-empty", open_work(p) == [] and has_open_work(p) is False)
        p.write_text("")                     # zero-length (killed mid-write)
        ck("zerolen-empty", open_work(p) == [])
        (pathlib.Path(d) / "gone.json")      # missing
        ck("missing-empty", open_work(pathlib.Path(d) / "gone.json") == [])
        # non-dict entries dropped
        write_json(p, [{"id": 1, "status": "todo"}, "junk", 5])
        ck("drops-nondict", len(open_work(p)) == 1)

    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK")
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
