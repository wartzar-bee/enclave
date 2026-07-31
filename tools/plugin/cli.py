#!/usr/bin/env python3
"""
`enclave plugin` — install, list and remove plugins (backlog deliverable #2).

A plugin is an installable add-on of an existing enclave extension type (bridge | tool | template |
policy). Installing one is an INSTALL SURFACE, so `add` runs the vetting gate (`validate.py`) FIRST
and refuses to install anything the gate rejects — pin version, no undeclared egress/secret/exec, no
blind install-scripts (studio rule #1). Nothing is ever auto-executed on install.

Commands:
  enclave plugin init <name> [--type tool]   scaffold a new plugin skeleton that passes the vetting gate
  enclave plugin add <local-dir> [--force]   validate, then copy into the plugins dir (refuse on findings)
  enclave plugin list                        list installed plugins (name · version · type)
  enclave plugin remove <name>               remove an installed plugin

Install location: $ENCLAVE_PLUGINS_DIR, else <repo>/plugins/. Stdlib only (+ PyYAML for yaml manifests,
via validate.py). Wired into bin/enclave as `cmd_plugin(args)`; also runnable standalone for tests.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# Import the vetting gate from the sibling module (works both as `python3 tools/plugin/cli.py`
# and when bin/enclave adds tools/plugin to sys.path).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as _validate  # noqa: E402


def _plugins_dir() -> Path:
    env = os.environ.get("ENCLAVE_PLUGINS_DIR")
    if env:
        return Path(env)
    # default: <repo-root>/plugins  (repo root = two levels up from tools/plugin/)
    return Path(__file__).resolve().parents[2] / "plugins"


def _read_manifest(plugin_dir: Path):
    try:
        manifest, _ = _validate._load_manifest(plugin_dir)
        return manifest or {}
    except SystemExit:
        return {}


# A clean entrypoint stub — declares nothing the vetting gate flags, so `init` always scaffolds a
# plugin that passes `validate` out of the box. `{name}`/`{type}` are filled per invocation.
_ENTRYPOINT_STUB = '''#!/usr/bin/env python3
"""{name} — an enclave {type} plugin.

