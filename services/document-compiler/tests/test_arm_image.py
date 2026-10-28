"""Inspect ARM runtime layout without requiring CPU emulation on test executors."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ARM_LOADER = Path(sys.argv.pop(1)).resolve()


class ArmImageTest(unittest.TestCase):
    def test_arm_image_preserves_elf_interpreter(self) -> None:
        subprocess.run(
            ["/bin/bash", str(ARM_LOADER)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        created = subprocess.run(
            [
                "docker",
                "create",
                "--platform",
                "linux/arm64",
                "local/document-worker:arm64-test",
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        container = created.stdout.decode().strip()
        try:
            with tempfile.TemporaryDirectory() as directory:
                copied = subprocess.run(
                    [
                        "docker",
                        "cp",
                        "-L",
                        container + ":/lib/ld-linux-aarch64.so.1",
                        directory + "/loader",
                    ],
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(copied.returncode, 0, copied.stderr.decode())
                self.assertEqual(
                    Path(directory, "loader").read_bytes()[:4], b"\x7fELF"
                )
        finally:
            subprocess.run(
                ["docker", "rm", container],
                check=True,
                capture_output=True,
                timeout=30,
            )


if __name__ == "__main__":
    unittest.main()
