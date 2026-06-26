"""monitor/policy.py — load + read the monitor policy (data, not code).

Operators tune which playbooks fire, the autofix allowlist, the fleet-default mode, and thresholds in
policies/monitor.json (env override ENCLAVE_MONITOR_POLICY) — no code change. Ships conservative
(alert-only, empty allowlist). Fail-open: a missing/broken policy yields safe defaults, never a crash.
"""
import os
import json
import pathlib

DEFAULT_PATH = pathlib.Path(__file__).resolve().parents[2] / "policies" / "monitor.json"
MODES = ("off", "observe", "alert", "suggest", "autofix")


class Policy:
    def __init__(self, data=None):
        self.data = data or {}

    @classmethod
    def load(cls, path=None):
        p = pathlib.Path(os.environ.get("ENCLAVE_MONITOR_POLICY", str(path or DEFAULT_PATH)))
        try:
            return cls(json.loads(p.read_text()))
        except Exception:
            return cls({})

    def default_mode(self):
        m = self.data.get("default_mode", "alert")
        return m if m in MODES else "alert"

    def enabled(self, key):
        return bool(self.data.get("playbooks", {}).get(key, {}).get("enabled", True))

    def severity(self, key, default):
        return self.data.get("playbooks", {}).get(key, {}).get("severity", default)

    def autofix_allowed(self, key):
        return key in (self.data.get("autofix_allowlist") or [])

    def llm_enabled(self):
        """D2b: whether the off-Opus LLM may hypothesise on novel high-sev anomalies. Default ON
        (it self-disables when no NVIDIA-free key is resolvable); set "llm_enabled": false to forbid."""
        return bool(self.data.get("llm_enabled", True))

    def push_enabled(self):
        """D2b: whether NEW high-sev alerts also push to Telegram. Default ON (self-disables when no
        bot/chat is configured); set "push_enabled": false to forbid."""
        return bool(self.data.get("push_enabled", True))

    def threshold(self, name, default):
        try:
            return type(default)(self.data.get("thresholds", {}).get(name, default))
        except (TypeError, ValueError):
            return default
