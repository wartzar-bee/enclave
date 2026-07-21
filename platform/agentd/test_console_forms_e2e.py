#!/usr/bin/env python3
"""Suite 2b — Playwright headless audit of the fleet console's FORMS and POPUPS.

Boots the REAL console on a temp fleet (tests_fixtures.boot_console, no docker) and drives the
rendered page in a headless browser to empirically audit:

  1. CREATE MODAL FIELDS  — every field present + of the right kind; template/brain option sets.
  2. CONDITIONAL VISIBILITY ("permissions") — brain=claude hides provider/escalation rows; brain=api
     shows them; provider=custom reveals the custom base/key-env block; the model <select> actually
     repopulates when brain/provider changes.
  3. VALIDATION + network intercept — an invalid name (space / empty) shows an inline #n_msg error and
     fires NO POST /api/create; a valid name DOES fire the POST.
  4. POPUP CLOSE AUDIT — for the create modal, the bell notification panel, the info `i` tooltip and
     the diagnostics tick inspector, every dismissal method is tried and the result recorded
     (works / broken). The CURRENT (sometimes broken) behaviour is encoded so the test is green;
     UX gaps are flagged in comments + the printed report (look for "UX GAP").

Same opt-in/self-skip contract as test_console_e2e.py: a missing browser is a CLEAN SKIP (exit 0);
ENCLAVE_E2E=1 turns a missing browser into a FAILURE. Backend = node-Playwright
(a local node_modules), the one present on this host.

Run:  python3 test_console_forms_e2e.py
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


# --------------------------------------------------------------------------- node-playwright driver
_NODE_DRIVER = r"""
const { chromium } = require('playwright');
const base = process.argv[2];
const NET_NOISE = /ERR_CONNECTION_REFUSED|ERR_CONNECTION|Failed to load resource|net::ERR|ERR_NAME_NOT_RESOLVED|favicon|the server responded with a status/i;

