"""Exercises the upstream wrapper with an exporter that rejects VFS output."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).absolute().parent / "buildx_command.sh"
EXPORTER = """#!/bin/bash
set -euo pipefail
printf '%s\\n' "$1" >> "$CALLS"
if [ "$1" != build ]; then exit 0; fi
for arg in "$@"; do
    case "$arg" in
        --output=type=oci,tar=false,dest=*) output="${arg#*=oci,tar=false,dest=}" ;;
    esac
done
case "$output" in
    "$WORKSPACE"/*|out) echo 'Exporter cannot use the VFS workspace' >&2; exit 73 ;;
esac
mkdir -p "$output/blobs/sha256"
printf '%s' '{"manifests":[]}' > "$output/index.json"
if [ "${FAIL_BUILD:-false}" = true ]; then exit 42; fi
printf '%s' 'image-and-attestations' > "$output/blobs/sha256/test"
"""


class BuildxExportTest(unittest.TestCase):
    def invoke(
        self, *, fail: bool
    ) -> tuple[int, str, dict[str, str], str, bool]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            scratch = root / "scratch"
            workspace.mkdir()
            scratch.mkdir()
            (workspace / "Dockerfile").write_text("FROM scratch\n")
            (workspace / "context").mkdir()
            exporter = root / "buildx"
            exporter.write_text(EXPORTER)
            exporter.chmod(0o755)
            calls = root / "calls"
            result = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    str(exporter),
                    "",
                    "test-builder",
                    "Dockerfile",
                    "1",
                    "context",
                    "--output=type=oci,tar=false,dest=out",
                    "--platform",
                    "linux/amd64",
                    ".",
                ],
                cwd=workspace,
                env=dict(
                    os.environ,
                    TMPDIR=str(scratch),
                    WORKSPACE=str(workspace),
                    CALLS=str(calls),
                    FAIL_BUILD=str(fail).lower(),
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                timeout=15,
            )
            output = workspace / "out"
            contents = {
                path.relative_to(output).as_posix(): path.read_text()
                for path in output.rglob("*")
                if path.is_file()
            }
            return (
                result.returncode,
                result.stdout,
                contents,
                calls.read_text() if calls.exists() else "",
                any(scratch.iterdir()),
            )

    def test_oci_export_uses_scratch_and_publishes_the_complete_layout(
        self,
    ) -> None:
        code, diagnostics, contents, calls, scratch_remaining = self.invoke(
            fail=False
        )
        self.assertEqual(code, 0, diagnostics)
        self.assertEqual(
            contents,
            {
                "index.json": '{"manifests":[]}',
                "blobs/sha256/test": "image-and-attestations",
            },
        )
        self.assertEqual(calls.splitlines(), ["create", "build", "rm"])
        self.assertFalse(scratch_remaining)

    def test_failed_export_cleans_up_and_does_not_publish_partial_output(
        self,
    ) -> None:
        code, diagnostics, contents, calls, scratch_remaining = self.invoke(
            fail=True
        )
        self.assertEqual(code, 42, diagnostics)
        self.assertEqual(contents, {})
        self.assertEqual(calls.splitlines(), ["create", "build", "rm"])
        self.assertFalse(scratch_remaining)


if __name__ == "__main__":
    unittest.main()
