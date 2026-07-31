#!/usr/bin/env python3
"""Proof for the runtime loader (deliverable #3): graceful skip + re-vet-on-load + bucketing.

Builds a temp plugins dir containing the honest reference plugin plus deliberately broken plugins,
then asserts load_all loads the good one, buckets it correctly, and SKIPS each broken one with a
reason — without ever raising. Stdlib unittest. Run: python3 test/test_plugin_loader.py
"""
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "plugin"))
import loader  # noqa: E402

GOOD = ROOT / "examples" / "plugins" / "sysinfo-bridge"
BAD = ROOT / "examples" / "plugins" / "_bad-example"


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _install(self, src: Path, as_name: str | None = None):
        dest = self.tmp / (as_name or src.name)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return dest

    def test_loads_good_plugin_into_right_bucket(self):
        self._install(GOOD)
        reg = loader.load_all(self.tmp)
        self.assertEqual(len(reg.all()), 1)
        self.assertEqual(len(reg.bridges), 1)
        self.assertEqual(reg.bridges[0].name, "sysinfo-bridge")
        self.assertEqual(reg.skipped, [])

    def test_empty_and_missing_dir_are_safe(self):
        self.assertEqual(loader.load_all(self.tmp).all(), [])
        self.assertEqual(loader.load_all(self.tmp / "does-not-exist").all(), [])

    def test_skips_plugin_with_no_manifest(self):
        d = self.tmp / "no-manifest"
        d.mkdir()
        (d / "whatever.py").write_text("x = 1\n")
        reg = loader.load_all(self.tmp)
        self.assertEqual(reg.all(), [])
        self.assertEqual(len(reg.skipped), 1)
        self.assertEqual(reg.skipped[0][0], "no-manifest")

    def test_skips_plugin_missing_entrypoint_but_still_loads_good_one(self):
        # broken: manifest points at an entrypoint file that doesn't exist
        d = self.tmp / "broken"
        d.mkdir()
        (d / "plugin.yaml").write_text(textwrap.dedent("""\
            name: broken
            version: 1.0.0
            type: tool
            entrypoint: missing.py
            security: {network: [], secrets: false, exec: false}
        """))
        self._install(GOOD)
        reg = loader.load_all(self.tmp)
        # good one still loads; broken one skipped -> graceful degradation
        self.assertEqual([p.name for p in reg.all()], ["sysinfo-bridge"])
        self.assertIn("broken", [n for n, _ in reg.skipped])

    def test_revets_on_load_skips_lying_plugin(self):
        # a plugin that fails the vetting gate must be skipped at LOAD, not wired in
        self._install(BAD)
        reg = loader.load_all(self.tmp)
        self.assertEqual(reg.all(), [])
        self.assertEqual(len(reg.skipped), 1)
        self.assertIn("failed vetting gate", reg.skipped[0][1])

    def test_never_raises_records_reason(self):
        self._install(GOOD)
        self._install(BAD)
        reg = loader.load_all(self.tmp)  # must not raise despite the lying plugin
        self.assertEqual([p.name for p in reg.all()], ["sysinfo-bridge"])
        self.assertEqual(len(reg.skipped), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
