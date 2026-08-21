#!/usr/bin/env python3
"""
settings_migrate.py — self-heal an existing agent's /agent/.claude/settings.json at tick boot.

runtime.sh re-syncs hooks/*.py into every pod at boot, but it has never re-synced settings.json —
so a matcher fix (e.g. adding WebFetch to the guard, closing an SSRF/IMDS bypass) reaches only
FRESH inits and stays dead on every pod already deployed. This applies the small set of matcher
migrations the framework needs — and appends any framework hook that is missing entirely (ADD_HOOKS)
— in place, preserving every operator customization.

SAFETY CONTRACT (this runs on the tick hot path for the whole fleet):
  * only ever ADDS — a tool to a matcher that is missing it, or a whole entry for a hook that is
    absent — never removes, reorders, or rewrites an existing entry;
  * writes atomically (tmp + os.replace) and ONLY when something changed;
  * on ANY error (unreadable, malformed, unwritable) it leaves the file exactly as it was and
    exits 0. The worst case is "this migration didn't apply to this pod", NEVER "this pod's
    settings.json is broken" — a corrupt settings.json would stop Claude Code from starting fleet-wide.

Idempotent: re-running is a no-op once applied. Standalone, no deps.

Usage: settings_migrate.py <path/to/settings.json>   (runtime.sh passes /agent/.claude/settings.json)
       settings_migrate.py --selftest
"""
import json, os, sys, tempfile

# hook-basename -> tools its guard PreToolUse matcher must cover. Extend here as matchers evolve.
REQUIRED = {
    "guard.py": ["WebFetch"],   # guard.decide() handles WebFetch url (SSRF/IMDS + egress); matcher omitted it
}

# hook-basename -> (event, matcher) for hooks that must EXIST on every pod, not merely be matched
# correctly. Same additive contract: if no entry under `event` runs this hook, append one; never
# remove, reorder or rewrite an existing entry.
#
# Only put a hook here if its DEFAULT behaviour is inert, because this re-adds it on every tick boot
# — an operator who deletes the entry gets it back. compactor.py qualifies: its default mode is
# `report`, which logs to state/compact.log and always allows. It does nothing at all until the
# agent sets COMPACT_MODE=enforce|spill.
ADD_HOOKS = {
    "compactor.py": ("PreToolUse", "Bash|Read"),
}


def _runs_hook(entry, basename):
    """True when this settings entry invokes a hook with this exact file basename. Token match, not
    substring: "guard.py" must not also match "delegation_guard.py"."""
    return basename in {os.path.basename(t) for h in (entry.get("hooks") or [])
                        for t in (h.get("command", "") or "").split()}


def _add_missing_hooks(data):
    """Append any ADD_HOOKS entry the file does not already have. Returns True if it changed."""
    changed = False
    for basename, (event, matcher) in ADD_HOOKS.items():
        hooks = data.setdefault("hooks", {})
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
            hooks[event] = entries
        if any(_runs_hook(e, basename) for e in entries):
            continue
        entries.append({"matcher": matcher, "hooks": [
            {"type": "command", "command": f"python3 /agent/.claude/hooks/{basename}"}]})
        changed = True
    return changed


def migrate(data):
    """Pure: take a parsed settings dict, return (changed, data). Adds missing tools to the matcher
    of any PreToolUse entry whose command runs a hook named in REQUIRED, and appends any hook in
    ADD_HOOKS that is absent entirely."""
    changed = _add_missing_hooks(data)
    pre = (data.get("hooks") or {}).get("PreToolUse") or []
    for entry in pre:
        for hook_name, needed in REQUIRED.items():
            if not _runs_hook(entry, hook_name):
                continue
            matcher = entry.get("matcher", "")
            if not matcher:
                continue
            tokens = matcher.split("|")
            for tool in needed:
                if tool not in tokens:
                    tokens.append(tool)
                    changed = True
            entry["matcher"] = "|".join(tokens)
    return changed, data


