#!/usr/bin/env python3
"""
enclave plugin runtime loader (backlog deliverable #3).

At startup enclave calls `load_all(plugins_dir)` to discover installed plugins and wire them by type:

  bridge   -> registered so its host-capability endpoint is reachable
  tool     -> mounted as an agent-callable tool
  template -> surfaced in `enclave init --template <name>`
  policy   -> merged into the guard policy

Two properties matter and are both tested:
  1. GRACEFUL SKIP — one broken plugin (bad manifest, missing entrypoint, or a plugin that no longer
     passes the vetting gate) is skipped with a logged reason; it NEVER aborts startup or the load of
     the other plugins.
  2. RE-VET ON LOAD — every plugin is re-run through `validate.py` at load time, not just at install.
     A plugin dir can be edited after `enclave plugin add`; loading is the last line of defence, so a
     plugin that fails the gate now is skipped (fail-closed) rather than wired in.

`load_all` returns a PluginRegistry the startup code consumes. Stdlib only (+ PyYAML via validate.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate as _validate  # noqa: E402


class LoadedPlugin:
    def __init__(self, name, version, ptype, directory: Path, entrypoint: Path, manifest: dict):
        self.name = name
        self.version = version
        self.type = ptype
        self.dir = directory
        self.entrypoint = entrypoint
        self.manifest = manifest

    def __repr__(self):
        return f"<LoadedPlugin {self.name} {self.version} ({self.type})>"


class PluginRegistry:
    def __init__(self):
        self.bridges: list[LoadedPlugin] = []
        self.tools: list[LoadedPlugin] = []
        self.templates: list[LoadedPlugin] = []
        self.policies: list[LoadedPlugin] = []
        self.skipped: list[tuple[str, str]] = []   # (name, reason)

    _BUCKET = {"bridge": "bridges", "tool": "tools", "template": "templates", "policy": "policies"}

    def _add(self, p: LoadedPlugin):
        getattr(self, self._BUCKET[p.type]).append(p)

    def all(self) -> list[LoadedPlugin]:
        return [*self.bridges, *self.tools, *self.templates, *self.policies]

    def by_type(self, ptype: str) -> list[LoadedPlugin]:
        return list(getattr(self, self._BUCKET[ptype], []))

    def template_names(self) -> list[str]:
        """Names to expose in `enclave init --template <name>`."""
        return [p.name for p in self.templates]

    def summary(self) -> str:
        return (f"{len(self.all())} plugin(s) loaded "
                f"(bridges={len(self.bridges)} tools={len(self.tools)} "
                f"templates={len(self.templates)} policies={len(self.policies)}), "
                f"{len(self.skipped)} skipped")


def load_all(plugins_dir, *, log=None) -> PluginRegistry:
    """Discover, re-vet and bucket every plugin under `plugins_dir`. Never raises on a bad plugin."""
    reg = PluginRegistry()
    pdir = Path(plugins_dir)

    def _warn(name, reason):
        reg.skipped.append((name, reason))
        if log:
            log(f"plugin '{name}' skipped: {reason}")

    if not pdir.is_dir():
        return reg

    for child in sorted(pdir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        try:
            # Re-vet on load — fail-closed. A dir edited after install is caught here.
            manifest, findings = _validate.validate(child)
            errors = [f for f in findings if f.level == "error"]
            if errors:
                _warn(child.name, "failed vetting gate on load: "
                                  + "; ".join(f"[{f.code}] {f.message}" for f in errors))
                continue
            ptype = manifest["type"]
            entry = (child / manifest["entrypoint"]).resolve()
            reg._add(LoadedPlugin(manifest["name"], manifest.get("version", "?"),
                                  ptype, child, entry, manifest))
        except SystemExit as e:            # _load_manifest raises SystemExit on a missing/bad manifest
            _warn(child.name, f"unloadable manifest: {e}")
        except Exception as e:             # any other malformed plugin — never abort the whole load
            _warn(child.name, f"load error: {type(e).__name__}: {e}")

    return reg


if __name__ == "__main__":
    import os
    target = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
        "ENCLAVE_PLUGINS_DIR", str(Path(__file__).resolve().parents[2] / "plugins"))
    r = load_all(target, log=lambda m: print(f"  ! {m}", file=sys.stderr))
    print(r.summary())
    for p in r.all():
        print(f"  ✓ {p.name} {p.version} ({p.type}) -> {p.entrypoint}")
