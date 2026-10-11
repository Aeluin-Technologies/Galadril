"""Verifies that sandbox source symlinks become readable container inputs."""

from __future__ import annotations

import ast
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


class NuxtStagingTest(unittest.TestCase):
    def test_container_receives_materialized_package_manifest(self) -> None:
        source = Path(__file__).with_name("nuxt_pnpm_layer.bzl").read_text()
        match = re.search(r'    command = """\n(.*?)\n"""', source, re.DOTALL)
        self.assertIsNotNone(match)
        if match is None:
            raise AssertionError("Missing build command")
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            manifest = root / "package.json"
            manifest.write_text("{}")
            link = root / "sandbox-package.json"
            link.symlink_to(manifest)
            sources = root / "sources.tsv"
            sources.write_text(f"{link}\tpackage.json\n")
            docker = root / "docker"
            docker.write_text("""#!/bin/sh
set -eu
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--volume" ]; then
    stage="${2%:/workspace}"
    shift
  fi
  shift
done
test ! -L "$stage/project/package.json"
test "$(cat "$stage/project/package.json")" = '{}'
printf artifact > "$stage/layer.tar"
""")
            docker.chmod(0o755)
            output = root / "layer.tar"
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    ast.literal_eval('"""\n' + match.group(1) + '\n"""'),
                    "test",
                    str(sources),
                    "layer.tar",
                    "test-image",
                    "linux/amd64",
                    "11.6.0",
                    "build",
                    "src",
                ],
                cwd=root,
                env=os.environ
                | {"PATH": str(root) + os.pathsep + os.environ["PATH"]},
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text(), "artifact")


if __name__ == "__main__":
    unittest.main()
