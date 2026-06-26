"""monitor/state.py — the fleet monitor's system of record (dedup + remediation history).

escalations.log is the human-facing NOTIFICATION (append-only prose → the dashboard inbox); this is
the machine's STATE: per-(agent, problem) fingerprints, an open→alerted→suppressed→recovered state
machine, remediation history, and rate-limit accounting. Keeping them separate is what prevents
re-alert storms (you can't dedup off append-only prose).

Single-writer (only the daemon writes it), host-side JSON at ~/.config/enclave/monitor-state.json
(env override ENCLAVE_MONITOR_STATE), atomic-replace on write. Pure stdlib.

Dedup rule: ALERT when there's no prior record, OR the problem had recovered and re-occurred, OR the
fingerprint changed (an escalation, e.g. severity med→high). SUPPRESS an unchanged, already-alerted
problem. When a previously-alerted problem stops matching, mark it RECOVERED (the daemon emits one
recovery line so the inbox shows the resolution, not just the problem).
"""
import os
import json
import time
import hashlib
import pathlib
import tempfile
import calendar

STATE_PATH = pathlib.Path(os.environ.get(
    "ENCLAVE_MONITOR_STATE", pathlib.Path.home() / ".config" / "enclave" / "monitor-state.json"))
MAX_REMEDIATIONS = 50   # per agent; bounded like runner.log/usage.jsonl


def _iso(now=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _epoch(iso):
    """Parse an ISO-8601 Z timestamp back to epoch (UTC-correct, unlike time.mktime)."""
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return 0.0


def fingerprint(severity, key, cause):
    """A coarse identity for a problem so noise doesn't re-alert. Deliberately EXCLUDES the volatile
    evidence string (e.g. "4 failures" vs "5 failures") — only severity/key/cause define identity, so
    a worsening (severity or cause change) re-alerts but a flapping count does not."""
    basis = f"{severity}|{key}|{cause or ''}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


class MonitorState:
    def __init__(self, data=None, path=STATE_PATH):
        self.path = pathlib.Path(path)
        self.data = data or {"version": 1, "agents": {}, "ratelimit": {}}
        self.data.setdefault("agents", {})
        self.data.setdefault("ratelimit", {})

    @classmethod
    def load(cls, path=STATE_PATH):
        try:
            return cls(json.loads(pathlib.Path(path).read_text()), path)
        except Exception:
            return cls(path=path)

    def _agent(self, aid):
        return self.data["agents"].setdefault(aid, {"alerts": {}, "remediations": []})

    # --- dedup state machine -----------------------------------------------------------------
    def observe(self, aid, finding, now=None):
        """Record a finding; return 'alert' (new/escalated/re-occurred → notify) or 'suppress'
        (unchanged, already known). finding = {key, severity, cause}."""
        a = self._agent(aid)
        key = finding["key"]
        fp = fingerprint(finding["severity"], key, finding.get("cause"))
        rec = a["alerts"].get(key)
        nowi = _iso(now)
        if rec is None or rec.get("state") == "recovered" or rec.get("fingerprint") != fp:
            a["alerts"][key] = {
                "fingerprint": fp, "severity": finding["severity"],
                "first_seen": (rec or {}).get("first_seen", nowi), "last_alerted": nowi,
                "state": "alerted", "occurrences": (rec or {}).get("occurrences", 0) + 1,
            }
            return "alert"
        rec["occurrences"] = rec.get("occurrences", 0) + 1
        rec["state"] = "suppressed"
        return "suppress"

    def reconcile_recoveries(self, aid, active_keys, now=None):
        """Mark previously-alerted problems that no longer fire as recovered; return their keys
        (the daemon writes one recovery line each)."""
        a = self._agent(aid)
        recovered = []
        for key, rec in a["alerts"].items():
            if rec.get("state") in ("alerted", "suppressed") and key not in active_keys:
                rec["state"] = "recovered"
                rec["recovered_at"] = _iso(now)
                recovered.append(key)
        return recovered

    # --- remediation safety accounting -------------------------------------------------------
    def record_remediation(self, aid, key, action, mode, result, now=None, dryrun=False):
        a = self._agent(aid)
        a["remediations"].append({
            "ts": _iso(now), "epoch": float(now or time.time()), "playbook": key,
            "action": action, "mode": mode, "result": result, "dryrun": dryrun,
        })
        a["remediations"][:] = a["remediations"][-MAX_REMEDIATIONS:]

    def fixed_recently(self, aid, key, within_s=3600, now=None):
        """Anti-loop guard: did the monitor already (non-dryrun) remediate this exact problem within
        the window? If so, the daemon escalates instead of re-applying."""
        t = now or time.time()
        for r in reversed(self._agent(aid)["remediations"]):
            if r["playbook"] == key and not r.get("dryrun"):
                rt = r.get("epoch") or _epoch(r.get("ts", ""))
                if t - rt < within_s:
                    return True
        return False

    def rate_ok(self, aid, per_agent=3, window_s=3600, now=None):
        """Fleet-global rate limit on autofix enqueues. Returns True if under the cap (prunes the
        window in place)."""
        t = now or time.time()
        rl = self.data["ratelimit"]
        recent = [x for x in rl.get(aid, []) if t - x < window_s]
        rl[aid] = recent
        return len(recent) < per_agent

    def mark_remediated(self, aid, now=None):
        self.data["ratelimit"].setdefault(aid, []).append(float(now or time.time()))

    def flush(self):
        self.data["updated"] = _iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f, indent=1)
        os.replace(tmp, self.path)
