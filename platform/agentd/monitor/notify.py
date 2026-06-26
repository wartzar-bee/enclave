"""monitor/notify.py — critical-alert push (D2b). Fail-open, dependency-free.

When a NEW high-severity problem alerts, the operator shouldn't have to be staring at the dashboard.
This pushes a one-line summary to Telegram. Self-contained (a tiny sendMessage over urllib — NOT an
import of telegram_relay, which sys.exits at import when unconfigured). No config ⇒ silent no-op, so the
monitor runs the same with or without a bot. Gating + dedup live in the caller (only NEW high-sev alert
transitions reach here); this module just resolves config + sends.
"""
import os
import json
import pathlib
import urllib.request


def _resolve():
    """(bot_token, chat_id) from env or a .secrets/notify.env under any stacks root (or one subdir
    level). Returns ('','') when unconfigured → callers treat that as 'pushes disabled'."""
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cid = os.environ.get("TELEGRAM_CHAT_ID", "")
    if tok and cid:
        return tok, cid
    cands = []
    ep = os.environ.get("ENCLAVE_NOTIFY_ENV")
    if ep:
        cands.append(pathlib.Path(ep))
    for r in [r for r in os.environ.get("ENCLAVE_STACKS_ROOTS", "").split(os.pathsep) if r]:
        rp = pathlib.Path(r)
        cands.append(rp / ".secrets" / "notify.env")
        try:
            for sub in sorted(rp.iterdir()):
                if sub.is_dir():
                    cands.append(sub / ".secrets" / "notify.env")
        except Exception:
            pass
    for f in cands:
        try:
            kv = {}
            for ln in f.read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    kv[k.strip()] = v.strip().strip('"').strip("'")
            tok = tok or kv.get("TELEGRAM_BOT_TOKEN", "")
            cid = cid or kv.get("TELEGRAM_CHAT_ID", "")
            if tok and cid:
                return tok, cid
        except Exception:
            pass
    return tok, cid


def available():
    tok, cid = _resolve()
    return bool(tok and cid)


def push(text, *, timeout=12):
    """Send one Telegram message. Returns True if sent. Fail-open: unconfigured or any error → False,
    never raises (a notification must never take the monitor down)."""
    tok, cid = _resolve()
    if not (tok and cid):
        return False
    try:
        body = json.dumps({"chat_id": int(cid) if str(cid).lstrip("-").isdigit() else cid,
                           "text": text[:3900], "disable_web_page_preview": True}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage",
                                     data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False
