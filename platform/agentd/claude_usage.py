#!/usr/bin/env python3
# ──────────────────────────────────────────────────────────────────────────
# claude_usage.py — the live SUBSCRIPTION usage that `claude /status` (and
# claude.ai → Settings → Usage) shows, made machine-readable for the fleet.
#
# SOURCE: a normal Messages API call made with the Claude Code OAuth subscription
# token returns the authoritative subscription limits in response HEADERS:
#     anthropic-ratelimit-unified-5h-utilization / -5h-reset / -5h-status
#     anthropic-ratelimit-unified-7d-utilization / -7d-reset / -7d-status
#     anthropic-ratelimit-unified-representative-claim   (which window binds now)
#     anthropic-ratelimit-unified-overage-status         (rejected ⇒ credits OFF)
# A tiny 1-token probe carries them for ~free. (The dedicated /api/oauth/usage
# endpoint needs a `user:profile` scope our setup-token lacks → 403; the header
# path works with the token we have.)
#
# We CACHE the parsed result and only re-probe past --max-age, so both the
# runtime.sh budget guard and the dashboard read ONE canonical file
# (platform/agentd/state/claude-usage.json) without each tick hitting the network.
#
#   claude_usage.py fetch [--max-age S]      → refresh cache if stale, print JSON
#   claude_usage.py show                     → print cached JSON (no network)
#   claude_usage.py guard --session-floor P --weekly-floor P \
#                         [--session-warn P] [--weekly-warn P]
#        → refresh, then exit 75 (DEFER) if either window's util ≥ its floor;
#          prints a human reason line; exit 0 otherwise (may print WARN/NOTE).
#
# Usage is % of the SUBSCRIPTION CEILING (mirrors /status) — NOT dollars (credits
# are off; dollars are meaningless). Fail-OPEN everywhere: any network/parse error
# falls back to the last cache, or "unknown", and the guard never blocks a tick.
# Pure stdlib (urllib) — no curl, no third-party deps.
# ──────────────────────────────────────────────────────────────────────────
import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO_ROOT = pathlib.Path(os.environ.get("TOOLS_ROOT", HERE.parent.parent))
DEFAULT_CACHE = HERE / "state" / "claude-usage.json"
API_URL = "https://api.anthropic.com/v1/messages"
# The OAuth (subscription) token is only authorized for Claude Code, so the probe must
# present the Claude Code identity as the first system block — this IS Claude Code infra.
CC_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."
PROBE_MODEL = "claude-haiku-4-5-20251001"  # cheapest; max_tokens=1


