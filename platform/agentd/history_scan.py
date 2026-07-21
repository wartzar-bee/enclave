#!/usr/bin/env python3
"""history_scan.py — "has a credential ever been committed, and is it exposed?" in one command.

Written 2026-07-21, when a defect in the pre-commit hook raised exactly that question and answering
it took an hour of ad-hoc scripting. Current-tree scanning is not enough: git history is forever, so
a secret removed from the working tree is still in every clone. This walks EVERY blob in EVERY repo
(the workspace, the nested product repos, each agent vault) and reports:

  * every real-format credential in history (key formats only — no generic label=value noise)
  * whether the repo is PUBLIC, private, or has no remote  (the exposure boundary)
  * whether each finding is still in the working tree, or history-only

Findings are printed with a truncated key; the full value is never written to stdout or a log, so
running this does not itself create a new copy of the secret.

Exit 0 = no real-format credential in any history. Exit 1 = at least one found (verify + rotate).
Run:  python3 tools/security/history_scan.py [--repo <path>] [--root <dir>] [--json]
"""
import json, os, pathlib, re, subprocess, sys, urllib.error, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secrets as _sec

# Scan root: the deployment/workspace this runs against. Override with --root or ENCLAVE_SCAN_ROOT.
ROOT = pathlib.Path(os.environ.get("ENCLAVE_SCAN_ROOT", os.getcwd())).resolve()

# Patterns come from the framework's single credential definition (secrets.FORMATS) — a scanner
# with its own private copy is how five copies drifted in the first place. Formats only: a generic
# `password=<value>` rule produces hundreds of hits on code like `PASSWORD = gen_password()` and
# trains you to ignore the report, which is the failure this file exists to prevent.
PATTERNS = [(re.compile(p), kind) for p, kind in _sec.FORMATS]


def repos():
    """The workspace repo + every nested repo (product repos, agent vaults)."""
    out = [ROOT]
    for g in sorted(ROOT.glob("*/.git")) + sorted(ROOT.glob("*/*/.git")) + sorted(ROOT.glob("*/*/*/.git")):
        if g.is_dir() and g.parent != ROOT:
            out.append(g.parent)
    return out


def remote_exposure(repo):
    """(url, visibility). visibility: PUBLIC | private | unknown | none."""
    url = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"],
                         capture_output=True, text=True).stdout.strip()
    if not url:
        return "", "none"
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+)", url)
    if not m:
        return url, "unknown"
    try:  # UNAUTHENTICATED on purpose: this asks "can the world read it?", not "can I read it?"
        with urllib.request.urlopen(f"https://api.github.com/repos/{m.group(1)}", timeout=15) as r:
            return url, "PUBLIC" if r.status == 200 else "unknown"
    except urllib.error.HTTPError as e:
        return url, "private" if e.code == 404 else "unknown"
    except Exception:
        return url, "unknown"


def scan_repo(repo):
    """{key: (kind, first_path)} across every blob in every ref."""
    objs = subprocess.run(["git", "-C", str(repo), "rev-list", "--objects", "--all"],
                          capture_output=True, text=True).stdout.splitlines()
    pairs = [p for p in (l.split(maxsplit=1) for l in objs) if len(p) == 2]
    found, CH = {}, 3000
    for i in range(0, len(pairs), CH):
        chunk = pairs[i:i + CH]
        inp = "".join(s + "\n" for s, _ in chunk).encode()
        out, _ = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE).communicate(inp)
        pos = 0
        for sha, path in chunk:
            nl = out.find(b"\n", pos)
            if nl < 0:
                break
            hdr = out[pos:nl].decode("utf-8", "ignore").split()
            if len(hdr) < 3:
                pos = nl + 1
                continue
            size = int(hdr[2])
            body = out[nl + 1:nl + 1 + size]
            pos = nl + 1 + size + 1
            if size > 400_000:
                continue
            txt = body.decode("utf-8", "ignore")
            for rx, kind in PATTERNS:
                for m in rx.findall(txt):
                    found.setdefault(m, (kind, path))
    return found


def in_working_tree(repo, key):
    r = subprocess.run(["git", "-C", str(repo), "grep", "-l", key], capture_output=True, text=True)
    return bool(r.stdout.strip())


def main():
    only = None
    if "--repo" in sys.argv:
        only = pathlib.Path(sys.argv[sys.argv.index("--repo") + 1]).resolve()
    as_json = "--json" in sys.argv
    report, bad = [], 0
    for repo in repos():
        if only and repo != only:
            continue
        url, vis = remote_exposure(repo)
        found = scan_repo(repo)
        rows = []
        for key, (kind, path) in found.items():
            rows.append({"kind": kind, "key_prefix": key[:18] + "…", "len": len(key),
                         "first_path": path, "in_working_tree": in_working_tree(repo, key)})
        bad += len(rows)
        report.append({"repo": str(repo.relative_to(ROOT)) if repo != ROOT else ".",
                       "visibility": vis, "remote": url, "findings": rows})
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        for r in report:
            flag = "  <-- PUBLIC" if r["visibility"] == "PUBLIC" and r["findings"] else ""
            print(f"{r['repo']:<42} {r['visibility']:<8} findings: {len(r['findings'])}{flag}")
            for f in r["findings"]:
                where = "IN WORKING TREE" if f["in_working_tree"] else "history only"
                print(f"     {f['kind']:<20} {f['key_prefix']} ({where})  {f['first_path'][:60]}")
        print("\nRESULT:", "CLEAN — no real-format credential in any history" if not bad
              else f"{bad} credential(s) in history — verify each is DEAD, rotate any that is live")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
