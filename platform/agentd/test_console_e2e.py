#!/usr/bin/env python3
"""Suite 2 — Playwright headless E2E for the Enclave fleet console.

Boots the REAL console on a temp fleet (tests_fixtures.boot_console, no docker) and drives the
rendered page in a headless browser, asserting it renders with no uncaught JS errors and that the
nav views, per-agent tabs, and the New-Agent create modal actually work.

SELF-SKIPS cleanly (prints a reason, exits 0) when no browser/driver is available — a skip must
never fail CI. Browser detection order:
  (a) Python Playwright (`import playwright`)
  (b) Node Playwright  (businesses/devtools/node_modules/playwright)  <- the one present on this host
  (c) host browser bridge on 127.0.0.1:18184  (probed; not sufficient for the interactive drive, so
      if it is the ONLY thing up we still skip with a clear note)

ENCLAVE_E2E=1 forces a hard failure if it cannot find a browser (instead of skipping) — useful in a
CI lane where the browser is guaranteed; otherwise a missing browser is a clean skip.

Run:  python3 test_console_e2e.py
"""
import datetime
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import tests_fixtures as F  # noqa: E402

DEVTOOLS = (HERE / "../../../devtools").resolve()
FORCE = os.environ.get("ENCLAVE_E2E") == "1"


def _skip(reason):
    print(f"SKIP: {reason}")
    if FORCE:
        print("  (ENCLAVE_E2E=1 set -> treating missing browser as a FAILURE)")
        raise SystemExit(1)
    raise SystemExit(0)


def _have_python_playwright():
    try:
        import playwright  # noqa: F401
        return True
    except Exception:
        return False


def _node_playwright():
    """Return (node_bin, cwd_with_playwright) if a runnable node+playwright pair exists, else None."""
    node = shutil.which("node") or (os.path.expanduser("~/.local/bin/node")
                                    if os.path.exists(os.path.expanduser("~/.local/bin/node")) else None)
    if not node:
        return None
    pw = DEVTOOLS / "node_modules" / "playwright"
    if pw.is_dir():
        return (node, str(DEVTOOLS))
    return None


def _bridge_up():
    try:
        with socket.create_connection(("127.0.0.1", 18184), timeout=1):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- the assertions
def _assert(check, res, hard, soft):
    """res = dict the driver returned; hard/soft = error lists. Shared by both driver backends."""
    check("page rendered (title contains Enclave)", "Enclave" in (res.get("title") or ""),
          f"title={res.get('title')!r}")
    check("app shell present (ENCLAVE brand)", res.get("hasBrand") is True)
    check("fixture agents loaded from /api/fleet", (res.get("agentCount") or 0) >= 1,
          f"count={res.get('agentCount')}")

    views = res.get("views") or {}
    for v in ["overview", "agents", "monitor", "activity", "models"]:
        check(f"nav view becomes visible: {v}", views.get(v) is True, f"visible={views.get(v)}")

    check("an agent was selected", bool(res.get("agent")), f"agent={res.get('agent')!r}")
    tabs = res.get("tabs") or {}
    for t in ["chat", "activity", "diag", "config", "skills", "logs"]:
        check(f"per-agent tab activates: {t}", tabs.get(t) is True, f"active={tabs.get(t)}")

    check("create modal opens", res.get("modalOpen") is True)
    check("brain=api reveals provider row", res.get("provRowVisible") is True)
    check("brain=api reveals escalation row", res.get("escRowVisible") is True)
    check("provider select is populated", (res.get("providerOpts") or 0) >= 2,
          f"opts={res.get('providerOpts')}")
    check("switching provider updates the model list", res.get("modelListChanged") is True,
          f"before={str(res.get('modelBefore'))[:60]!r} after={str(res.get('modelAfter'))[:60]!r}")
    check("create modal closes", res.get("modalClosed") is True)

    # error gate: uncaught exceptions / real console.error fail; network noise (the chat iframe to a
    # not-running agent port -> connection refused) is expected and ignored.
    check("no uncaught page errors / console.error", not hard, "; ".join(hard)[:500])
    if soft:
        print(f"  (info: {len(soft)} benign network-noise messages ignored, "
              f"e.g. {soft[0][:80]!r})")


