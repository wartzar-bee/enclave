#!/usr/bin/env python3
"""
test_guard_wiring.py — the guard's SSRF/egress logic is only as good as its MATCHER wiring.

guard.py has always been able to decide() a WebFetch url (SSRF→IMDS, egress allowlist, binary
upload), but every shipped settings.json registered it only for `Bash|Read|Edit|Write|NotebookEdit`
— so for the Claude runtime the WebFetch path was dead code, and `guard.py --selftest` (which calls
decide() directly) could not reveal the gap. An agent could WebFetch http://169.254.169.254/... and
walk off with cloud credentials, unblocked.

This test guards the wiring, not just the logic:
  1. every shipped guard PreToolUse matcher must cover WebFetch;
  2. driving guard.py's main() (JSON on stdin, as the harness does) with a WebFetch→IMDS payload
     must BLOCK (exit 2); an allowed host must pass (exit 0).

Standalone, no deps. exit 0 = green, non-zero = the wiring regressed.
"""
import json, pathlib, re, subprocess, sys

HOOKS = pathlib.Path(__file__).resolve().parent
ROOT = HOOKS.parent.parent.parent          # platform/agentd/hooks -> repo root
GUARD = HOOKS / "guard.py"
PY = sys.executable or "python3"

fails = []


def ck(name, cond, detail=""):
    if not cond:
        fails.append(f"{name}{(' — ' + detail) if detail else ''}")


def _guard_matchers(settings_path):
    """Return the matcher strings of every PreToolUse entry that runs guard.py."""
    data = json.loads(settings_path.read_text())
    out = []
    for entry in data.get("hooks", {}).get("PreToolUse", []):
        cmds = " ".join(h.get("command", "") for h in entry.get("hooks", []))
        if "guard.py" in cmds:
            out.append(entry.get("matcher", ""))
    return out


# 1) matcher coverage across every shipped settings.json (templates + any generated fixture)
settings_files = sorted(ROOT.glob("templates/*/.claude/settings.json"))
ck("templates-present", settings_files, "no templates/*/.claude/settings.json found")
for sf in settings_files:
    matchers = _guard_matchers(sf)
    ck(f"guard-entry:{sf.parent.parent.parent.name}", matchers, "no PreToolUse entry runs guard.py")
    ck(f"webfetch-covered:{sf.parent.parent.parent.name}",
       any(re.search(r"\bWebFetch\b", m) for m in matchers),
       f"guard matcher(s) {matchers} do not include WebFetch")

# bin/enclave's generated default must also cover WebFetch
enclave_cli = (ROOT / "bin" / "enclave").read_text()
ck("bin-enclave-default-webfetch",
   re.search(r'_pre\s*=.*WebFetch', enclave_cli) or "NotebookEdit|WebFetch" in enclave_cli,
   "bin/enclave generated guard matcher omits WebFetch")


# 2) behavioural: main() over stdin, the way Claude Code invokes the hook
def run_guard(payload):
    r = subprocess.run([PY, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True)
    return r.returncode


ck("webfetch-imds-blocked",
   run_guard({"tool_name": "WebFetch",
              "tool_input": {"url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"}}) == 2,
   "WebFetch to the cloud metadata IP was not blocked")
ck("webfetch-gcp-metadata-blocked",
   run_guard({"tool_name": "WebFetch",
              "tool_input": {"url": "http://metadata.google.internal/computeMetadata/v1/"}}) == 2,
   "WebFetch to metadata.google.internal was not blocked")
ck("webfetch-normal-allowed",
   run_guard({"tool_name": "WebFetch",
              "tool_input": {"url": "https://docs.anthropic.com/en/api"}}) == 0,
   "a normal WebFetch was blocked (should pass in report-only default)")

if fails:
    print("test_guard_wiring FAIL:")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print(f"test_guard_wiring OK ({len(settings_files)} settings files, WebFetch wired + IMDS blocked)")
