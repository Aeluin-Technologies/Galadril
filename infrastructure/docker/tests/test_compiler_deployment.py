"""Deployment contracts keep orchestration shared and worker resources restricted."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).resolve().parents[2]


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a configuration mapping")
    return cast(dict[str, object], value)


def records(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise TypeError("Expected configuration records")
    return [mapping(item) for item in value]


class CompilerDeploymentTest(unittest.TestCase):
    def test_compose_runs_only_one_permanent_compiler_container(self) -> None:
        services = mapping(
            mapping(
                yaml.safe_load(
                    (ROOT / "docker/document-compiler.yaml").read_text()
                )
            )["services"]
        )
        self.assertNotIn("document-supervisor", services)
        api = mapping(services["document-compiler"])
        environment = mapping(api["environment"])
        self.assertNotIn("DOCUMENT_COMPILER_WORKER_SOCKET", environment)
        self.assertEqual(
            environment["DOCUMENT_COMPILER_WORKER_ROOTFS"], "/worker-root"
        )
        self.assertIn("document-worker-root:/worker-root:ro", api["volumes"])
        self.assertIn("document-worker-rootfs", mapping(api["depends_on"]))
        self.assertEqual(api["mem_limit"], "3072m")
        self.assertEqual(api["cpus"], 3)
        self.assertTrue(api["read_only"])
        self.assertEqual(api["cap_drop"], ["ALL"])
        self.assertTrue(api["tmpfs"])
        initializer = mapping(services["document-worker-rootfs"])
        self.assertEqual(initializer["network_mode"], "none")
        self.assertNotIn(
            "DOCUMENT_COMPILER_S3_SECRET_KEY",
            mapping(initializer["environment"]),
        )

    def test_kubernetes_keeps_only_initializer_and_combined_api(self) -> None:
        documents = yaml.safe_load_all(
            (
                ROOT / "kubernetes-overlays/document-compiler/sandbox.yaml"
            ).read_text()
        )
        deployment = next(
            mapping(doc)
            for doc in documents
            if mapping(doc).get("kind") == "Deployment"
        )
        pod = mapping(mapping(mapping(deployment["spec"])["template"])["spec"])
        containers = records(pod["containers"])
        self.assertEqual(
            [container["name"] for container in containers], ["api"]
        )
        api = containers[0]
        mounts = {
            entry["name"]: entry for entry in records(api["volumeMounts"])
        }
        self.assertTrue(mounts["worker-root"]["readOnly"])
        self.assertEqual(
            mounts["state"]["mountPath"], "/run/document-compiler-state"
        )
        self.assertNotIn("ipc", mounts)
        env = {entry["name"]: entry for entry in records(api["env"])}
        self.assertEqual(
            env["DOCUMENT_COMPILER_WORKER_ROOTFS"]["value"], "/worker-root"
        )
        self.assertNotIn("DOCUMENT_COMPILER_WORKER_SOCKET", env)
        limits = mapping(mapping(api["resources"])["limits"])
        self.assertEqual(limits["memory"], "3Gi")
        self.assertEqual(str(limits["cpu"]), "3")
        self.assertEqual(
            mapping(api["securityContext"])["capabilities"], {"drop": ["ALL"]}
        )
        self.assertEqual(
            [entry["name"] for entry in records(pod["initContainers"])],
            ["tex-bundle", "worker-rootfs"],
        )
        self.assertNotIn("envFrom", records(pod["initContainers"])[0])
        self.assertNotIn(
            "ipc", [entry["name"] for entry in records(pod["volumes"])]
        )


if __name__ == "__main__":
    unittest.main()
