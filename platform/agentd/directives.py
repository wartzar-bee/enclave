#!/usr/bin/env python3
"""
directives.py — the fleet's revocation primitive (2026-07-19 evaluation, fix #1).

Problem this solves: every instruction channel was append-only prose, so a live instruction
and its cancellation coexisted at equal priority and the agent had to resolve them by
inference on a fresh context, every tick (forgepod FOCUS LOCK vs 10-games mission;
scribepod term sheet re-asserting a retracted claim; scoutpod's retraction never
reaching recall at all because memory.py filtered directives on a `\\bboard\\b` regex).

The primitive: `state/directives.json` — compiled directive STATE, not paragraphs.
  { "version": 1, "updated": "<iso>", "compiled_by": "studio", "source": "inbox.md",
    "directives": [ { "id": "<slug>", "status": "active|superseded|retracted",
                      "priority": <int, lower = first>, "date": "...", "text": "..." } ] }

Rules of the road:
- The OPERATOR keeps writing prose (inbox/chat). the orchestrator compiles it into this file at
  every board sweep / directive delivery — writing the inbox without recompiling is the
  "term sheet never reached the pod" failure; `verify` catches that mechanically.
- The tick injects ACTIVE entries IN FULL, in priority order (memory.py digest reads this
  file first; the inbox is fallback only). RETRACTED entries are surfaced as one-liners so
  the agent knows what it must never cite again. SUPERSEDED entries are silent history.
- route_tier.py reads active texts from here when present, so a stale `[tier:top]` tag in
  the inbox can no longer pin every tick (incl. heartbeats) to the top model.

CLI:
  directives.py <agent-base> show                  # human-readable current state
  directives.py <agent-base> draft                 # zero-LLM draft from open inbox items (studio then edits)
  directives.py <agent-base> verify [--max-age-h N]# schema + staleness gate; rc!=0 on failure
  directives.py --selftest                         # tests built from the real 2026-07-19 faults
"""
import argparse, json, pathlib, re, sys, tempfile, time

STATUSES = ("active", "superseded", "retracted")


def path_for(base):
    return pathlib.Path(base) / "state" / "directives.json"


def load(base):
    """Parsed directives file for an agent base dir, or None if absent/unparseable."""
    f = path_for(base)
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text())
        return d if isinstance(d, dict) and isinstance(d.get("directives"), list) else None
    except Exception:
        return None


def active(base):
    """Active directives, priority-ordered (lower priority value first). [] when no file."""
    d = load(base)
    if not d:
        return []
    acts = [x for x in d["directives"]
            if isinstance(x, dict) and x.get("status") == "active" and x.get("text", "").strip()]
    return sorted(acts, key=lambda x: (x.get("priority", 50), x.get("id", "")))


def retracted(base):
    d = load(base)
    if not d:
        return []
    return [x for x in d["directives"] if isinstance(x, dict) and x.get("status") == "retracted"]


def verify(base, max_age_h=None):
    """Mechanical delivery/coherence gate. Returns (ok, [problems]).
    Failures: missing/unparseable file, bad schema, duplicate ids, inbox newer than the
    compiled state (directive written but never delivered — the L-304 class)."""
    base = pathlib.Path(base)
    f = path_for(base)
    probs = []
    if not f.exists():
        return False, [f"missing: {f}"]
    try:
        d = json.loads(f.read_text())
    except Exception as e:
        return False, [f"unparseable JSON: {e}"]
    items = d.get("directives") if isinstance(d, dict) else None
    if not isinstance(items, list):
        return False, ["schema: top-level 'directives' list missing"]
    seen = set()
    for i, x in enumerate(items):
        if not isinstance(x, dict):
            probs.append(f"directives[{i}]: not an object"); continue
        did = x.get("id")
        if not did or not isinstance(did, str):
            probs.append(f"directives[{i}]: missing id")
        elif did in seen:
            probs.append(f"directives[{i}]: duplicate id '{did}'")
        else:
            seen.add(did)
        if x.get("status") not in STATUSES:
            probs.append(f"directives[{i}] ({did}): status '{x.get('status')}' not in {STATUSES}")
        if not str(x.get("text", "")).strip():
            probs.append(f"directives[{i}] ({did}): empty text")
    if not any(isinstance(x, dict) and x.get("status") == "active" for x in items):
        probs.append("no active directive (a governed pod should always have one)")
    ib = base / "inbox.md"
    if ib.exists():
        lag_h = (ib.stat().st_mtime - f.stat().st_mtime) / 3600.0
        limit = 1.0 if max_age_h is None else float(max_age_h)
        if lag_h > limit:
            probs.append(f"STALE: inbox.md is {lag_h:.1f}h newer than directives.json — "
                         "a directive was written but never compiled/delivered (recompile)")
    return (len(probs) == 0), probs