# --------------------------------------------------------------------------- node-playwright driver
_NODE_DRIVER = r"""
const { chromium } = require('playwright');
const base = process.argv[2];
const NET_NOISE = /ERR_CONNECTION_REFUSED|ERR_CONNECTION|Failed to load resource|net::ERR|ERR_NAME_NOT_RESOLVED|favicon|the server responded with a status/i;
(async () => {
  const hard = [], soft = [], res = {};
  let browser;
  try {
    browser = await chromium.launch({ headless: true });
    const page = await browser.newPage();
    page.on('pageerror', e => hard.push('pageerror: ' + e.message));
    page.on('console', m => {
      if (m.type() !== 'error') return;
      const t = m.text();
      (NET_NOISE.test(t) ? soft : hard).push(t);
    });
    page.on('requestfailed', r => soft.push('reqfail ' + r.url()));

    await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForFunction(() => typeof agents !== 'undefined' && Object.keys(agents).length > 0,
                               { timeout: 10000 }).catch(() => {});
    res.title = await page.title();
    res.hasBrand = (await page.locator('.brand', { hasText: 'ENCLAVE' }).count()) > 0;
    res.agentCount = await page.evaluate(() => (typeof agents !== 'undefined' ? Object.keys(agents).length : 0));

    res.views = {};
    for (const v of ['overview', 'agents', 'monitor', 'activity', 'models']) {
      await page.click(`.navtab[data-v="${v}"]`);
      await page.waitForTimeout(350);
      res.views[v] = await page.evaluate((v) => {
        const el = document.getElementById('view-' + v);
        if (!el) return false;
        return getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
      }, v);
    }

    await page.evaluate(() => view('agents'));
    const aid = await page.evaluate(() => Object.keys(agents)[0]);
    res.agent = aid;
    await page.evaluate((id) => pick(id), aid);
    await page.waitForTimeout(300);
    res.tabs = {};
    for (const t of ['chat', 'activity', 'diag', 'config', 'skills', 'logs']) {
      await page.click(`.tab[data-t="${t}"]`).catch(() => {});
      await page.waitForTimeout(450);
      res.tabs[t] = await page.evaluate((t) => {
        const el = document.querySelector(`.tab[data-t="${t}"]`);
        return el ? el.classList.contains('sel') : false;
      }, t);
    }

    // create modal: switch brain claude->api, check rows appear, switch provider, check models update
    await page.evaluate(() => view('agents'));
    await page.click('button[onclick="openNew()"]');
    await page.waitForTimeout(500);
    res.modalOpen = await page.evaluate(() => getComputedStyle(document.getElementById('newmodal')).display !== 'none');
    await page.selectOption('#n_brain', 'api');
    await page.waitForTimeout(500);
    res.provRowVisible = await page.evaluate(() => getComputedStyle(document.getElementById('n_provrow')).display !== 'none');
    res.escRowVisible  = await page.evaluate(() => getComputedStyle(document.getElementById('n_escrow')).display !== 'none');
    const provVals = await page.evaluate(() => Array.from(document.getElementById('n_provider').options).map(o => o.value));
    res.providerOpts = provVals.length;
    res.modelBefore = await page.evaluate(() => Array.from(document.getElementById('n_model').options).map(o => o.value || o.textContent).join('|'));
    const cur = await page.evaluate(() => document.getElementById('n_provider').value);
    const other = provVals.find(v => v && v !== cur && v !== 'custom') || provVals.find(v => v !== cur);
    if (other) { await page.selectOption('#n_provider', other); await page.waitForTimeout(500); }
    res.modelAfter = await page.evaluate(() => Array.from(document.getElementById('n_model').options).map(o => o.value || o.textContent).join('|'));
    res.modelListChanged = res.modelBefore !== res.modelAfter;
    await page.evaluate(() => closeNew());
    await page.waitForTimeout(200);
    res.modalClosed = await page.evaluate(() => getComputedStyle(document.getElementById('newmodal')).display === 'none');
  } catch (e) {
    hard.push('driver-exception: ' + (e && e.message ? e.message : String(e)));
  } finally {
    if (browser) { try { await browser.close(); } catch (e) {} }
  }
  process.stdout.write(JSON.stringify({ res, hard, soft }));
})();
"""


