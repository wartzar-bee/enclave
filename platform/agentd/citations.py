#!/usr/bin/env python3
"""citations.py — framework citation-liveness primitive: "prove this URL resolves".

Why this is a FRAMEWORK primitive and not an agent skill: you cannot verify a citation resolves
by reasoning about it, and an LLM judge shares the generator's hallucination blind spot. Every
agent that cites sources needs the same deterministic check, so it lives here (migrated 2026-07-20
from an earlier implementation, where it was hand-rolled and initially WRONG in a way that
invalidated every verdict the gate ever issued).

The two hard-won rules — DO NOT REGRESS (see the design notes):
  * A HEAD 404 is NOT evidence of absence. Many real hosts refuse HEAD outright
    (business.linkedin.com answers HEAD 404 / GET 200). Only a GET is decisive.
  * Our own transient failure (timeout/reset/rate-limit) is never proof a URL is invented.
    Only a deterministic NXDOMAIN on every probe — reconfirmed once with a generous timeout —
    may kill a citation. 403/405/429 mean "exists and dislikes bots", not "absent".
Verdicts are cached (TTL) so the same input gets the same verdict on every run — a gate that
flips on network luck is a coin-flip, not a gate.

Library : from citations import url_alive, alive_from_status
CLI     : citations.py <url> [...]     exit 0 = all alive, 1 = any absent
          citations.py --selftest      offline status-code truth table
Cache   : $CITATIONS_CACHE, else $AGENT_DIR/state/url-liveness.json, else ./state/.url-liveness-cache.json
"""
import datetime
import json
import os
import socket
import sys
import urllib.error
import urllib.request

ABSENT_STATUS = (404, 410)  # the only codes that mean "there is nothing here"
LIVENESS_TTL_DAYS = 7


def alive_from_status(code):
    """Does an HTTP status prove the URL exists? Pure (selftest-able).

    Any HTTP response at all proves the host resolves and serves — only 404/410 say the resource
    is absent. 403/405/429 are anti-bot defence, not evidence of invention.
    """
    return code not in ABSENT_STATUS


def _cache_path():
    p = os.environ.get("CITATIONS_CACHE")
    if p:
        return p
    ad = os.environ.get("AGENT_DIR")
    if ad and os.path.isdir(ad):
        return os.path.join(ad, "state", "url-liveness.json")
    return os.path.join(os.getcwd(), "state", ".url-liveness-cache.json")


def _cache_load(path=None):
    try:
        with open(path or _cache_path()) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _cache_get(url, path=None):
    """Cached True/False, or None if absent/expired. Makes verdicts REPRODUCIBLE."""
    ent = _cache_load(path).get(url)
    if not ent:
        return None
    try:
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(ent["checked_at"])).days
    except Exception:
        return None
    return ent.get("alive") if age < LIVENESS_TTL_DAYS else None


def _cache_put(url, alive, path=None):
    path = path or _cache_path()
    data = _cache_load(path)
    data[url] = {"alive": bool(alive),
                 "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def url_alive(url, timeout=8, attempts=3, cache=None):
    """Does this URL point at something real? Distinguishes ABSENT from UNREACHABLE.

    An invented domain fails DNS deterministically -> absent -> False (still enforced).
    A real site timing out / resetting / rate-limiting is OUR network, not their existence -> True.
    """
    cached = _cache_get(url, cache)
    if cached is not None:
        return cached

    probes = 0
    dns_failures = 0
    for _ in range(attempts):
        for method in ("HEAD", "GET"):          # many real hosts refuse HEAD outright
            probes += 1
            req = urllib.request.Request(url, method=method,
                                         headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    v = alive_from_status(resp.status)         # any success = it exists
                    _cache_put(url, v, cache)
                    return v
            except urllib.error.HTTPError as exc:
                # A HEAD 404/405 is NOT decisive — fall through to the GET before judging.
                if method == "HEAD":
                    continue
                v = alive_from_status(exc.code)
                _cache_put(url, v, cache)
                return v
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, socket.gaierror) or "not known" in str(reason).lower():
                    dns_failures += 1                          # NXDOMAIN — domain doesn't exist
            except Exception:
                pass                                           # timeout/reset — OUR side

    if dns_failures == probes and probes:
        # A False can kill downstream work, so reconfirm once with a generous timeout.
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, method="GET",
                                           headers={"User-Agent": "Mozilla/5.0"}),
                    timeout=20) as resp:
                verdict = alive_from_status(resp.status)
        except urllib.error.HTTPError as exc:
            verdict = alive_from_status(exc.code)
        except Exception:
            verdict = False
        _cache_put(url, verdict, cache)
        return verdict
    _cache_put(url, True, cache)
    return True          # unreachable != absent; never kill a citation on our own connectivity


def selftest():
    failed = 0
    for code, expect in ((200, True), (403, True), (405, True), (429, True), (500, True),
                         (404, False), (410, False)):
        got = alive_from_status(code)
        good = got is expect
        print(f" {'PASS' if good else 'FAIL'}  alive_from_status({code}) -> {got}")
        failed += 0 if good else 1
    print("SELFTEST " + ("PASSED" if not failed else f"FAILED ({failed})"))
    return 1 if failed else 0


def main():
    args = [a for a in sys.argv[1:]]
    if "--selftest" in args:
        return selftest()
    urls = [a for a in args if not a.startswith("--")]
    if not urls:
        print(__doc__)
        return 2
    worst = 0
    for u in urls:
        v = url_alive(u)
        print(f"{'ALIVE ' if v else 'ABSENT'} {u}")
        if not v:
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
