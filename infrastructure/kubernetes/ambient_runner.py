"""Runs the real Ambient security suite in a disposable k3s cluster."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path

from infrastructure.kubernetes.cluster import (
    install_ambient,
    isolated_cluster,
    run,
)
from infrastructure.kubernetes.test_ambient import AmbientIntegrationTest

ROOT = Path(__file__).absolute().parent


def cancelled(signum: int, frame: object) -> None:
    """Unwinds context managers when Bazel cancels the test process."""
    raise InterruptedError(f"Ambient test cancelled by signal {signum}")


def main() -> int:
    """Keeps kubeconfig, Helm caches, cluster resources and ports test-owned."""
    signal.signal(signal.SIGTERM, cancelled)
    test_tmpdir = os.environ.get("TEST_TMPDIR")
    temporary_dir_root: str | None = None
    if test_tmpdir:
        base_tmp = Path(tempfile.gettempdir()).resolve()
        candidate_tmp = Path(test_tmpdir).resolve()
        try:
            candidate_tmp.relative_to(base_tmp)
            temporary_dir_root = str(candidate_tmp)
        except ValueError:
            temporary_dir_root = None

    with tempfile.TemporaryDirectory(dir=temporary_dir_root) as temporary:
        state = Path(temporary)
        os.environ.update(
            {
                "KUBECONFIG": str(state / "kubeconfig"),
                "HELM_CACHE_HOME": str(state / "helm/cache"),
                "HELM_CONFIG_HOME": str(state / "helm/config"),
                "HELM_DATA_HOME": str(state / "helm/data"),
                "GALADRIL_MESH_E2E": "1",
                "PATH": str(ROOT / "bin")
                + os.pathsep
                + os.environ.get("PATH", ""),
            }
        )
        with isolated_cluster(ROOT / "bin", state):
            try:
                install_ambient(ROOT)
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(
                    AmbientIntegrationTest
                )
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                if result.wasSuccessful():
                    return 0
            finally:
                for args in (
                    ["get", "pods", "--all-namespaces", "-o", "wide"],
                    [
                        "get",
                        "events",
                        "--all-namespaces",
                        "--sort-by=.lastTimestamp",
                    ],
                    [
                        "logs",
                        "-n",
                        "istio-system",
                        "-l",
                        "app=ztunnel",
                        "--tail=100",
                    ],
                ):
                    try:
                        print(
                            run(
                                [str(ROOT / "bin/kubectl"), *args],
                                check=False,
                                timeout=30,
                            )
                        )
                    except (OSError, subprocess.SubprocessError) as error:
                        print(
                            f"Cluster diagnostic unavailable: {type(error).__name__}"
                        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
