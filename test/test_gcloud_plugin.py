#!/usr/bin/env python3
"""Deliverable #4 proof: a REAL enclave bridge (gcloud) goes add -> load end-to-end.

Proves the plugin system works on non-toy code that legitimately needs network + secret access:
the honest manifest passes the vetting gate (with an install_script WARN, not an error), the CLI
installs it, and the runtime loader buckets it as a bridge. Stdlib unittest.
Run: python3 test/test_gcloud_plugin.py
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "plugin"))
import validate as _validate  # noqa: E402
import cli  # noqa: E402
import loader  # noqa: E402

GCLOUD = ROOT / "examples" / "plugins" / "gcloud-bridge"


class GcloudPluginEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["ENCLAVE_PLUGINS_DIR"] = self.tmp

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ENCLAVE_PLUGINS_DIR", None)

    def test_real_bridge_passes_gate_with_declared_caps(self):
        manifest, findings = _validate.validate(GCLOUD)
        errors = [f for f in findings if f.level == "error"]
        self.assertEqual(errors, [], f"real bridge should pass; errors: {[f.code for f in errors]}")
        # its declared network + secret access do NOT trip the gate because they're declared
        self.assertTrue(manifest["security"]["secrets"])
        self.assertIn("host.docker.internal", manifest["security"]["network"])
        # the host-side install script is surfaced as a WARN (human runs it), not silently ignored
        self.assertIn("install-script", [f.code for f in findings])

    def test_add_then_load_buckets_as_bridge(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = cli.main(["add", str(GCLOUD)])
        self.assertEqual(code, 0, out.getvalue())

        reg = loader.load_all(self.tmp)
        self.assertEqual([p.name for p in reg.bridges], ["gcloud-bridge"])
        self.assertEqual(reg.skipped, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