const disp = (page, sel) => page.evaluate(s => {
  const el = document.querySelector(s);
  if (!el) return '__missing__';
  return getComputedStyle(el).display;
}, sel);
const visible = (page, sel) => page.evaluate(s => {
  const el = document.querySelector(s);
  return !!el && getComputedStyle(el).display !== 'none' && el.offsetParent !== null;
}, sel);
const optVals = (page, id) => page.evaluate(i => {
  const el = document.getElementById(i);
  return el ? Array.from(el.options).map(o => o.value || o.textContent) : null;
}, id);
const exists = (page, sel) => page.evaluate(s => !!document.querySelector(s), sel);

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

    // intercept create POSTs (validation gate)
    let createPosts = [];
    page.on('request', r => {
      if (r.method() === 'POST' && r.url().includes('/api/create')) createPosts.push(r.url());
    });

    const goHome = async () => {
      await page.goto(base, { waitUntil: 'domcontentloaded', timeout: 15000 });
      await page.waitForFunction(
        () => typeof agents !== 'undefined' && Object.keys(agents).length > 0,
        { timeout: 10000 }).catch(() => {});
    };
    await goHome();
    res.title = await page.title();

    // ---------------------------------------------------------------- 1. FORM FIELDS
    await page.evaluate(() => view('agents'));
    await page.click('button[onclick="openNew()"]');
    await page.waitForTimeout(500);
    res.modalOpen = (await disp(page, '#newmodal')) !== 'none';

    // expected kind per field id: 'input'|'select'|'textarea'|'checkbox'|'div'
    const want = {
      n_name: 'input', n_template: 'select', n_clonefrom: 'select', n_clonework: 'checkbox',
      n_brain: 'select', n_provider: 'select', n_apibase: 'input', n_keyenv: 'input',
      n_model: 'select', n_escmodel: 'select', n_interval: 'input', n_mission: 'textarea',
      n_secsearch: 'input', n_msg: 'div',
    };
    res.fields = await page.evaluate((want) => {
      const out = {};
      for (const id of Object.keys(want)) {
        const el = document.getElementById(id);
        if (!el) { out[id] = { present: false }; continue; }
        const tag = el.tagName.toLowerCase();
        let kind = tag;
        if (tag === 'input') kind = (el.type === 'checkbox') ? 'checkbox' : 'input';
        out[id] = { present: true, kind, type: el.type || null };
      }
      return out;
    }, want);
    res.wantKinds = want;
    res.templateOpts = await optVals(page, 'n_template');
    res.brainOpts = await optVals(page, 'n_brain');

    // ---------------------------------------------------------------- 2. CONDITIONAL VISIBILITY
    // brain=claude (default) -> provider + escalation rows hidden
    await page.selectOption('#n_brain', 'claude');
    await page.waitForTimeout(300);
    res.claudeProvHidden = !(await visible(page, '#n_provrow'));
    res.claudeEscHidden = !(await visible(page, '#n_escrow'));
    res.modelOptsClaude = (await optVals(page, 'n_model') || []).length;

    // brain=api -> both rows visible
    await page.selectOption('#n_brain', 'api');
    await page.waitForTimeout(400);
    res.apiProvVisible = await visible(page, '#n_provrow');
    res.apiEscVisible = await visible(page, '#n_escrow');
    res.providerOpts = (await optVals(page, 'n_provider') || []);

    // provider default (nvidia) -> custom block hidden; capture model list
    const provVals = res.providerOpts.slice();
    const firstReal = provVals.find(v => v && v !== 'custom') || provVals[0];
    await page.selectOption('#n_provider', firstReal);
    await page.waitForTimeout(400);
    res.firstProvider = firstReal;
    res.nvidiaProvcustomHidden = !(await visible(page, '#n_provcustom'));
    res.modelOptsProvA = (await optVals(page, 'n_model') || []).join('|');

    // provider=custom -> custom base/key-env block visible
    await page.selectOption('#n_provider', 'custom');
    await page.waitForTimeout(400);
    res.custProvcustomVisible = await visible(page, '#n_provcustom');
    res.apibaseVisible = await visible(page, '#n_apibase');
    res.keyenvVisible = await visible(page, '#n_keyenv');

    // switch to a different real provider -> model list should repopulate/change
    const otherReal = provVals.find(v => v && v !== 'custom' && v !== firstReal);
    if (otherReal) {
      await page.selectOption('#n_provider', otherReal);
      await page.waitForTimeout(400);
      res.otherProvider = otherReal;
      res.modelOptsProvB = (await optVals(page, 'n_model') || []).join('|');
      res.modelListChangedOnProvider = res.modelOptsProvB !== res.modelOptsProvA;
    } else {
      res.modelListChangedOnProvider = null; // only one real provider configured
    }
    // model list also changes when brain flips api<->claude
    res.modelOptsApi = (await optVals(page, 'n_model') || []).length;
    res.modelListChangedOnBrain = res.modelOptsApi !== res.modelOptsClaude
      || ((await optVals(page, 'n_model') || []).join('|') !== '');

    // ---------------------------------------------------------------- 3. VALIDATION + intercept
    await page.selectOption('#n_brain', 'claude');
    await page.waitForTimeout(200);
    const msgText = () => page.evaluate(() => (document.getElementById('n_msg').textContent || '').trim());
    const clickCreate = () => page.click('button[onclick="submitNew()"]');

    // invalid: name with a space
    createPosts = [];
    await page.fill('#n_name', 'Bad Name');
    await clickCreate();
    await page.waitForTimeout(400);
    res.invalidSpaceMsg = await msgText();
    res.invalidSpacePosts = createPosts.length;

    // invalid: empty name
    createPosts = [];
    await page.fill('#n_name', '');
    await clickCreate();
    await page.waitForTimeout(400);
    res.emptyMsg = await msgText();
    res.emptyPosts = createPosts.length;

    // valid: kebab-case name -> POST fires
    createPosts = [];
    await page.fill('#n_name', 'audit-valid-agent');
    await clickCreate();
    await page.waitForTimeout(800);
    res.validPosts = createPosts.length;
    res.validMsg = await msgText();

    // ---------------------------------------------------------------- 4a. MODAL close audit
    const close = {};
    // (a) Cancel button
    await goHome();
    await page.evaluate(() => openNew());
    await page.waitForTimeout(300);
    await page.click('button[onclick="closeNew()"]');
    await page.waitForTimeout(200);
    close.modal_cancel = (await disp(page, '#newmodal')) === 'none';
    // (b) Escape key
    await page.evaluate(() => openNew());
    await page.waitForTimeout(300);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    close.modal_escape = (await disp(page, '#newmodal')) === 'none';
    await page.evaluate(() => closeNew());
    // (c) click the dark overlay (outside the inner card) — top-left corner is overlay, not card
    await page.evaluate(() => openNew());
    await page.waitForTimeout(300);
    await page.mouse.click(5, 5);
    await page.waitForTimeout(200);
    close.modal_overlay = (await disp(page, '#newmodal')) === 'none';
    await page.evaluate(() => closeNew());
    // (d) is there any X/✕ close affordance inside the modal?
    await page.evaluate(() => openNew());
    await page.waitForTimeout(200);
    close.modal_hasX = await page.evaluate(() => {
      const m = document.getElementById('newmodal');
      if (!m) return false;
      return Array.from(m.querySelectorAll('button,span,a,.x,.ax,.nx'))
        .some(e => /^[\s]*[×✕✖xX⨯╳]\s*$/.test(e.textContent || '')
                   && /close|dismiss|cancel/i.test((e.getAttribute('onclick') || '') + (e.title || '')));
    });
    await page.evaluate(() => closeNew());

    // ---------------------------------------------------------------- 4b. BELL notif panel
    // (a) click outside
    await goHome();
    await page.click('#bellbtn');
    await page.waitForTimeout(250);
    const notifOpened = (await disp(page, '#notifpanel')) === 'block';
    await page.mouse.click(400, 500);   // somewhere outside #bellwrap
    await page.waitForTimeout(200);
    close.notif_clickOutside = notifOpened && (await disp(page, '#notifpanel')) === 'none';
    // (b) Escape
    await page.click('#bellbtn');
    await page.waitForTimeout(250);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(200);
    close.notif_escape = (await disp(page, '#notifpanel')) === 'none';
    res.notifOpened = notifOpened;

    // ---------------------------------------------------------------- 4c. INFO `i` tooltip
    // (a) click elsewhere dismisses (one-time document click listener)
    await goHome();
    await page.evaluate(() => openNew());
    await page.waitForTimeout(300);
    await page.click('#newmodal .info');           // open an info popup
    await page.waitForTimeout(150);
    const infoOpened = await exists(page, '#infopop');
    // dismiss by clicking a neutral spot INSIDE the modal card (a label) — fires the one-time doc
    // listener that removes the tooltip WITHOUT hitting the overlay (which would now close the modal).
    await page.click('#newmodal label');
    await page.waitForTimeout(200);
    close.info_clickElsewhere = infoOpened && !(await exists(page, '#infopop'));
    // (b) Escape
    await page.click('#newmodal .info');
    await page.waitForTimeout(150);
    await page.keyboard.press('Escape');
    await page.waitForTimeout(150);
    close.info_escape = !(await exists(page, '#infopop'));   // true only if Escape removed it
    res.infoOpened = infoOpened;
    await page.evaluate(() => { const e = document.getElementById('infopop'); if (e) e.remove(); closeNew(); });

    // ---------------------------------------------------------------- 4d. tick inspector
    await goHome();
    await page.evaluate(() => view('agents'));
    const aid = await page.evaluate(() => Object.keys(agents)[0]);
    await page.evaluate((id) => pick(id), aid);
    await page.waitForTimeout(300);
    await page.click('.tab[data-t="diag"]').catch(() => {});
    // wait for the inspector table to populate
    await page.waitForFunction(
      () => { const tb = document.getElementById('dgInspect'); return tb && tb.querySelector('tr'); },
      { timeout: 8000 }).catch(() => {});
    res.inspectorRows = await page.evaluate(() => {
      const tb = document.getElementById('dgInspect');
      return tb ? tb.querySelectorAll('tr[onclick]').length : 0;
    });
    if (res.inspectorRows > 0) {
      const expDisp = () => page.evaluate(() => {
        const e = document.getElementById('dgexp0');
        return e ? getComputedStyle(e).display : '__missing__';
      });
      // open by clicking the first data row
      await page.click('#dgInspect tr[onclick]');
      await page.waitForTimeout(200);
      close.inspect_opensOnRowClick = (await expDisp()) === 'table-row';
      // close by re-clicking the row (toggle) — test from the opened state
      await page.click('#dgInspect tr[onclick]');
      await page.waitForTimeout(200);
      close.inspect_closesOnReclick = (await expDisp()) === 'none';
      // Escape collapses it: re-open, then press Escape
      await page.click('#dgInspect tr[onclick]');
      await page.waitForTimeout(200);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(150);
      close.inspect_escapeCloses = (await expDisp()) === 'none';
      // any dedicated X close button?
      close.inspect_hasX = await page.evaluate(() => {
        const e = document.getElementById('dgexp0');
        return e ? Array.from(e.querySelectorAll('button,.x,.ax,.nx,[onclick]'))
          .some(x => /close|dismiss/i.test((x.getAttribute('onclick') || '') + (x.title || ''))) : false;
      });
    }

    res.close = close;
  } catch (e) {
    hard.push('driver-exception: ' + (e && e.message ? e.message : String(e)));
  } finally {
    if (browser) { try { await browser.close(); } catch (e) {} }
  }
  process.stdout.write(JSON.stringify({ res, hard, soft }));
})();
"""


def _run_node(base, node, cwd):
    driver = pathlib.Path(os.environ.get("TMPDIR", "/tmp")) / "enclave_forms_e2e_driver.js"
    driver.write_text(_NODE_DRIVER)
    env = dict(os.environ)
    nm = str(pathlib.Path(cwd) / "node_modules")
    env["NODE_PATH"] = nm + (os.pathsep + env["NODE_PATH"] if env.get("NODE_PATH") else "")
    cflibs = os.path.expanduser("~/.local/cflibs/usr/lib/aarch64-linux-gnu")
    if os.path.isdir(cflibs):
        env["LD_LIBRARY_PATH"] = cflibs + ":" + env.get("LD_LIBRARY_PATH", "")
    r = subprocess.run([node, str(driver), base], cwd=cwd, env=env,
                       capture_output=True, text=True, timeout=180)
    out = (r.stdout or "").strip()
    try:
        data = json.loads(out[out.index("{"):])
    except Exception as e:
        raise RuntimeError(f"node driver gave no JSON (rc={r.returncode}): "
                           f"{(r.stderr or out)[:800]}") from e
    return data["res"], data["hard"], data["soft"]


# --------------------------------------------------------------------------- assertions
def _assert(check, res, hard, soft):
    # ---- 1. form fields ----
    check("create modal opens", res.get("modalOpen") is True)
    fields = res.get("fields") or {}
    want = res.get("wantKinds") or {}
    for fid, kind in want.items():
        f = fields.get(fid) or {}
        check(f"field present: #{fid}", f.get("present") is True, f"{f!r}")
        check(f"field kind ok: #{fid} == {kind}", f.get("kind") == kind,
              f"got {f.get('kind')!r} want {kind!r}")
    tmpl = res.get("templateOpts") or []
    check("template select has 6 options", len(tmpl) == 6, f"opts={tmpl}")
    check("template options are the expected set",
          [t.lower() for t in tmpl] == ["venture", "autonomous", "orchestrator", "ops", "analyst", "support"],
          f"opts={tmpl}")
    brain = [b.lower() for b in (res.get("brainOpts") or [])]
    check("brain select == claude/api/local/optimize", brain == ["claude", "api", "local", "optimize"],
          f"opts={brain}")

    # ---- 2. conditional visibility ("permissions") ----
    check("brain=claude hides provider row", res.get("claudeProvHidden") is True)
    check("brain=claude hides escalation row", res.get("claudeEscHidden") is True)
    check("brain=api shows provider row", res.get("apiProvVisible") is True)
    check("brain=api shows escalation row", res.get("apiEscVisible") is True)
    check("provider select populated (>=2 incl custom)", len(res.get("providerOpts") or []) >= 2,
          f"opts={res.get('providerOpts')}")
    check("provider!=custom hides custom base/key-env block", res.get("nvidiaProvcustomHidden") is True)
    check("provider=custom reveals custom base/key-env block", res.get("custProvcustomVisible") is True)
    check("  -> #n_apibase visible under custom", res.get("apibaseVisible") is True)
    check("  -> #n_keyenv visible under custom", res.get("keyenvVisible") is True)
    check("model dropdown populates for brain=claude", (res.get("modelOptsClaude") or 0) >= 1,
          f"n={res.get('modelOptsClaude')}")
    check("model dropdown populates for brain=api", (res.get("modelOptsApi") or 0) >= 1,
          f"n={res.get('modelOptsApi')}")
    if res.get("modelListChangedOnProvider") is None:
        print("  (info: only one real provider configured -> provider->model-change check skipped)")
    else:
        check("switching provider repopulates the model list",
              res.get("modelListChangedOnProvider") is True,
              f"A={str(res.get('modelOptsProvA'))[:50]!r} B={str(res.get('modelOptsProvB'))[:50]!r}")

    # ---- 3. validation + network intercept ----
    check("invalid name (space) shows inline error", bool(res.get("invalidSpaceMsg")),
          f"msg={res.get('invalidSpaceMsg')!r}")
    check("invalid name (space) fires NO POST /api/create", res.get("invalidSpacePosts") == 0,
          f"posts={res.get('invalidSpacePosts')}")
    check("empty name shows inline error", bool(res.get("emptyMsg")), f"msg={res.get('emptyMsg')!r}")
    check("empty name fires NO POST /api/create", res.get("emptyPosts") == 0,
          f"posts={res.get('emptyPosts')}")
    check("valid name fires exactly one POST /api/create", res.get("validPosts") == 1,
          f"posts={res.get('validPosts')}")

    # ---- 4. popup close audit (CURRENT behaviour encoded; UX gaps flagged) ----
    c = res.get("close") or {}
    # create modal: Cancel, Escape, overlay-click, AND an X button all close it (UX gaps fixed 2026-06-27).
    check("modal: Cancel button closes it", c.get("modal_cancel") is True)
    check("modal: Escape closes it", c.get("modal_escape") is True, f"got {c.get('modal_escape')!r}")
    check("modal: overlay/outside click closes it", c.get("modal_overlay") is True,
          f"got {c.get('modal_overlay')!r}")
    check("modal: has an X/close affordance", c.get("modal_hasX") is True, f"got {c.get('modal_hasX')!r}")
    # bell notif: click-outside AND Escape close it.
    check("notif panel opened", res.get("notifOpened") is True)
    check("notif: click-outside closes it", c.get("notif_clickOutside") is True)
    check("notif: Escape closes it", c.get("notif_escape") is True, f"got {c.get('notif_escape')!r}")
    # info tooltip: next click dismisses, Escape now also dismisses.
    check("info tooltip opened", res.get("infoOpened") is True)
    check("info: click-elsewhere dismisses it", c.get("info_clickElsewhere") is True)
    check("info: Escape dismisses it", c.get("info_escape") is True, f"got {c.get('info_escape')!r}")
    # tick inspector: row toggles open/close, Escape now also collapses it (no dedicated X — minor).
    check("tick inspector has rows in fixture", (res.get("inspectorRows") or 0) >= 1,
          f"rows={res.get('inspectorRows')}")
    if (res.get("inspectorRows") or 0) >= 1:
        check("inspector: row click opens raw JSON", c.get("inspect_opensOnRowClick") is True)
        check("inspector: Escape collapses it", c.get("inspect_escapeCloses") is True,
              f"got {c.get('inspect_escapeCloses')!r}")
        check("inspector: re-clicking the row closes it (toggle)", c.get("inspect_closesOnReclick") is True)

    # error gate
    check("no uncaught page errors / console.error", not hard, "; ".join(hard)[:600])
    if soft:
        print(f"  (info: {len(soft)} benign network-noise messages ignored, e.g. {soft[0][:80]!r})")

    # ---- printed UX-gap summary (for the operator) ----
    gaps = []
    if c.get("modal_escape") is False:
        gaps.append("create modal: Escape does nothing")
    if c.get("modal_overlay") is False:
        gaps.append("create modal: clicking the dark overlay/outside does NOT close")
    if c.get("modal_hasX") is False:
        gaps.append("create modal: no X/✕ close button (only Cancel)")
    if c.get("notif_escape") is False:
        gaps.append("bell notif panel: Escape does nothing (only click-outside)")
    if c.get("info_escape") is False:
        gaps.append("info `i` tooltip: Escape does nothing (only next-click)")
    if c.get("inspect_escapeCloses") is False:
        gaps.append("tick inspector: Escape does nothing (only re-click toggle)")
    if c.get("inspect_hasX") is False:
        gaps.append("tick inspector: no X/close button (re-click row to collapse)")
    print("\n  UX GAPS FOUND (current behaviour, candidates to fix):")
    for g in gaps:
        print(f"    - {g}")


def main():
    npw = _node_playwright()
    if not npw:
        extra = " (host browser bridge :18184 is up but cannot drive the interactive flow)" \
            if _bridge_up() else ""
        _skip("no node-playwright available (local node_modules)" + extra)
    node, cwd = npw
    print(f"E2E forms/popups audit driver: node-playwright @ {cwd}")

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
        res, hard, soft = _run_node(base, node, cwd)
        _assert(check, res, hard, soft)
    finally:
        stop()
    raise SystemExit(check.report())


if __name__ == "__main__":
    main()
