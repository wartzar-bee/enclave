"""monitor/notify.py — critical-alert push (D2b). Fail-open, dependency-free, CHANNEL-AGNOSTIC.

When a NEW high-severity problem alerts, the operator shouldn't have to be staring at the dashboard.
This pushes a one-line summary to whichever channel is configured — preference order:
  1. SLACK_WEBHOOK_URL   — a Slack incoming webhook (POST {"text": …}); the operator's preferred path.
  2. TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID — a Telegram bot sendMessage.
Self-contained (tiny urllib POSTs — NOT an import of telegram_relay, which sys.exits when unconfigured).
No config ⇒ silent no-op, so the monitor runs the same with or without a channel. Gating + dedup live in
the caller (only NEW high-sev alert transitions reach here); this module just resolves config + sends.

Config lives in .secrets/notify.env (gitignored) under any stacks root — drop ONE line:
  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…     (or the two TELEGRAM_* lines)
"""
import os
import json
import pathlib
import urllib.request

_KEYS = ("SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")


def _resolve():
    """Config dict (SLACK_WEBHOOK_URL / TELEGRAM_*) from env, then a .secrets/notify.env under any stacks
    root (or one subdir level). Empty values when unconfigured → callers treat that as 'pushes disabled'."""
    cfg = {k: os.environ.get(k, "") for k in _KEYS}
    if cfg["SLACK_WEBHOOK_URL"] or (cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]):
        return cfg
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
            for ln in f.read_text().splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#") and "=" in ln:
                    k, v = ln.split("=", 1)
                    k = k.strip()
                    if k in _KEYS and not cfg.get(k):
                        cfg[k] = v.strip().strip('"').strip("'")
        except Exception:
            pass
        if cfg["SLACK_WEBHOOK_URL"] or (cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]):
            break
    return cfg


def channel():
    """Which channel is configured ('slack' | 'telegram' | None) — preference: Slack, then Telegram."""
    cfg = _resolve()
    if cfg["SLACK_WEBHOOK_URL"]:
        return "slack"
    if cfg["TELEGRAM_BOT_TOKEN"] and cfg["TELEGRAM_CHAT_ID"]:
        return "telegram"
    return None


def available():
    return channel() is not None


def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def push(text, *, timeout=12):
    """Send one alert to the configured channel. Returns True if sent. Fail-open: unconfigured or any
    error → False, never raises (a notification must never take the monitor down)."""
    cfg = _resolve()
    try:
        if cfg["SLACK_WEBHOOK_URL"]:
            _post(cfg["SLACK_WEBHOOK_URL"], {"text": text[:3900]}, timeout)
            return True   # Slack incoming webhooks return a plain "ok" body, not JSON
        tok, cid = cfg["TELEGRAM_BOT_TOKEN"], cfg["TELEGRAM_CHAT_ID"]
        if tok and cid:
            raw = _post(f"https://api.telegram.org/bot{tok}/sendMessage",
                        {"chat_id": int(cid) if str(cid).lstrip("-").isdigit() else cid,
                         "text": text[:3900], "disable_web_page_preview": True}, timeout)
            return json.loads(raw).get("ok", False)
    except Exception:
        return False
    return False