def main(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        data = json.loads(raw)
    except Exception:
        return 0  # unreadable / malformed → do nothing, never wedge the pod
    try:
        changed, data = migrate(data)
        if not changed:
            return 0
        out = json.dumps(data, indent=2) + "\n"
        d = os.path.dirname(os.path.abspath(path)) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(out)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception:
        return 0  # any write failure → leave the original untouched
    return 0


def _selftest():
    fails = []
    def ck(n, c):
        if not c: fails.append(n)

    base = {"hooks": {"PreToolUse": [
        {"matcher": "Bash|Read|Edit|Write|NotebookEdit",
         "hooks": [{"type": "command", "command": "python3 /agent/.claude/hooks/guard.py"}]},
        {"matcher": "Write|Edit|MultiEdit",
         "hooks": [{"type": "command", "command": "python3 /agent/.claude/hooks/delegation_guard.py"}]},
        {"matcher": "Write|Edit|MultiEdit",
         "hooks": [{"type": "command", "command": "python3 /agent/.claude/hooks/secret_scan.py"}]},
    ]}}
    ch, out = migrate(json.loads(json.dumps(base)))
    ck("adds-webfetch", ch and "WebFetch" in out["hooks"]["PreToolUse"][0]["matcher"])
    ck("guard-order-preserved", out["hooks"]["PreToolUse"][0]["matcher"].startswith("Bash|Read|Edit|Write|NotebookEdit"))
    # "guard.py" must NOT match "delegation_guard.py" — its matcher stays untouched
    ck("leaves-delegation-guard", out["hooks"]["PreToolUse"][1]["matcher"] == "Write|Edit|MultiEdit")
    ck("leaves-secret-scan", out["hooks"]["PreToolUse"][2]["matcher"] == "Write|Edit|MultiEdit")
    # and an already-current settings (guard has WebFetch, compactor present) → no change at all
    cur = {"hooks": {"PreToolUse": [
        {"matcher": "Bash|WebFetch", "hooks": [{"command": "python3 /agent/.claude/hooks/guard.py"}]},
        {"matcher": "Write|Edit", "hooks": [{"command": "python3 /agent/.claude/hooks/delegation_guard.py"}]},
        {"matcher": "Bash|Read", "hooks": [{"command": "python3 /agent/.claude/hooks/compactor.py"}]}]}}
    ck("current-is-noop", migrate(json.loads(json.dumps(cur)))[0] is False)

    # --- ADD_HOOKS: a whole missing entry is appended, exactly once, without disturbing the rest ---
    ck("adds-compactor", any(e.get("matcher") == "Bash|Read" and _runs_hook(e, "compactor.py")
                             for e in out["hooks"]["PreToolUse"]))
    ck("adds-compactor-once", sum(_runs_hook(e, "compactor.py") for e in out["hooks"]["PreToolUse"]) == 1)
    ck("append-not-prepend", not _runs_hook(out["hooks"]["PreToolUse"][0], "compactor.py"))
    # an operator's custom matcher for the same hook is respected, not duplicated or rewritten
    custom = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"command": "python3 /agent/.claude/hooks/compactor.py"}]}]}}
    ch3, out3 = migrate(json.loads(json.dumps(custom)))
    ck("respects-custom-matcher", ch3 is False and out3["hooks"]["PreToolUse"][0]["matcher"] == "Bash")
    # a settings.json with no hooks at all still gets it, and stays valid
    ch4, out4 = migrate({})
    ck("adds-to-empty", ch4 and _runs_hook(out4["hooks"]["PreToolUse"][0], "compactor.py"))
    # a malformed event value is replaced rather than crashing
    ch5, out5 = migrate({"hooks": {"PreToolUse": "not-a-list"}})
    ck("survives-malformed-event", ch5 and isinstance(out5["hooks"]["PreToolUse"], list))
    # idempotent
    ch2, out2 = migrate(out)
    ck("idempotent", (not ch2) and out2["hooks"]["PreToolUse"][0]["matcher"].count("WebFetch") == 1)
    # already-correct → no change
    good = {"hooks": {"PreToolUse": [
        {"matcher": "Bash|WebFetch", "hooks": [{"command": "python3 x/guard.py"}]},
        {"matcher": "Bash|Read", "hooks": [{"command": "python3 /agent/.claude/hooks/compactor.py"}]}]}}
    ck("no-change-when-present", migrate(json.loads(json.dumps(good)))[0] is False)
    # malformed file on disk → main() returns 0 and does not raise
    import tempfile as _tf
    fd, p = _tf.mkstemp(); os.write(fd, b"{not json"); os.close(fd)
    try:
        ck("malformed-safe", main(p) == 0)
        ck("malformed-untouched", open(p).read() == "{not json")
    finally:
        os.unlink(p)
    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if len(sys.argv) < 2:
        sys.stderr.write("usage: settings_migrate.py <settings.json>\n")
        sys.exit(0)
    sys.exit(main(sys.argv[1]))
