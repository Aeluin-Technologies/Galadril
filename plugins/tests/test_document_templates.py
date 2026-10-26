"""Packaged template discovery never imports executable plugin bindings."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import galadril_plugins


class PackagedTemplatesTest(unittest.TestCase):
    def test_packaged_classes_are_discovered_with_trusted_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "galadril_plugins"
            shutil.copytree(Path(galadril_plugins.__file__).parent, package)
            shutil.copytree(
                Path(__file__).parent / "fixtures/fixture", package / "fixture"
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    """
import sys
from galadril_plugins import PluginRegistry
inventory = PluginRegistry.discover().document_templates()
assert len(inventory) == 1
entry = inventory[0]
assert entry['plugin_id'] == 'fixture'
assert entry['plugin_version'] == '1.0.0'
assert entry['template']['scope'] == 'plugin'
assert entry['template']['plugin'] == 'fixture'
assert 'owner' not in entry['template']
assert any(asset['path'] == 'letter.cls' for asset in entry['template']['assets'])
assert 'galadril_plugins.fixture.handlers' not in sys.modules
assert 'galadril_plugins.fixture.scribe' not in sys.modules
""",
                ],
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join((directory, *sys.path)),
                },
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, result.stdout + result.stderr
            )


if __name__ == "__main__":
    unittest.main()
