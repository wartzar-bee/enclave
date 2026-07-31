#!/usr/bin/env python3
"""
enclave plugin manifest + vetting validator.

This is the safety gate for `enclave plugin add` (backlog deliverable #2). A plugin is an
installable add-on of an existing enclave extension type (bridge | tool | template | policy).
Because installing a plugin is an *install surface*, every plugin is vetted BEFORE it is wired
in — the same hard rule enclave already applies to baked dependencies (`docs/VETTING.md`).

What this enforces (studio rule #1 — plugins are an install surface):
  1. PINNED version           — `version` must be an exact semver (no ranges / `^` / `~` / `latest`).
  2. A declared contract      — name, type, entrypoint present and well-formed; entrypoint exists.
  3. Declared-vs-actual match — the manifest must DECLARE its network egress, secret access and any
                                code-exec/install step. A static scan of EVERY source file the plugin
                                ships then checks the code against those declarations. Anything the
                                code DOES but the manifest did NOT declare is a FAIL — that is the
                                "read the code, flag secret/network access, no blind install-scripts"
                                rule, mechanised.

  The scan covers the whole plugin dir, not just the declared entrypoint: `enclave plugin add` copies
  the ENTIRE directory into the runtime, and the entrypoint imports its siblings at load time, so an
  exfil hidden in a helper file (`utils.py`, `vendor.js`) is exactly as live as one in the entrypoint.
  A vetting gate that read only the entrypoint would be trivially bypassed by moving the payload one
  import away — so it reads them all.

This is a HEURISTIC static gate, not a proof of safety. It catches the common exfil/undeclared-access
shapes and forces an honest manifest; a human still reads the code before trusting a plugin. It is
deliberately fail-closed: an undeclared capability blocks the install rather than warning.

Usage:
  python3 tools/plugin/validate.py <plugin-dir>        # validate one plugin, exit 0 clean / 2 on findings
  python3 tools/plugin/validate.py <plugin-dir> --json # machine-readable report on stdout

Stdlib only, except PyYAML for `plugin.yaml` (a `plugin.json` manifest needs no deps) — the same
convention `bin/enclave` uses for specs.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLUGIN_TYPES = {"bridge", "tool", "template", "policy"}
PINNED_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")           # exact pin, no range operators
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")

# Source files the plugin ships that could execute at load/run time. The whole plugin dir is copied
# into the runtime on `plugin add`, so every one of these is live — the scan must read them ALL, not
# just the declared entrypoint (moving a payload into a helper file must not slip past the gate).
SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".bash"}
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv"}
MAX_SCAN_BYTES = 2_000_000                                # over this = unscannable = ERROR (fail-closed)

# --- static-scan signatures (heuristic; the manifest must DECLARE anything these find) ----------
# Secret access: reads of the well-known secret stores / credential env names.
SECRET_PATTERNS = [
    re.compile(r"\.secrets\b"),
    re.compile(r"\.ssh\b"),
    re.compile(r"\.aws\b"),
    re.compile(r"\.npmrc\b"),
    re.compile(r"(?i)\b(getenv|environ)\b.{0,40}(token|secret|key|password|passwd|credential)"),
    re.compile(r"(?i)(token|secret|api[_-]?key|password|credential)\s*=\s*os\.(environ|getenv)"),
]
# Network egress: any outbound-capable call. We then try to pull literal hosts out of the source.
NETWORK_PATTERNS = [
    re.compile(r"\b(urllib\.request|urlopen|requests\.(get|post|put|patch|delete|request)|httpx|http\.client|socket\.(socket|create_connection)|aiohttp)\b"),
]
HOST_LITERAL = re.compile(r"https?://([a-zA-Z0-9.\-]+)")
# Egress via a shell command (curl/wget/nc/…). The Python-level NETWORK_PATTERNS miss egress that a
# plugin performs by shelling out — `subprocess.run(["curl", ..., "https://attacker"])` or a bare
# `curl` in a .sh entrypoint. Any of these is treated as egress and its hosts checked against
# `security.network` exactly like an in-process connection (closes B1: exec-file egress bypass).
NET_COMMAND = re.compile(r"(?<![\w.-])(curl|wget|nc|ncat|netcat|scp|sftp|telnet|ftp)(?![\w.-])")
# Code execution / install-time run: the "no blind install-scripts / no auto-exec" rule.
EXEC_PATTERNS = [
    re.compile(r"\b(subprocess\.(run|Popen|call|check_output)|os\.system|os\.popen|pty\.spawn)\b"),
    re.compile(r"\beval\s*\(|\bexec\s*\("),
]
# Obfuscation: the classic decode-then-run shapes.
OBFUSCATION_PATTERNS = [
    re.compile(r"\b(b64decode|base64\.(b64decode|decodebytes)|codecs\.decode)\b"),
    re.compile(r"fromCharCode|atob\("),
]

COMMENT_OR_STRING_HINT = re.compile(r"^\s*#")   # cheap: skip whole-line comments in the scan


class Finding:
    def __init__(self, level: str, code: str, message: str):
        self.level = level      # "error" | "warn"
        self.code = code
        self.message = message

    def as_dict(self):
        return {"level": self.level, "code": self.code, "message": self.message}


def _load_manifest(plugin_dir: Path):
    """Return (manifest_dict, manifest_path). Prefer plugin.yaml, fall back to plugin.json."""
    y = plugin_dir / "plugin.yaml"
    yml = plugin_dir / "plugin.yml"
    j = plugin_dir / "plugin.json"
    if y.exists() or yml.exists():
        path = y if y.exists() else yml
        try:
            import yaml
        except ImportError:
            raise SystemExit(
                "plugin manifest is YAML but PyYAML isn't installed "
                "(`pip install pyyaml`), or ship a plugin.json manifest instead"
            )
        return yaml.safe_load(path.read_text()) or {}, path
    if j.exists():
        return json.loads(j.read_text()), j
    raise SystemExit(f"no plugin.yaml / plugin.json in {plugin_dir}")


def _has_shebang(path: Path) -> bool:
    """True if the file starts with `#!` — an executable script regardless of its suffix."""
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _source_files(plugin_dir: Path, ep_path: Path | None = None):
    """Every file the plugin ships that must be vetted (recursively), sorted for stable output.

    Returns (targets, sym_escapes):
      targets      — list of (relative_path_str, Path) to scan. A file is a target if it has a known
                     source suffix, OR is the declared entrypoint (ANY suffix — closes B2: a
                     suffixless `start` entrypoint must still be read), OR begins with a `#!` shebang
                     (an executable script hiding behind a non-source suffix).
      sym_escapes  — list of relative paths that are symlinks whose target resolves OUTSIDE the plugin
                     dir. `plugin add` does copytree(symlinks=False), so it materialises the real
                     (unscanned) content on install — that is a fail-closed ERROR, not a silent skip
                     (closes B4).

    Skips vendored/cache dirs and manifests. The whole dir is copied on install, so a helper file is
    as live as the entrypoint.
    """
    root = plugin_dir.resolve()
    ep_resolved = ep_path.resolve() if ep_path is not None else None
    targets, sym_escapes, seen = [], [], set()
    for p in sorted(plugin_dir.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.is_symlink():
            try:
                p.resolve(strict=False).relative_to(root)
            except (ValueError, OSError, RuntimeError):
                sym_escapes.append(str(p.relative_to(plugin_dir)))
                continue                                   # escaping symlink — flagged, not scanned
        if not p.is_file():
            continue
        try:
            rel = str(p.resolve().relative_to(root))
        except (ValueError, OSError):
            continue
        if rel in seen:
            continue
        is_entry = ep_resolved is not None and p.resolve() == ep_resolved
        if p.suffix.lower() in SOURCE_SUFFIXES or is_entry or _has_shebang(p):
            seen.add(rel)
            targets.append((rel, p))
    return targets, sym_escapes


def _scan_source(text: str, patterns) -> bool:
    for line in text.splitlines():
        if COMMENT_OR_STRING_HINT.match(line):
            continue
        for pat in patterns:
            if pat.search(line):
                return True
    return False


def _hosts_in_source(text: str):
    hosts = set()
    for line in text.splitlines():
        if COMMENT_OR_STRING_HINT.match(line):
            continue
        for m in HOST_LITERAL.finditer(line):
            hosts.add(m.group(1))
    return hosts


def validate(plugin_dir: Path):
    findings: list[Finding] = []
    manifest, mpath = _load_manifest(plugin_dir)

    def err(code, msg):
        findings.append(Finding("error", code, msg))

    def warn(code, msg):
        findings.append(Finding("warn", code, msg))

    # --- 1. contract / schema -------------------------------------------------------------------
    name = manifest.get("name")
    if not isinstance(name, str) or not SLUG.match(name or ""):
        err("name", "`name` must be a lowercase slug (a-z, 0-9, -), 2-64 chars")

    version = manifest.get("version")
    if not isinstance(version, str) or not PINNED_SEMVER.match(version or ""):
        err("version-pin",
            f"`version` must be an EXACT pinned semver like 1.2.0 (got {version!r}); "
            "ranges / ^ / ~ / 'latest' are refused — an install surface must be reproducible")

    ptype = manifest.get("type")
    if ptype not in PLUGIN_TYPES:
        err("type", f"`type` must be one of {sorted(PLUGIN_TYPES)} (got {ptype!r})")

    entrypoint = manifest.get("entrypoint")
    ep_path = None
    if not isinstance(entrypoint, str) or not entrypoint:
        err("entrypoint", "`entrypoint` (relative path to the plugin's main file) is required")
    else:
        ep_path = (plugin_dir / entrypoint).resolve()
        if not ep_path.is_relative_to(plugin_dir.resolve()):
            err("entrypoint-escape", "`entrypoint` must stay inside the plugin dir (no ../ escape)")
            ep_path = None
        elif not ep_path.exists():
            err("entrypoint-missing", f"entrypoint file not found: {entrypoint}")
            ep_path = None

    # --- 2. security declarations ---------------------------------------------------------------
    security = manifest.get("security") or {}
    if not isinstance(security, dict):
        err("security", "`security` must be a mapping (network / secrets / exec / install_script)")
        security = {}
    declared_network = security.get("network") or []
    if not isinstance(declared_network, list):
        err("security.network", "`security.network` must be a list of allowed hostnames ([] = none)")
        declared_network = []
    declared_secrets = bool(security.get("secrets", False))
    declared_exec = bool(security.get("exec", False))
    install_script = security.get("install_script")

    # --- 3. static vetting scan: declared-vs-actual (EVERY source file, not just the entrypoint) --
    # A plugin's whole dir is copied on install and the entrypoint imports its siblings, so a payload
    # hidden in a helper file is as live as one in the entrypoint. Aggregate findings across all of
    # them, attributing each to the file that triggered it so a reviewer knows where to look.
    secret_files, exec_files, obf_files = [], [], []
    net_files = []                          # files that open a connection (in-process OR via a shell cmd)
    undeclared_hosts = set()                # hosts, across all files, not in security.network
    net_without_host = False                # egress present but no literal host declared/found
    declared_host_set = set(declared_network)

    sources, sym_escapes = _source_files(plugin_dir, ep_path)
    for rel in sym_escapes:
        # B4: copytree(symlinks=False) materialises the real target on install — unscannable = ERROR.
        err("symlink-escape",
            f"{rel} is a symlink whose target resolves OUTSIDE the plugin dir — install would copy "
            "its real (unvetted) content. Refused; vendor the file inside the plugin instead")
    for rel, path in sources:
        # B3: anything we cannot fully scan (too large / unreadable) is a fail-closed ERROR, never a
        # warn — an install surface must not go in on code a human hasn't been forced to read.
        try:
            size = path.stat().st_size
        except OSError as e:
            err("scan-unreadable", f"cannot stat {rel} ({e}) — refusing to install unvettable code")
            continue
        if size > MAX_SCAN_BYTES:
            err("scan-too-large",
                f"{rel} is larger than {MAX_SCAN_BYTES} bytes — refusing to install what can't be "
                "scanned (fail-closed); move any vendored blob outside the plugin")
            continue
        try:
            src = path.read_text(errors="replace")
        except OSError as e:
            err("scan-unreadable", f"could not read {rel}: {e} — refusing to install unvettable code")
            continue

        if _scan_source(src, SECRET_PATTERNS):
            secret_files.append(rel)

        opens_net = _scan_source(src, NETWORK_PATTERNS)
        # B1: egress via a shell command (curl/wget/nc). Counts even without a Python net call — a
        # subprocess-to-curl or a bare `curl` in a .sh entrypoint is real egress the old net-only
        # branch never checked, so `network:[]` + curl exfil used to pass clean.
        net_cmd = _scan_source(src, [NET_COMMAND])
        if _scan_source(src, EXEC_PATTERNS):
            exec_files.append(rel)

        if opens_net or net_cmd:
            net_files.append(rel)
            hosts = _hosts_in_source(src)
            undeclared_hosts.update(h for h in hosts if h not in declared_host_set)
            if not hosts and not declared_network:
                net_without_host = True

        if _scan_source(src, OBFUSCATION_PATTERNS):
            obf_files.append(rel)

    if secret_files and not declared_secrets:
        err("undeclared-secrets",
            f"reads secrets/credentials in {secret_files} but `security.secrets` is not true — "
            "declare it (and justify it) or remove the access")

    if net_files:
        if undeclared_hosts:
            err("undeclared-egress",
                f"reaches host(s) not in `security.network`: {sorted(undeclared_hosts)} "
                f"(declared: {declared_network or '[]'}); egress code in {net_files}")
        if net_without_host:
            err("undeclared-egress",
                f"opens network connections in {net_files} but `security.network` is empty and no "
                "literal host was found — declare the hosts it may reach")

    if exec_files and not declared_exec:
        err("undeclared-exec",
            f"runs subprocesses / eval / exec in {exec_files} but `security.exec` is not true — "
            "no blind code execution; declare it and a reviewer must read it")

    if obf_files:
        warn("obfuscation",
             f"decode-then-run shapes (base64/atob/codecs) in {obf_files} — a human must "
             "read this before trusting the plugin")

    if install_script:
        warn("install-script",
             f"plugin declares an install_script ({install_script}) — enclave never auto-runs it; "
             "a maintainer reads it and runs it explicitly")

    return manifest, findings


def main(argv):
    as_json = "--json" in argv
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        raise SystemExit("usage: validate.py <plugin-dir> [--json]")
    plugin_dir = Path(args[0])
    if not plugin_dir.is_dir():
        raise SystemExit(f"not a directory: {plugin_dir}")

    manifest, findings = validate(plugin_dir)
    errors = [f for f in findings if f.level == "error"]

    if as_json:
        print(json.dumps({
            "plugin": manifest.get("name"),
            "ok": not errors,
            "findings": [f.as_dict() for f in findings],
        }, indent=2))
    else:
        label = manifest.get("name") or plugin_dir.name
        if not findings:
            print(f"✅ {label}: manifest valid, vetting scan clean")
        else:
            print(f"{'❌' if errors else '⚠️ '} {label}:")
            for f in findings:
                mark = "✗" if f.level == "error" else "•"
                print(f"  {mark} [{f.code}] {f.message}")
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
