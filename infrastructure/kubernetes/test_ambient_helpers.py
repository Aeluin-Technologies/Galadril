"""Verifies Ambient orchestration without requiring a Kubernetes cluster."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from infrastructure.kubernetes import test_ambient


class KubectlCommandTest(unittest.TestCase):
    def test_environment_cannot_replace_the_kubectl_executable(self) -> None:
        """Prevents environment values from becoming executable commands."""
        with (
            patch.dict(os.environ, {"GALADRIL_KUBECTL": "/tmp/untrusted-tool"}),
            patch.object(
                test_ambient.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="ready"),
            ) as run,
        ):
            self.assertEqual(
                test_ambient.kubectl("apply", "-f", "-", payload="fixture"),
                "ready",
            )
        run.assert_called_once_with(
            ["kubectl", "apply", "-f", "-"],
            input="fixture",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=210,
            check=False,
        )

    def test_failed_command_preserves_the_primary_diagnostic(self) -> None:
        """Keeps cluster failures actionable after restricting the executable."""
        with patch.object(
            test_ambient.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                [], 1, stdout="cluster unavailable"
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "cluster unavailable"):
                test_ambient.kubectl("get", "pods")
            self.assertEqual(
                test_ambient.kubectl("get", "pods", check=False),
                "cluster unavailable",
            )


if __name__ == "__main__":
    unittest.main()
