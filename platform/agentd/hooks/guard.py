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
Tune the publish denylist per venture via env GUARD_PUBLISH_DENY="pat1,pat2,…".
"""
import sys, os, re, json

# Live long-form publish / sales / bio-link go-live — human-gated (publish-gate +
# never-advertise). NOT content posting (bluesky_post / reels stay allowed — that's the job).
# Self-contained companies run their own FREE distribution autonomously (publish chapters, edit
# settings/tags, post content, upload readings) — NOT gated. Only MONEY (the operator's funds) and
# NEVER-ADVERTISE (bio-link/CTA) stay blocked; legal-identity (KDP) is its own escalation.
DEFAULT_PUBLISH_DENY = (
    "kdp_", " kdp ", "gumroad", "stripe", "checkout", "payout", "purchase",   # money = operator
    "edit_bio", "set_bio", "linkinbio", "link-in-bio", "update_bio",           # never-advertise
)
# Foreign credentials / key stores outside this agent's scoped secrets (the mount already
# limits which .secrets/ files exist; this blocks attempts to reach anything else).
SECRET_DENY = (".ssh/", "id_rsa", "id_ed25519", ".aws/credentials", "/.netrc",
               ".secrets/launcher-master", ".secrets/comms-bridge", ".secrets/github",
               ".secrets/anthropic", ".secrets/google.env",
               # git-push credential (BRAIN/allow_git agents): the agent can `git push` but NOT read the
               # raw token nor run the helper directly — git invokes the helper internally.
               ".secrets/git.env", "gitcreds-helper")
GIT_RE = re.compile(r"(?:^|[;&|]\s*|\s)git(?:\s|$)")

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


def _env_deny():
    extra = [p.strip().lower() for p in os.environ.get("GUARD_PUBLISH_DENY", "").split(",") if p.strip()]
    return tuple(DEFAULT_PUBLISH_DENY) + tuple(extra)


def decide(tool_name, tool_input, publish_deny=None):
    """PURE allow/deny decision (unit-tested). Returns (allow: bool, reason: str)."""
    cmd = (tool_input.get("command") or "") if tool_name == "Bash" else ""
    path = tool_input.get("file_path") or tool_input.get("path") or tool_input.get("notebook_path") or ""
    low_cmd = cmd.lower()
    blob = f"{cmd} {path}".lower()

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

    # 2) foreign credential / key-store access (defense-in-depth atop the scoped mount)
    for pat in SECRET_DENY:
        if pat in blob:
            return False, f"access to '{pat.strip()}' denied — outside this agent's scoped secrets"

    # 3) publish-gate / never-advertise — live long-form/sales/bio-link go-live is human-gated
    for pat in (publish_deny if publish_deny is not None else _env_deny()):
        if pat and pat in low_cmd:
            return False, (f"'{pat.strip()}' is publish-gated — long-form/sales/bio-link go-live needs "
                           f"human approval; draft + stage it, don't push it live")

    # 4) read-only cloud policy (opt-in per agent): gcloud/gsutil/bq/GraphQL writes blocked
    if tool_name == "Bash" and os.environ.get("GUARD_CLOUD_READONLY"):
        ok, reason = decide_cloud_readonly(cmd)
        if not ok:
            return False, reason

    return True, ""


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


if __name__ == "__main__":
    main()
