#!/usr/bin/env python3
"""
propagation_check.py — verify the Phase 0 security fixes actually REACHED every pod.

Some fixes don't auto-propagate to already-deployed pods:
  * the WebFetch guard matcher reaches existing pods only via settings_migrate at tick boot;
  * the tightened egress policy (no *.amazonaws.com) is copied at INIT and never re-synced;
  * secret_scan's import fix rides the hooks/*.py re-sync (should be automatic — verified anyway).

So "I committed the fix" is NOT "every pod has the fix" (the operator's own lesson: a fix that changes
nothing never ran). This is a READ-ONLY audit: for each pod home it reports whether each fix is present,
and exits non-zero if ANY pod is confirmed stale. Run it before flipping egress to fail-closed (1.9) —
a pod without the tightened policy would go from report-only straight to bricked-egress.

Usage:
  propagation_check.py --fleet-root ~/my-fleet   # audit every <root>/*/home
  propagation_check.py --home /path/to/home [--home ...]          # audit specific homes
  propagation_check.py --selftest
Exit: 0 = all present (or only unknowns), 1 = at least one pod confirmed STALE.
"""
import argparse, json, pathlib, sys

# True = fix present, False = confirmed stale, None = can't tell (file absent)
def _webfetch_wired(home):
    sf = pathlib.Path(home) / ".claude" / "settings.json"
    try:
        data = json.loads(sf.read_text())
    except Exception:
        return None
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
        if "guard.py" in cmds:
            return "WebFetch" in (entry.get("matcher", "") or "").split("|")
    return None  # no guard entry found


def _secret_scan_fixed(home):
    ss = pathlib.Path(home) / ".claude" / "hooks" / "secret_scan.py"
    try:
        txt = ss.read_text()
    except Exception:
        return None
    # the fix loads secrets.py by file path (spec_from_file_location); the broken version did a bare
    # `import secrets as _sec` that silently binds Python's stdlib module.
    return "spec_from_file_location" in txt


def _egress_tightened(home):
    pol = pathlib.Path(home) / ".claude" / "hooks" / "policies" / "default-egress.json"
    try:
        data = json.loads(pol.read_text())
    except Exception:
        return None
    hosts = [e.get("host", "") for e in data.get("allow", [])] + \
            [e.get("host", "") for e in data.get("binary_allow", [])]
    return "*.amazonaws.com" not in hosts


CHECKS = [("webfetch_wired", _webfetch_wired),
          ("secret_scan_fixed", _secret_scan_fixed),
          ("egress_tightened", _egress_tightened)]


def check_home(home):
    return {name: fn(home) for name, fn in CHECKS}


def _sym(v):
    return {True: "ok  ", False: "STALE", None: "  ? "}[v]


def audit(homes):
    """Return (rows, any_stale, egress_unknown). rows = [(home, {check: bool|None})].
    egress_unknown flags pods with NO egress policy file — those are the MOST dangerous to enable
    fail-closed against (a missing policy under enforce blocks all egress), so the gate must not pass
    them silently even though the check can't read a value."""
    rows = [(str(h), check_home(h)) for h in homes]
    any_stale = any(v is False for _, res in rows for v in res.values())
    egress_unknown = [h for h, res in rows if res["egress_tightened"] is None]
    return rows, any_stale, egress_unknown


def _discover(fleet_root):
    root = pathlib.Path(fleet_root).expanduser()
    return sorted(p / "home" for p in root.iterdir()
                  if (p / "home" / ".claude").is_dir()) if root.is_dir() else []


def _selftest():
    import tempfile
    fails = []
    def ck(n, c):
        if not c: fails.append(n)

    with tempfile.TemporaryDirectory() as d:
        root = pathlib.Path(d)
        def mk(name, matcher, scan_txt, hosts):
            h = root / name / "home"
            (h / ".claude" / "hooks" / "policies").mkdir(parents=True)
            (h / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": matcher, "hooks": [{"command": "python3 /agent/.claude/hooks/guard.py"}]}]}}))
            (h / ".claude" / "hooks" / "secret_scan.py").write_text(scan_txt)
            (h / ".claude" / "hooks" / "policies" / "default-egress.json").write_text(
                json.dumps({"allow": [{"host": x} for x in hosts], "binary_allow": []}))
            return h

        good = mk("good", "Bash|WebFetch", "x = spec_from_file_location('a','b')", ["api.anthropic.com"])
        stale = mk("stale", "Bash|Read|Edit", "import secrets as _sec", ["*.amazonaws.com"])

        rg = check_home(good)
        ck("good-webfetch", rg["webfetch_wired"] is True)
        ck("good-scan", rg["secret_scan_fixed"] is True)
        ck("good-egress", rg["egress_tightened"] is True)
        rs = check_home(stale)
        ck("stale-webfetch", rs["webfetch_wired"] is False)
        ck("stale-scan", rs["secret_scan_fixed"] is False)
        ck("stale-egress", rs["egress_tightened"] is False)

        rows, any_stale, egress_unknown = audit(_discover(root))
        ck("discovers-two", len(rows) == 2)
        ck("flags-stale", any_stale is True)

        # a home with the OTHER files but no egress policy → egress flagged (dangerous for fail-closed)
        empty = root / "empty" / "home"
        (empty / ".claude").mkdir(parents=True)
        r_empty = check_home(empty)
        ck("unknown-not-stale", all(v is None for v in r_empty.values()))
        _, only_unknown_stale, eu = audit([empty])
        ck("unknown-not-counted-stale", only_unknown_stale is False)
        ck("egress-absent-flagged", str(empty) in eu)

    print(("selftest FAIL: " + ", ".join(fails)) if fails else "selftest OK")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description="Audit which pods have the Phase 0 security fixes.")
    ap.add_argument("--fleet-root", help="audit every <root>/*/home")
    ap.add_argument("--home", action="append", default=[], help="audit a specific home (repeatable)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    homes = list(a.home) + (_discover(a.fleet_root) if a.fleet_root else [])
    if not homes:
        ap.error("give --fleet-root or one/more --home (or --selftest)")
    rows, any_stale, egress_unknown = audit(homes)
    width = max((len(h) for h, _ in rows), default=4)
    print(f"{'POD HOME':<{width}}  " + "  ".join(n for n, _ in CHECKS))
    for h, res in rows:
        print(f"{h:<{width}}  " + "  ".join(f"{_sym(res[n]):>{max(len(n),5)}}" for n, _ in CHECKS))
    if egress_unknown:
        print(f"\nEGRESS POLICY ABSENT on {len(egress_unknown)} pod(s) — do NOT enable fail-closed egress "
              f"there (a missing policy blocks all egress under enforce):")
        for h in egress_unknown:
            print(f"  ? {h}")
    if any_stale:
        print("\nSTALE — some pods are missing a fix (see above)")
    elif not egress_unknown:
        print("\nall pods current")
    return 1 if (any_stale or egress_unknown) else 0


if __name__ == "__main__":
    sys.exit(main())
