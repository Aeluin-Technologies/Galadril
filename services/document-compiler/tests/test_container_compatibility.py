"""The actual worker image enforces its policy or refuses an incompatible kernel."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from worker_fixture import offline_bundle

MANIFEST = Path(sys.argv.pop(1)).resolve()
LOADER = Path(sys.argv.pop(1)).resolve()
API_LOADER = Path(sys.argv.pop(1)).resolve()


class ContainerPolicyTest(unittest.TestCase):
    def test_state_initializer_is_repeatable_with_only_chown_capability(
        self,
    ) -> None:
        subprocess.run(
            ["/bin/bash", str(API_LOADER)],
            check=True,
            capture_output=True,
            timeout=180,
        )
        volume = "document-state-" + uuid.uuid4().hex
        subprocess.run(
            ["docker", "volume", "create", volume],
            check=True,
            capture_output=True,
            timeout=30,
        )
        try:
            for _attempt in range(2):
                initialized = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0:0",
                        "--cap-drop",
                        "ALL",
                        "--cap-add",
                        "CHOWN",
                        "-v",
                        volume + ":/var/lib/document-compiler",
                        "local/document-api:test",
                        "--prepare-state",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(
                    initialized.returncode, 0, initialized.stderr.decode()
                )
        finally:
            subprocess.run(
                ["docker", "volume", "rm", volume],
                check=True,
                capture_output=True,
                timeout=30,
            )

    def test_container_enforces_or_refuses_unavailable_kernel_policy(
        self,
    ) -> None:
        loaded = subprocess.run(
            ["/bin/bash", str(LOADER)],
            capture_output=True,
            timeout=180,
            check=False,
        )
        self.assertEqual(loaded.returncode, 0, loaded.stderr.decode())
        name = "document-worker-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "bundle.ttb"
            offline_bundle(MANIFEST, bundle)
            # Transfer through Docker stdin: remote daemons cannot mount runner paths.
            volume = "document-bundle-" + uuid.uuid4().hex
            subprocess.run(
                ["docker", "volume", "create", volume],
                check=True,
                capture_output=True,
            )
            try:
                copied = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "-i",
                        "-v",
                        f"{volume}:/bundle",
                        "busybox:1.37.0-musl",
                        "sh",
                        "-c",
                        "cat > /bundle/bundle.ttb; sha256sum /bundle/bundle.ttb | cut -d ' ' -f 1 > /bundle/bundle.sha256; chmod 644 /bundle/bundle.ttb /bundle/bundle.sha256",
                    ],
                    input=bundle.read_bytes(),
                    capture_output=True,
                    check=False,
                    timeout=60,
                )
                self.assertEqual(copied.returncode, 0, copied.stderr.decode())
                (bundle.parent / "bundle.sha256").write_text(
                    hashlib.sha256(bundle.read_bytes()).hexdigest() + "\n"
                )
                installed = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "-v",
                        f"{volume}:/bundle-source",
                        "-v",
                        f"{volume}:/worker-root",
                        "local/document-worker:test",
                        "--install-rootfs",
                        "/worker-root/runtime",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=90,
                )
                self.assertEqual(
                    installed.returncode, 0, installed.stderr.decode()
                )
                repeated = subprocess.run(
                    installed.args,
                    check=False,
                    capture_output=True,
                    timeout=90,
                )
                self.assertEqual(
                    repeated.returncode, 0, repeated.stderr.decode()
                )
                project = {
                    "entrypoint": list(
                        rb"\documentclass{letter}\begin{document}\reference{Test} Hello\end{document}"
                    ),
                    "assets": [
                        {
                            "path": "letter.cls",
                            "data": list(
                                rb"\NeedsTeXFormat{LaTeX2e}\ProvidesClass{letter}\LoadClass{article}\newcommand{\reference}[1]{\textbf{#1}}"
                            ),
                        }
                    ],
                }
                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--name",
                        name,
                        "-i",
                        "--network",
                        "none",
                        "--read-only",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges",
                        "--user",
                        "1000:1000",
                        "--memory",
                        "1g",
                        "--cpus",
                        "1",
                        "--pids-limit",
                        "8",
                        "--tmpfs",
                        "/tmp:size=128m,mode=1777,noexec",
                        "--tmpfs",
                        "/workspace:size=32m,mode=1777,noexec",
                        "--mount",
                        f"type=volume,source={volume},target=/worker-root,readonly",
                        "--entrypoint",
                        "/worker-root/runtime/compiler-worker",
                        "--env",
                        "DOCUMENT_COMPILER_WORKER_ROOTFS=/worker-root/runtime",
                        "--env",
                        "DOCUMENT_COMPILER_WORKSPACE=/workspace",
                        "--env",
                        "DOCUMENT_COMPILER_PARENT_PID=0",
                        "--env",
                        "LD_LIBRARY_PATH=/worker-root/runtime/usr/lib/x86_64-linux-gnu:/worker-root/runtime/lib/x86_64-linux-gnu",
                        "local/document-worker:test",
                        "--probe",
                    ],
                    input=json.dumps(project).encode(),
                    capture_output=True,
                    check=False,
                    timeout=90,
                )
                if result.returncode == 0:
                    self.assertEqual(result.stdout, b"ready\n")
                else:
                    self.assertIn(
                        b"fully incompatible access-rights", result.stderr
                    )
                    self.assertEqual(result.stdout, b"")
            finally:
                subprocess.run(
                    ["docker", "rm", "--force", name],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
                subprocess.run(
                    ["docker", "volume", "rm", "--force", volume],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )


if __name__ == "__main__":
    unittest.main()