def _secrets_token():
    """Read CLAUDE_CODE_OAUTH_TOKEN from anthropic.env (never logged). Searches, in order: env var,
    $SECRETS_DIR, $ENCLAVE_SECRETS_LIB (the console's own secrets-library env), then REPO_ROOT/.secrets.
    The fallbacks matter when the agentd code lives in a subdir whose ./.secrets isn't the real one (e.g.
    the studio mounts the workspace .secrets, not businesses/enclave/.secrets)."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    for d in (os.environ.get("SECRETS_DIR"), os.environ.get("ENCLAVE_SECRETS_LIB"),
              str(REPO_ROOT / ".secrets")):
        if not d:
            continue
        try:
            for line in (pathlib.Path(d) / "anthropic.env").read_text().splitlines():
                if line.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                    return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return None


def _cc_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10).stdout
        for tok in out.split():
            if tok and tok[0].isdigit() and "." in tok:
                return tok
    except (OSError, subprocess.SubprocessError):
        pass
    return "0.0.0"


def _probe_headers(token, timeout=25):
    """Make the tiny Messages probe; return the response headers (dict-like). Raises on hard failure."""
    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "system": CC_IDENTITY,
        "messages": [{"role": "user", "content": "."}],
    }).encode()
    req = urllib.request.Request(API_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-beta", "oauth-2025-04-20")
    req.add_header("User-Agent", f"claude-code/{_cc_version()}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.headers
    except urllib.error.HTTPError as e:
        # Rate-limit headers are present even on many error responses — use them.
        return e.headers


def _window(headers, prefix):
    u = headers.get(f"anthropic-ratelimit-unified-{prefix}-utilization")
    r = headers.get(f"anthropic-ratelimit-unified-{prefix}-reset")
    s = headers.get(f"anthropic-ratelimit-unified-{prefix}-status")
    if u is None:
        return None
    try:
        pct = round(float(u) * 100, 1)
    except (TypeError, ValueError):
        return None
    out = {"pct": pct, "status": s}
    try:
        out["reset_epoch"] = int(r)
    except (TypeError, ValueError):
        out["reset_epoch"] = None
    return out


def parse_usage(headers):
    """Build the canonical usage dict from unified rate-limit headers, or None if absent."""
    if headers is None or headers.get("anthropic-ratelimit-unified-5h-utilization") is None:
        return None
    overage = headers.get("anthropic-ratelimit-unified-overage-status")
    return {
        "ts": int(time.time()),
        "five_hour": _window(headers, "5h"),
        "seven_day": _window(headers, "7d"),
        "representative": headers.get("anthropic-ratelimit-unified-representative-claim"),
        # overage = pay-as-you-go credits. 'rejected'/absent ⇒ credits OFF (operator invariant).
        "credits_enabled": overage == "allowed",
        "source": "ratelimit-headers",
    }


def _read_cache(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return None


def _write_cache(path, data):
    try:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)  # atomic
    except OSError as e:
        sys.stderr.write(f"claude_usage: cache write failed: {e}\n")


def fetch(cache_path, max_age):
    """Return canonical usage, refreshing the cache only if older than max_age. Fail-open to cache."""
    cached = _read_cache(cache_path)
    if cached and (time.time() - cached.get("ts", 0)) < max_age:
        return cached
    token = _secrets_token()
    if not token:
        return cached  # no token → keep whatever we had
    try:
        usage = parse_usage(_probe_headers(token))
    except (urllib.error.URLError, OSError, ValueError):
        usage = None
    if usage is None:
        return cached  # probe failed / no headers → keep last good reading
    _write_cache(cache_path, usage)
    return usage


def _reset_in(window):
    if not window or not window.get("reset_epoch"):
        return None
    return max(0, window["reset_epoch"] - int(time.time()))


def _fmt_eta(secs):
    if secs is None:
        return "?"
    h, m = divmod(secs // 60, 60)
    return f"{h}h{m:02d}m"


def main():
    ap = argparse.ArgumentParser(description="Live subscription usage (the /status numbers), machine-readable.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pf = sub.add_parser("fetch")
    pf.add_argument("--max-age", type=int, default=180, help="reuse cache younger than this many seconds")
    pf.add_argument("--out", default=str(DEFAULT_CACHE))
    pf.add_argument("--pretty", action="store_true")

    ps = sub.add_parser("show")
    ps.add_argument("--out", default=str(DEFAULT_CACHE))
    ps.add_argument("--pretty", action="store_true")

    pg = sub.add_parser("guard")
    pg.add_argument("--out", default=str(DEFAULT_CACHE))
    pg.add_argument("--max-age", type=int, default=180)
    pg.add_argument("--session-floor", type=float, default=0, help="defer when 5h util ≥ this %% (0=off)")
    pg.add_argument("--weekly-floor", type=float, default=0, help="defer when 7d util ≥ this %% (0=off)")
    pg.add_argument("--session-warn", type=float, default=70)
    pg.add_argument("--weekly-warn", type=float, default=85)
    pg.add_argument("--stale-after", type=int, default=3600,
                    help="a cached reading older than this many seconds counts as BLIND (exit 66)")
    args = ap.parse_args()

    if args.cmd == "show":
        data = _read_cache(args.out)
        if data is None:
            print(json.dumps({"error": "no cache", "source": "unknown"}))
            return
        print(json.dumps(data, indent=2 if args.pretty else None))
        return

    if args.cmd == "fetch":
        data = fetch(args.out, args.max_age)
        if data is None:
            print(json.dumps({"error": "unavailable", "source": "unknown"}))
            return
        print(json.dumps(data, indent=2 if args.pretty else None))
        return

    if args.cmd == "guard":
        data = fetch(args.out, args.max_age)
        # BLIND is loud, not silent (2026-07-04 enclave review fix #5). This guard is the fleet's only
        # real spend ceiling; returning rc 0 with no output when it can't read usage made blindness
        # indistinguishable from health, and runtime.sh's ccusage fallback (gated on rc 66) was
        # unreachable dead code because nothing ever exited 66. Now: no reading, or a reading staler
        # than --stale-after, exits 66 → the caller falls back to ccusage and alarms if that's blind
        # too. Still fail-OPEN (66 never defers a tick by itself).
        if not data or "five_hour" not in data:
            print("guard BLIND: no subscription usage reading (probe failed / no token / no cache)")
            sys.exit(66)
        age = int(time.time()) - int(data.get("ts", 0) or 0)
        if age > args.stale_after:
            print(f"guard BLIND: last usage reading is {age // 60}m old (> {args.stale_after // 60}m) — treating as no reading")
            sys.exit(66)
        sess = data.get("five_hour") or {}
        wk = data.get("seven_day") or {}
        sp, wp = sess.get("pct"), wk.get("pct")
        defer, lines = False, []
        if sp is not None and args.session_floor and sp >= args.session_floor:
            lines.append(f"session cap guard: 5h block at {sp}% (≥ {args.session_floor}% floor, resets {_fmt_eta(_reset_in(sess))}) — DEFER")
            defer = True
        elif sp is not None and sp >= args.session_warn:
            lines.append(f"session WARN: 5h block at {sp}% used (resets {_fmt_eta(_reset_in(sess))})")
        if wp is not None and args.weekly_floor and wp >= args.weekly_floor:
            lines.append(f"weekly ceiling guard: week at {wp}% (≥ {args.weekly_floor}% floor, resets {_fmt_eta(_reset_in(wk))}) — DEFER")
            defer = True
        elif wp is not None and wp >= args.weekly_warn:
            lines.append(f"weekly WARN: week at {wp}% used (resets {_fmt_eta(_reset_in(wk))})")
        if not lines:
            # Positive health line (fix #5): guard silence used to be ambiguous between "healthy,
            # under thresholds" and "blind". Every guarded tick now states its reading.
            lines.append(f"guard OK: 5h {sp if sp is not None else '?'}% / wk {wp if wp is not None else '?'}%")
        print(" ; ".join(lines))
        sys.exit(75 if defer else 0)


if __name__ == "__main__":
    main()
