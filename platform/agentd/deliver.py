#!/usr/bin/env python3
"""deliver.py — framework output→destination delivery: an agent's outputs REACH their pipeline.

Why this is a framework primitive: scoutpod filed candidates to `ideas/scout/*.md` for days
while the deal-flow ledger read a separate file nothing ever wrote — a pipe attached to nothing.
The pipeline reported permanent STARVED, which fired starvation escalations, a rebuke, a quota,
and finally fabricated candidates. The failure was invisible because delivery was an unowned,
special-cased script. This makes "output reaches destination" a declared, generic, verifiable
mechanism: config-driven, deterministic, idempotent, with a HEARTBEAT file that preflight's
`delivery` probe checks from inside the pod — so a disconnected pipe becomes a broken capability
the agent can SEE, instead of a silent lie in its KPIs.

Semantics (ported from the orchestrator's scout_sync.py, generalised):
  * only ADDS records it hasn't added before (id-keyed); never edits or deletes existing entries
  * never changes a record's stage once the owner moved it beyond `initial_stage`
  * an optional gate_cmd filters sources (stdout starting with "PASS" = deliverable)

Config (JSON):
  {
    "source_glob":   "/abs/ideas/scout/*.md",
    "gate_cmd":      ["python3", "/abs/candidate_gate.py", "{path}"],       // optional
    "ledger":        "/abs/path/deal-flow.json",
    "list_key":      "candidates",
    "id_from":       "name",                       // field slugified into the id
    "initial_stage": "raw",
    "owned_stages":  ["screened", "in_flight", "parked", "killed"],
    "source_label":  "scoutpod",
    "copy_fields":   ["one_line", "buyer", "demand_test", "pass_line"],
    "heartbeat":     "/abs/reports/dealflow/.deliver-heartbeat"             // optional
  }

Run: deliver.py --config <file> [--dry-run] [--json]     (host launchd timer or in-pod post-tick)
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone


def slugify(name, fallback):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:60] or fallback


def first_json_object(text):
    """First balanced {...} that parses as an object, ignoring braces inside strings."""
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
    return None


def parse_source(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    if path.endswith(".json"):
        try:
            return json.loads(txt)
        except Exception:
            return None
    m = re.search(r"```json\s*(\{.*?\})\s*```", txt, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return first_json_object(txt)


def gate_ok(gate_cmd, path):
    if not gate_cmd:
        return True, "no gate"
    cmd = [a.replace("{path}", path) for a in gate_cmd]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out = (r.stdout or r.stderr or "").strip().split("\n")[0]
        return out.startswith("PASS"), out
    except Exception as e:
        return False, f"gate error: {type(e).__name__}: {e}"


def deliver(cfg, dry=False):
    ledger_path = cfg["ledger"]
    list_key = cfg.get("list_key", "items")
    ledger = json.load(open(ledger_path))
    items = ledger.setdefault(list_key, [])
    by_id = {c.get("id"): c for c in items}
    owned = set(cfg.get("owned_stages", []))
    initial = cfg.get("initial_stage", "raw")

    added, skipped, rejected = [], [], []
    sources = sorted(glob.glob(cfg["source_glob"]))
    for path in sources:
        base = os.path.basename(path)
        ok, verdict = gate_ok(cfg.get("gate_cmd"), path)
        if not ok:
            rejected.append((base, verdict[:90]))
            continue
        rec = parse_source(path)
        if not rec:
            rejected.append((base, "unparseable source (no JSON object)"))
            continue
        rid = slugify(rec.get(cfg.get("id_from", "name")), os.path.splitext(base)[0])
        if rid in by_id:
            cur = by_id[rid]
            note = (f"already {cur.get('stage')} — owner-moved, untouched"
                    if cur.get("stage") in owned else f"already in ledger as {cur.get('stage')}")
            skipped.append((base, note))
            continue
        entry = {"id": rid,
                 "name": rec.get(cfg.get("id_from", "name")) or rid,
                 "stage": initial,
                 "source": cfg.get("source_label", "deliver"),
                 "source_file": path,
                 "added_by": "deliver",
                 "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        for f in cfg.get("copy_fields", []):
            entry[f] = rec.get(f, "")
        added.append(entry)
        by_id[rid] = entry

    if added and not dry:
        items.extend(added)
        ledger["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(ledger_path, "w") as fh:
            json.dump(ledger, fh, indent=2)
            fh.write("\n")

    # Heartbeat LAST and unconditionally (even a no-op run proves the pipe is attended) — this is
    # what preflight's `delivery` probe reads in-pod to assert the pipeline is connected.
    #
    # It records HOW MANY SOURCES THE GLOB SAW, not just that the run happened. A timestamp alone
    # says the daemon is alive; it says nothing about whether the daemon and the agent still agree
    # on WHERE output goes. On 2026-07-22 scoutpod began writing candidates to /agent/ideas/scout
    # while this glob pointed at /workspace/ideas/scout: the run kept firing, the heartbeat stayed
    # fresh, the probe stayed green, and 16 gate-passing candidates piled up somewhere nothing read.
    # That is D-110 again in a new spelling — a connected pipe with nothing flowing through it reads
    # identical to a healthy one unless you record the flow. First line stays a bare timestamp so
    # anything reading it as text still works.
    hb = cfg.get("heartbeat")
    if hb and not dry:
        try:
            os.makedirs(os.path.dirname(hb), exist_ok=True)
            with open(hb, "w") as fh:
                fh.write(datetime.now(timezone.utc).isoformat(timespec="seconds") + "\n")
                fh.write(json.dumps({"sources_seen": len(sources), "added": len(added),
                                     "skipped": len(skipped), "rejected": len(rejected),
                                     "source_glob": cfg["source_glob"]}) + "\n")
        except OSError:
            pass
    return added, skipped, rejected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", dest="as_json")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    added, skipped, rejected = deliver(cfg, dry=a.dry_run)
    if a.as_json:
        print(json.dumps({"added": [x["id"] for x in added],
                          "skipped": len(skipped), "rejected": len(rejected)}, indent=2))
    else:
        print(f"deliver{' (dry-run)' if a.dry_run else ''}: +{len(added)} added · "
              f"{len(skipped)} already present · {len(rejected)} not deliverable")
        for x in added:
            print(f"   + {x['id']}  <- {os.path.basename(x['source_file'])}")
        for b, why in rejected:
            print(f"   - {b}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
