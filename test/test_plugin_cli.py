#!/usr/bin/env python3
"""RED->GREEN proof for `enclave plugin` (add / list / remove).

  GREEN: `add` the honest reference plugin -> exit 0, it appears in `list`, then `remove` clears it.
  RED:   `add` the lying fixture -> exit 2 AND nothing is installed (the gate blocks it before copy).

Each test uses a fresh temp ENCLAVE_PLUGINS_DIR so nothing touches the repo. Stdlib unittest.
Run: python3 test/test_plugin_cli.py
"""
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "tools" / "plugin"))
import cli  # noqa: E402

GOOD = ROOT / "examples" / "plugins" / "sysinfo-bridge"
BAD = ROOT / "examples" / "plugins" / "_bad-example"


def run(argv):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(out):
        code = cli.main(argv)
    return code, out.getvalue()


class PluginCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["ENCLAVE_PLUGINS_DIR"] = self.tmp

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENCLAVE_PLUGINS_DIR", None)

    def test_add_good_then_list_then_remove(self):
        code, out = run(["add", str(GOOD)])
        self.assertEqual(code, 0, out)
        self.assertTrue((Path(self.tmp) / "sysinfo-bridge" / "plugin.yaml").exists())

        code, out = run(["list"])
        self.assertEqual(code, 0)
        self.assertIn("sysinfo-bridge", out)

        code, out = run(["remove", "sysinfo-bridge"])
        self.assertEqual(code, 0, out)
        self.assertFalse((Path(self.tmp) / "sysinfo-bridge").exists())

    def test_add_lying_plugin_is_refused_and_not_installed(self):
        code, out = run(["add", str(BAD)])
        self.assertEqual(code, 2, out)
        # nothing installed — the gate blocked it before any copy
        self.assertFalse((Path(self.tmp) / "telemetry-helper").exists())
        self.assertEqual([p for p in Path(self.tmp).iterdir()], [])

    def test_add_refuses_duplicate_without_force(self):
        self.assertEqual(run(["add", str(GOOD)])[0], 0)
        code, out = run(["add", str(GOOD)])
        self.assertEqual(code, 1)
        self.assertIn("already installed", out)
        self.assertEqual(run(["add", str(GOOD), "--force"])[0], 0)

    def test_init_scaffolds_a_plugin_that_passes_the_gate_and_installs(self):
        # init must produce a skeleton that (a) exists, (b) passes validate, (c) `add` accepts —
        # i.e. the authoring on-ramp can never hand a user a plugin its own gate would reject.
        with tempfile.TemporaryDirectory() as work:
            code, out = run(["init", "my-tool", "--type", "tool", "--dir", work])
            self.assertEqual(code, 0, out)
            scaffold = Path(work) / "my-tool"
            self.assertTrue((scaffold / "plugin.yaml").exists())
            self.assertTrue((scaffold / "main.py").exists())
            # the scaffold installs cleanly through the real vetting gate
            code, out = run(["add", str(scaffold)])
            self.assertEqual(code, 0, out)
            self.assertTrue((Path(self.tmp) / "my-tool" / "plugin.yaml").exists())

    def test_init_rejects_bad_name_and_type(self):
        with tempfile.TemporaryDirectory() as work:
            self.assertEqual(run(["init", "Bad_Name", "--dir", work])[0], 1)
            self.assertEqual(run(["init", "okname", "--type", "nope", "--dir", work])[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
