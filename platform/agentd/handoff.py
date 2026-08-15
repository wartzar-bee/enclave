#!/usr/bin/env python3
"""handoff.py — emit a typed handoff envelope to state/outbox/.

You (a pod) can't push, route to another pod, or fire an operator-gated action — you PREPARE, a studio
actor FIRES. The canonical way to hand something off is ONE typed envelope, not a bespoke filename:

    python3 platform/agentd/handoff.py emit \
        --to channel-lab --type distribution-help \
        --title "Prove a Medium publish path for tokenscope" \
        --payload '{"product":"tokenscope","goal":"validated recipe","audience":"devs on agent cost"}'

Writes `<base>/state/outbox/<utc>-<type>.json` = {id,to,type,title,payload,from,created}. The off-Opus
handoff-broker dispatches purely on `type` — routing types (distribution-help → `to`'s help-requests/;
candidate-handoff → `to`'s inbox.md) auto-deliver pod-to-pod with no studio relay; judgment/operator
types (maintainer-queue, board-request, glama-claim, operator-fire, release,
cursor-correction, vision-captcha) are surfaced for a studio session to fire. An unknown `type` is
surfaced, never dropped. This is a PARSED protocol (AGENT-RULES §1), unlike the old bespoke queue files.

`to` is a pod id, or `studio` / `operator` for the ones a human/studio fires.
"""
import argparse, json, os, sys, pathlib, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import statefile

ROUTE_TYPES = {"distribution-help", "candidate-handoff"}   # auto-deliver to `to` pod (no studio relay)
SURFACE_TYPES = {"maintainer-queue", "glama-claim", "operator-fire", "board-request",
                 "release", "cursor-correction", "vision-captcha"}
KNOWN = ROUTE_TYPES | SURFACE_TYPES


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def emit(base, to, typ, title, payload):
    outbox = pathlib.Path(base) / "state" / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    eid = f"{stamp}-{typ}"
    env = {"id": eid, "to": to, "type": typ, "title": title,
           "payload": payload, "from": os.environ.get("AGENT_ID", pathlib.Path(base).name),
           "created": _now()}
    path = outbox / f"{eid}.json"
    statefile.write_json(path, env)   # atomic: a reader never sees a half-written envelope
    return path


def main():
    ap = argparse.ArgumentParser(prog="handoff.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit", help="write a typed handoff envelope to state/outbox/")
    e.add_argument("--base", default=os.environ.get("AGENT_HOME", "/agent"),
                   help="agent home (holds state/); default /agent")
    e.add_argument("--to", required=True, help="target pod id, or 'studio'/'operator'")
    e.add_argument("--type", required=True, dest="typ", help="handoff type (e.g. distribution-help, maintainer-queue)")
    e.add_argument("--title", required=True, help="one-line human summary")
    e.add_argument("--payload", default="{}", help="inline JSON, or use --payload-file")
    e.add_argument("--payload-file", help="path to a JSON (or text) payload file")
    a = ap.parse_args()

    if a.typ not in KNOWN:
        print(f"warning: unknown type '{a.typ}' — it will be SURFACED, not routed. Known: {sorted(KNOWN)}",
              file=sys.stderr)
    if a.payload_file:
        raw = pathlib.Path(a.payload_file).read_text()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw                      # allow a plain-text payload (e.g. a markdown block)
    else:
        try:
            payload = json.loads(a.payload)
        except json.JSONDecodeError:
            sys.exit("--payload must be valid JSON (or use --payload-file for text)")

    path = emit(a.base, a.to, a.typ, a.title, payload)
    print(f"emitted {path}  (to={a.to} type={a.typ})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
