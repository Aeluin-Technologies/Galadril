"""Exercises the upstream wrapper with an exporter that rejects VFS output."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).absolute().parent / "buildx_command.sh"
ARGS = SCRIPT.with_name("buildx_command.args")
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
case "$FAILURE" in
    permanent) exit 42 ;;
    reset|repeated-reset)
        if [[ "$FAILURE" == repeated-reset ]] || [[ "$(grep -c '^build$' "$CALLS")" == 1 ]]; then
            touch "$output/partial"
            echo 'ERROR: failed to build: failed to solve: failed to compute cache key: failed to copy: read tcp: connection reset by peer' >&2
            exit 42
        fi
        test ! -e "$output/partial"
        ;;
esac
printf '%s' 'image-and-attestations' > "$output/blobs/sha256/test"
"""


class BuildxExportTest(unittest.TestCase):
    def test_builder_pins_the_emulator_instead_of_using_buildx_stable(
        self,
    ) -> None:
        """The implicit Buildx image can retain a broken ARM64 ldconfig emulator."""
        script = SCRIPT.read_text()
        self.assertIn("--driver-opt", script)
        self.assertIn(
            "image=moby/buildkit@sha256:"
            "b059f8d7226d0b326bc871af489a5dddf65e36af4d20d14251b072974d9ee87c",
            script,
        )

    def test_sbom_scanner_is_an_immutable_action_input(self) -> None:
        self.assertIn(
            "--attest=type=sbom,generator=docker/buildkit-syft-scanner@sha256:"
            "873a7458c3c8ac8e32c7b796c10e1600b07b963d2aa000a72a2d97aaaa2d6315",
            ARGS.read_text(),
        )

    def invoke(
        self, *, failure: str = ""
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
                    FAILURE=failure,
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
        code, diagnostics, contents, calls, scratch_remaining = self.invoke()
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

    def test_registry_reset_reuses_builder_and_discards_partial_export(
        self,
    ) -> None:
        code, diagnostics, contents, calls, scratch_remaining = self.invoke(
            failure="reset"
        )
        self.assertEqual(code, 0, diagnostics)
        self.assertNotIn("partial", contents)
        self.assertEqual(calls.splitlines(), ["create", "build", "build", "rm"])
        self.assertFalse(scratch_remaining)

    def test_registry_resets_exhaust_a_bounded_retry_budget(self) -> None:
        code, diagnostics, contents, calls, scratch_remaining = self.invoke(
            failure="repeated-reset"
        )
        self.assertEqual(code, 42, diagnostics)
        self.assertEqual(contents, {})
        self.assertEqual(
            calls.splitlines(), ["create", "build", "build", "build", "rm"]
        )
        self.assertFalse(scratch_remaining)

    def test_failed_export_cleans_up_and_does_not_publish_partial_output(
        self,
    ) -> None:
        code, diagnostics, contents, calls, scratch_remaining = self.invoke(
            failure="permanent"
        )
        self.assertEqual(code, 42, diagnostics)
        self.assertEqual(contents, {})
        self.assertEqual(calls.splitlines(), ["create", "build", "rm"])
        self.assertFalse(scratch_remaining)


if __name__ == "__main__":
    unittest.main()