def _run_node(base, node, cwd):
    driver = pathlib.Path(os.environ.get(
        "TMPDIR", "/tmp")) / "enclave_e2e_driver.js"
    driver.write_text(_NODE_DRIVER)
    env = dict(os.environ)
    # node resolves require() relative to the SCRIPT dir (in TMPDIR), not cwd — point it at the
    # devtools node_modules so `require('playwright')` resolves.
    nm = str(pathlib.Path(cwd) / "node_modules")
    env["NODE_PATH"] = nm + (os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else "")
    # toolbit's e2e uses a linux LD_LIBRARY_PATH; harmless on mac, helps if run in the linux container
    cflibs = os.path.expanduser("~/.local/cflibs/usr/lib/aarch64-linux-gnu")
    if os.path.isdir(cflibs):
        env["LD_LIBRARY_PATH"] = cflibs + ":" + env.get("LD_LIBRARY_PATH", "")
    r = subprocess.run([node, str(driver), base], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip()
    try:
        # the driver writes a single JSON object; tolerate any stray pre-output
        data = json.loads(out[out.index("{"):])
    except Exception as e:
        raise RuntimeError(f"node driver gave no JSON (rc={r.returncode}): "
                           f"{(r.stderr or out)[:600]}") from e
    return data["res"], data["hard"], data["soft"]


# --------------------------------------------------------------------------- python-playwright driver
def _run_python(base):
    from playwright.sync_api import sync_playwright  # type: ignore
    res, hard, soft = {}, [], []
    import re
    noise = re.compile(r"ERR_CONNECTION|Failed to load resource|net::ERR|favicon|"
                       r"the server responded with a status", re.I)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("pageerror", lambda e: hard.append("pageerror: " + str(e)))

        def _con(m):
            if m.type == "error":
                (soft if noise.search(m.text) else hard).append(m.text)
        page.on("console", _con)
        page.on("requestfailed", lambda r: soft.append("reqfail " + r.url))
        try:
            page.goto(base, wait_until="domcontentloaded", timeout=15000)
            try:
                page.wait_for_function(
                    "() => typeof agents!=='undefined' && Object.keys(agents).length>0", timeout=10000)
            except Exception:
                pass
            res["title"] = page.title()
            res["hasBrand"] = page.locator(".brand", has_text="ENCLAVE").count() > 0
            res["agentCount"] = page.evaluate(
                "() => (typeof agents!=='undefined'?Object.keys(agents).length:0)")
            res["views"] = {}
            for v in ["overview", "agents", "monitor", "activity", "models"]:
                page.click(f'.navtab[data-v="{v}"]')
                page.wait_for_timeout(350)
                res["views"][v] = page.evaluate(
                    "(v)=>{const el=document.getElementById('view-'+v);return !!el && "
                    "getComputedStyle(el).display!=='none' && el.offsetParent!==null;}", v)
            page.evaluate("() => view('agents')")
            aid = page.evaluate("() => Object.keys(agents)[0]")
            res["agent"] = aid
            page.evaluate("(id) => pick(id)", aid)
            page.wait_for_timeout(300)
            res["tabs"] = {}
            for t in ["chat", "activity", "diag", "config", "skills", "logs"]:
                try:
                    page.click(f'.tab[data-t="{t}"]')
                except Exception:
                    pass
                page.wait_for_timeout(450)
                res["tabs"][t] = page.evaluate(
                    "(t)=>{const el=document.querySelector(`.tab[data-t=\"${t}\"]`);"
                    "return el?el.classList.contains('sel'):false;}", t)
            page.evaluate("() => view('agents')")
            page.click('button[onclick="openNew()"]')
            page.wait_for_timeout(500)
            res["modalOpen"] = page.evaluate(
                "()=>getComputedStyle(document.getElementById('newmodal')).display!=='none'")
            page.select_option("#n_brain", "api")
            page.wait_for_timeout(500)
            res["provRowVisible"] = page.evaluate(
                "()=>getComputedStyle(document.getElementById('n_provrow')).display!=='none'")
            res["escRowVisible"] = page.evaluate(
                "()=>getComputedStyle(document.getElementById('n_escrow')).display!=='none'")
            prov_vals = page.evaluate(
                "()=>Array.from(document.getElementById('n_provider').options).map(o=>o.value)")
            res["providerOpts"] = len(prov_vals)
            res["modelBefore"] = page.evaluate(
                "()=>Array.from(document.getElementById('n_model').options).map(o=>o.value||o.textContent).join('|')")
            cur = page.evaluate("()=>document.getElementById('n_provider').value")
            other = next((v for v in prov_vals if v and v != cur and v != "custom"),
                         next((v for v in prov_vals if v != cur), None))
            if other:
                page.select_option("#n_provider", other)
                page.wait_for_timeout(500)
            res["modelAfter"] = page.evaluate(
                "()=>Array.from(document.getElementById('n_model').options).map(o=>o.value||o.textContent).join('|')")
            res["modelListChanged"] = res["modelBefore"] != res["modelAfter"]
            page.evaluate("() => closeNew()")
            page.wait_for_timeout(200)
            res["modalClosed"] = page.evaluate(
                "()=>getComputedStyle(document.getElementById('newmodal')).display==='none'")
        except Exception as e:
            hard.append("driver-exception: " + str(e))
        finally:
            browser.close()
    return res, hard, soft


def main():
    # pick a driver
    backend = None
    if _have_python_playwright():
        backend = ("python", None)
    else:
        npw = _node_playwright()
        if npw:
            backend = ("node", npw)
    if not backend:
        extra = " (host browser bridge :18184 is up but cannot drive the interactive flow)" \
            if _bridge_up() else ""
        _skip("no browser/playwright available — install Python playwright or node playwright"
              + extra)

    kind = backend[0]
    print(f"E2E driver: {kind}-playwright"
          + (f" @ {backend[1][1]}" if kind == "node" else ""))

    # usage records with ISO-8601 'Z' timestamps — the PRODUCTION shape (usage._parse_ts parses a
    # string; the dashboard JS does ts.slice/replace). build_fleet uses setdefault("ts",...) so our
    # ts wins; this keeps the page's recent-ticks / diagnostics-inspect renders from throwing on a
    # numeric epoch (which is the fixture default, not what a real agent writes).
    def _iso(mins_ago):
        return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=mins_ago)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
    usage = [{"reason": "heartbeat", "model": "claude-sonnet-4-6", "input": 500, "output": 1200,
              "cache_read": 900000, "cache_write": 12000, "cost_usd": 1.1, "duration_s": 42,
              "turns": 18, "rc": 0, "subtype": "success", "ts": _iso(120)},
             {"reason": "continue", "model": "claude-sonnet-4-6", "input": 480, "output": 1300,
              "cache_read": 1100000, "cache_write": 9000, "cost_usd": 1.4, "duration_s": 51,
              "turns": 22, "rc": 0, "subtype": "success", "ts": _iso(30)}]
    root = F.build_fleet(specs=[
        {"id": "alpha", "headline": "alpha builds the thing", "usage": usage},
        {"id": "beta", "brain": "api", "model": "qwen", "usage": usage}])
    con, base, stop = F.boot_console(root)
    check = F.Check()
    try:
        if kind == "python":
            res, hard, soft = _run_python(base)
        else:
            node, cwd = backend[1]
            res, hard, soft = _run_node(base, node, cwd)
        _assert(check, res, hard, soft)
    finally:
        stop()
    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