Fill in the behaviour below. If you add network egress, secret access or a subprocess/eval/exec,
you MUST declare it in plugin.yaml under `security:` — the vetting gate (`enclave plugin add`) scans
every source file and refuses to install anything the manifest does not declare.
"""


def run():
    # TODO: implement your {type}. Keep the manifest's `security` block honest.
    print("{name}: hello from an enclave {type} plugin")


if __name__ == "__main__":
    run()
'''

_MANIFEST_STUB = '''# enclave plugin manifest — see docs/PLUGINS.md. Keep `security` HONEST: the vetting gate scans
# every source file and refuses to install anything you do not declare here.
name: {name}
version: 0.1.0            # exact pinned semver — ranges / ^ / ~ / latest are refused
type: {type}
entrypoint: main.py
security:
  network: []            # allowed egress hostnames; [] = no network
  secrets: false         # true only if the plugin reads a secret store / credential env
  exec: false            # true only if the plugin runs a subprocess / eval / exec
'''


def cmd_init(name: str, ptype: str, dest_root: Path) -> int:
    if not _validate.SLUG.match(name or ""):
        print(f"✗ invalid name {name!r}: use a lowercase slug (a-z, 0-9, -), 2-64 chars",
              file=sys.stderr)
        return 1
    if ptype not in _validate.PLUGIN_TYPES:
        print(f"✗ invalid --type {ptype!r}: one of {sorted(_validate.PLUGIN_TYPES)}", file=sys.stderr)
        return 1

    plugin_dir = (dest_root / name).resolve()
    if plugin_dir.exists():
        print(f"✗ {plugin_dir} already exists — pick another name or remove it first", file=sys.stderr)
        return 1

    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(_MANIFEST_STUB.format(name=name, type=ptype))
    (plugin_dir / "main.py").write_text(_ENTRYPOINT_STUB.format(name=name, type=ptype))

    # Self-check: the scaffold must pass its own vetting gate, or `init` is broken.
    _, findings = _validate.validate(plugin_dir)
    errors = [f for f in findings if f.level == "error"]
    if errors:
        for f in errors:
            print(f"  ✗ [{f.code}] {f.message}", file=sys.stderr)
        print("✗ internal error: scaffold did not pass the vetting gate", file=sys.stderr)
        return 2

    print(f"✅ scaffolded {ptype} plugin {name} → {plugin_dir}")
    print(f"   edit main.py, keep plugin.yaml `security` honest, then: enclave plugin add {plugin_dir}")
    return 0


def cmd_add(source: Path, force: bool) -> int:
    source = source.resolve()
    if not source.is_dir():
        print(f"✗ not a directory: {source}", file=sys.stderr)
        return 1

    # 1. VET FIRST — never install what the gate rejects.
    manifest, findings = _validate.validate(source)
    errors = [f for f in findings if f.level == "error"]
    for f in findings:
        mark = "✗" if f.level == "error" else "•"
        print(f"  {mark} [{f.code}] {f.message}")
    if errors:
        print(f"✗ refused to install {source.name}: {len(errors)} vetting error(s) — fix the manifest "
              f"or the code, then retry", file=sys.stderr)
        return 2

    name = manifest["name"]
    dest = _plugins_dir() / name
    if dest.exists():
        if not force:
            print(f"✗ {name} already installed at {dest} — pass --force to replace", file=sys.stderr)
            return 1
        shutil.rmtree(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Copy the plugin tree, skipping caches. No install script is ever run.
    shutil.copytree(source, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
    print(f"✅ installed {name} {manifest.get('version','')} ({manifest.get('type','?')}) → {dest}")
    if manifest.get("security", {}).get("install_script"):
        print(f"  • note: this plugin ships an install_script — enclave did NOT run it; review and run "
              f"it yourself if needed.")
    return 0


def cmd_list() -> int:
    pdir = _plugins_dir()
    if not pdir.is_dir():
        print("(no plugins installed)")
        return 0
    rows = []
    for child in sorted(pdir.iterdir()):
        if not child.is_dir():
            continue
        m = _read_manifest(child)
        if m:
            rows.append((m.get("name", child.name), m.get("version", "?"), m.get("type", "?")))
    if not rows:
        print("(no plugins installed)")
        return 0
    w = max(len(r[0]) for r in rows)
    for name, ver, typ in rows:
        print(f"{name:<{w}}  {ver:<8}  {typ}")
    return 0


def cmd_remove(name: str) -> int:
    dest = _plugins_dir() / name
    if not dest.is_dir():
        print(f"✗ not installed: {name}", file=sys.stderr)
        return 1
    shutil.rmtree(dest)
    print(f"✅ removed {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="enclave plugin", description="install / list / remove enclave plugins")
    sub = p.add_subparsers(dest="action", required=True)
    i = sub.add_parser("init", help="scaffold a new plugin skeleton that passes the vetting gate")
    i.add_argument("name", help="plugin name (lowercase slug)")
    i.add_argument("--type", default="tool", help=f"plugin type: {sorted(_validate.PLUGIN_TYPES)} (default: tool)")
    i.add_argument("--dir", default=".", help="parent directory to create the plugin in (default: cwd)")
    a = sub.add_parser("add", help="validate then install a plugin from a local dir")
    a.add_argument("source", help="path to the plugin directory (must contain plugin.yaml/json)")
    a.add_argument("--force", action="store_true", help="replace an already-installed plugin")
    sub.add_parser("list", help="list installed plugins")
    r = sub.add_parser("remove", help="remove an installed plugin")
    r.add_argument("name", help="plugin name to remove")
    return p


def main(argv) -> int:
    args = build_parser().parse_args(argv)
    if args.action == "init":
        return cmd_init(args.name, args.type, Path(args.dir))
    if args.action == "add":
        return cmd_add(Path(args.source), args.force)
    if args.action == "list":
        return cmd_list()
    if args.action == "remove":
        return cmd_remove(args.name)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