def draft(base):
    """Zero-LLM draft compiled from ALL open '- [ ]' inbox items — no keyword filter (the old
    `\\bboard\\b` regex silently dropped most directives). the orchestrator edits statuses/priorities
    by hand; supersession is a human judgment, this only guarantees nothing is dropped."""
    base = pathlib.Path(base)
    ib = base / "inbox.md"
    items, seen = [], set()
    if ib.exists():
        for ln in ib.read_text().splitlines():
            s = ln.strip()
            if not s.startswith("- [ ]"):
                continue
            text = s[5:].strip()
            body = text.split("): ", 1)[-1]
            key = re.sub(r"\s+", " ", body.lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            slug = re.sub(r"[^a-z0-9]+", "-", body.lower()[:40]).strip("-") or f"item-{len(items)+1}"
            items.append({"id": slug, "status": "active", "priority": (len(items) + 1) * 10,
                          "date": time.strftime("%Y-%m-%d"), "text": text})
    return {"version": 1, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "compiled_by": "draft", "source": "inbox.md", "directives": items}


# ── selftest: fixtures are the REAL 2026-07-19 faults (a gate nobody has seen fail is theater) ──
def _selftest():
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as td:
        b = pathlib.Path(td); (b / "state").mkdir()
        # F1: the scoutpod fault — 3 [tier:top] inbox directives, NO directives.json.
        (b / "inbox.md").write_text(
            "- [ ] 2026-07-19 — [tier:top] FIRST DIRECTIVE — you are live.\n"
            "- [ ] 2026-07-19 — [tier:top] CORRECTION — THIS REPLACES IT.\n"
            "- [ ] 2026-07-19 — [tier:top] YOUR FIRST TICK'S CONCLUSION IS WRONG.\n")
        ok, probs = verify(b)
        check("missing-file-fails", not ok and "missing" in probs[0])
        d = draft(b)
        check("draft-keeps-all-3", len(d["directives"]) == 3)   # the old regex kept 0 of these
        check("draft-all-active", all(x["status"] == "active" for x in d["directives"]))
        # F2: valid file → verify passes, active() ordered by priority.
        path_for(b).write_text(json.dumps({"version": 1, "directives": [
            {"id": "b", "status": "active", "priority": 2, "text": "second"},
            {"id": "a", "status": "active", "priority": 1, "text": "first"},
            {"id": "r", "status": "retracted", "priority": 90, "text": "the 61-followers claim"},
        ]}))
        ok, probs = verify(b)
        check("valid-passes", ok, )
        check("active-ordered", [x["id"] for x in active(b)] == ["a", "b"])
        check("retracted-listed", [x["id"] for x in retracted(b)] == ["r"])
        # F3: the L-304 fault — term sheet written to the inbox AFTER the last compile.
        import os
        old = time.time() - 8 * 3600
        os.utime(path_for(b), (old, old))
        ok, probs = verify(b)
        check("stale-detected", not ok and any("STALE" in p for p in probs))
        # F4: schema faults — duplicate id, bad status, empty text, no active.
        path_for(b).write_text(json.dumps({"version": 1, "directives": [
            {"id": "x", "status": "retracted", "priority": 1, "text": "t"},
            {"id": "x", "status": "nonsense", "priority": 2, "text": ""},
        ]}))
        ok, probs = verify(b)
        check("dupe-id", any("duplicate id" in p for p in probs))
        check("bad-status", any("not in" in p for p in probs))
        check("empty-text", any("empty text" in p for p in probs))
        check("no-active", any("no active" in p for p in probs))
        # F5: unparseable file never crashes the callers.
        path_for(b).write_text("{not json")
        check("load-none", load(b) is None)
        check("active-empty", active(b) == [])
    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK (12/12)")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", help="agent base dir (the dir holding inbox.md + state/)")
    ap.add_argument("cmd", nargs="?", choices=["show", "draft", "verify"], default="show")
    ap.add_argument("--max-age-h", type=float, default=None)
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    if not a.base:
        ap.error("base required (or --selftest)")
    if a.cmd == "draft":
        print(json.dumps(draft(a.base), indent=2))
    elif a.cmd == "verify":
        ok, probs = verify(a.base, a.max_age_h)
        for p in probs:
            print(f"✗ {p}")
        print("directives: OK" if ok else "directives: FAIL")
        sys.exit(0 if ok else 1)
    else:
        acts, retr = active(a.base), retracted(a.base)
        for x in acts:
            print(f"[{x.get('priority',50):>3}] {x['id']}: {x['text'][:120]}")
        for x in retr:
            print(f"[RETRACTED] {x['id']}: {x.get('text','')[:100]}")
        if not acts and not retr:
            print("(no directives.json or no entries)")


if __name__ == "__main__":
    main()
