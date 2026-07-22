#!/usr/bin/env python3
"""Suite 1 — static checks on the Enclave fleet console's embedded frontend (console.PAGE).

Fast, no browser, stdlib-only (plus `node --check` as a subprocess). ALWAYS runs in CI.

What it guards:
  1. The inline <script> block in console.PAGE is SYNTACTICALLY VALID JavaScript (`node --check`).
     A broken edit to the embedded JS fails here, not in a user's browser. (Skips with a printed
     note only if `node` is absent — the image bakes node, so on a dev box it should run.)
  2. The PAGE still contains the structural anchors the app needs: the nav views, the per-agent
     tabs, the create-modal trigger, and the create-modal fields (provider/model/escalation/
     clone-from/work). Asserts on the EXACT ids/tokens the code uses, so accidental removal trips.
  3. No frontend->backend drift: every /api/... path the JS calls is actually served by console.py's
     do_GET/do_POST. Catches a renamed/removed endpoint.
  4. The vendored static assets the page <script src>'s exist on disk under console.STATIC.

Run:  python3 test_frontend_static.py     (exit 0 = pass/skip, non-zero = real failure)
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F  # noqa: E402
import console              # noqa: E402

check = F.Check()

SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.DOTALL | re.IGNORECASE)
APIPATH_RE = re.compile(r"/api/[A-Za-z0-9_./-]*")


def _script_blocks(html):
    return [b.strip() for b in SCRIPT_RE.findall(html) if b.strip()]


def _server_paths():
    """Parse the literal request paths console.py routes (do_GET/do_POST `p == "..."`)."""
    src = (HERE / "console.py").read_text()
    paths = set(re.findall(r'p\s*==\s*"(/[A-Za-z0-9_./-]+)"', src))
    paths.update(re.findall(r'p\.startswith\("(/[A-Za-z0-9_./-]+)"\)', src))
    return paths


def main():
    page = console.PAGE
    check("PAGE is a non-empty string", isinstance(page, str) and len(page) > 1000,
          f"len={len(page) if isinstance(page, str) else 'n/a'}")
    check("PAGE has no leftover __PLACEHOLDER__ tokens (served verbatim)",
          not re.search(r"__[A-Z]+__", page), str(set(re.findall(r"__[A-Z]+__", page))))

    blocks = _script_blocks(page)
    check("exactly one non-empty inline <script> block", len(blocks) == 1, f"found {len(blocks)}")
    js = blocks[0] if blocks else ""
    check("inline JS is substantial", len(js) > 5000, f"len={len(js)}")

    # ---- 1. node --check the inline JS ------------------------------------------------------------
    node = shutil.which("node") or os.path.expanduser("~/.local/bin/node")
    if not (node and os.path.exists(node)):
        node = shutil.which("node")
    if node and os.path.exists(str(node)):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(js)
            jspath = f.name
        try:
            r = subprocess.run([str(node), "--check", jspath], capture_output=True, text=True)
        finally:
            os.unlink(jspath)
        ok = r.returncode == 0
        check("node --check: inline JS is syntactically valid", ok, r.stderr.strip()[:400])
    else:
        print("  NOTE: node not on PATH — skipping `node --check` syntax gate (non-blocking)")

    # ---- 1b. every element id is UNIQUE -----------------------------------------------------------
    # getElementById returns the FIRST match, so a duplicate id does not error — it silently wires a
    # handler to the wrong element. Adding an agent Pause button as a second #pausebtn meant
    # syncPauseBtn() relabelled the nav's auto-refresh ⏸ instead: the word "Resume" appeared in a
    # different row from the agent controls, and the real button never updated. Nothing failed; it
    # just moved. This is the cheapest possible guard against a whole class of that.
    ids = re.findall(r'\bid="([A-Za-z0-9_-]+)"', page)
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    check("no duplicate element ids in PAGE", not dupes, f"duplicated: {dupes}")

    # ---- 2. structural anchors -------------------------------------------------------------------
    # nav views: data-v + view(...) + matching #view-<x> container.  ("Audit" label -> activity view)
    nav = dict(re.findall(r'data-v="([a-z]+)"[^>]*>([^<]+)<', page))
    for vid, label in [("overview", "Overview"), ("agents", "Agents"), ("monitor", "Monitor"),
                       ("activity", "Audit"), ("models", "Models")]:
        check(f"nav tab present: {label} (data-v={vid})", nav.get(vid) == label,
              f"got {nav.get(vid)!r}")
        check(f"nav view container present: #view-{vid}", f'id="view-{vid}"' in page)

    # per-agent tabs (data-t).  Note: Status == the merged diag tab; Diagnostics is its section title.
    tabs = dict(re.findall(r'data-t="([a-z]+)"[^>]*>([^<]+)<', page))
    for tid, label in [("chat", "Chat"), ("activity", "Activity"), ("diag", "Status"),
                       ("config", "Config"), ("skills", "Skills"), ("logs", "Logs")]:
        check(f"agent tab present: {label} (data-t={tid})", tabs.get(tid) == label,
              f"got {tabs.get(tid)!r}")
    check("Diagnostics profiler section title present", "Diagnostics" in page)

    # create-modal trigger + modal + recently-shipped fields
    check("create-agent trigger present (+ New Agent / openNew)",
          "+ New Agent" in page and "openNew(" in page)
    check("create modal present (#newmodal)", 'id="newmodal"' in page)
    for fid, why in [("n_name", "name"), ("n_brain", "brain claude/api select"),
                     ("n_provider", "provider select"), ("n_model", "model-by-provider dropdown"),
                     ("n_escmodel", "escalation model"), ("n_escrow", "escalation row"),
                     ("n_provrow", "provider row"), ("n_clonefrom", "clone-from"),
                     ("n_clonework", "work checkbox")]:
        check(f"create-modal field present: #{fid} ({why})", f'id="{fid}"' in page)
    check("brain select offers claude + api options",
          re.search(r'id="n_brain"[^>]*>.*?claude.*?api', page, re.DOTALL) is not None)
    check("brain change re-fills model list (fillNewModels)", "fillNewModels(" in page)
    check("provider change updates models (provChange)", "provChange(" in page)
    check("work checkbox is a real checkbox input",
          re.search(r'type="checkbox"\s+id="n_clonework"', page) is not None)

    # ---- 3. frontend -> backend endpoint drift ---------------------------------------------------
    server = _server_paths()
    check("server routes parsed from console.py", len(server) >= 10, f"found {len(server)}")
    client = sorted(set(APIPATH_RE.findall(js)))
    check("client /api calls discovered in JS", len(client) >= 10, f"found {len(client)}")
    missing = [c for c in client if c not in server]
    check("every /api path the JS calls is served by the backend", not missing,
          f"orphans: {missing}")
    # spot-check a few critical ones are genuinely on both sides
    for crit in ["/api/fleet", "/api/overview", "/api/create", "/api/config",
                 "/api/monitor/control", "/api/stream"]:
        check(f"endpoint wired both sides: {crit}", crit in client and crit in server,
              f"client={crit in client} server={crit in server}")

    # ---- 4. vendored static assets exist ---------------------------------------------------------
    static = pathlib.Path(console.STATIC)
    for asset in ["chart.umd.min.js", "force-graph.min.js"]:
        check(f"<script src> references /static/{asset}", f"/static/{asset}" in page)
        check(f"vendored asset exists on disk: {asset}", (static / asset).is_file(),
              str(static / asset))

    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
