"""Checks isolated cluster cleanup when provisioning or tests fail."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure.kubernetes.cluster import isolated_cluster


class ClusterLifecycleTest(unittest.TestCase):
    def test_cleanup_failure_does_not_replace_the_primary_failure(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("infrastructure.kubernetes.cluster.run") as run,
        ):
            run.side_effect = ["", "", subprocess.TimeoutExpired("k3d", 300)]
            with self.assertRaisesRegex(ValueError, "primary failure"):
                with isolated_cluster(Path("/tmp/tools"), Path(temporary)):
                    raise ValueError("primary failure")

    def test_cleanup_runs_when_cluster_creation_fails(self) -> None:
        with patch("infrastructure.kubernetes.cluster.run") as run:
            run.side_effect = [
                subprocess.CalledProcessError(1, "k3d"),
                "server diagnostics",
                "",
            ]
            with self.assertRaises(subprocess.CalledProcessError):
                with isolated_cluster(Path("/tmp/tools"), Path("/tmp/state")):
                    self.fail("An unavailable cluster must not run tests")
        self.assertEqual(
            run.call_args_list[-1].args[0][1:3], ["cluster", "delete"]
        )
        create = run.call_args_list[0].args[0]
        self.assertIn("--no-rollback", create)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["docker", "logs", "--tail", "200", f"k3d-{create[3]}-server-0"],
        )
        self.assertEqual(run.call_args_list[1].kwargs["timeout"], 30)

    def test_diagnostics_failure_preserves_cluster_creation_failure(
        self,
    ) -> None:
        failure = subprocess.CalledProcessError(1, "k3d")
        with patch("infrastructure.kubernetes.cluster.run") as run:
            run.side_effect = [
                failure,
                subprocess.TimeoutExpired("docker", 30),
                "",
            ]
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                with isolated_cluster(Path("/tmp/tools"), Path("/tmp/state")):
                    self.fail("An unavailable cluster must not run tests")
        self.assertIs(raised.exception, failure)
        self.assertEqual(
            run.call_args_list[-1].args[0][1:3], ["cluster", "delete"]
        )

    def test_cleanup_preserves_test_failure_and_uses_a_unique_name(
        self,
    ) -> None:
        names: list[str] = []
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch(
                "infrastructure.kubernetes.cluster.run", return_value=""
            ) as run,
        ):
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "primary failure"):
                    with isolated_cluster(Path("/tmp/tools"), Path(temporary)):
                        names.append(run.call_args_list[-2].args[0][3])
                        raise ValueError("primary failure")
        self.assertEqual(len(set(names)), 2)
        self.assertTrue(
            all(name.startswith("galadril-test-") for name in names)
        )


if __name__ == "__main__":
    unittest.main()
