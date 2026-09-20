"""Docker deployment contracts for Registry-owned artifact persistence."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).parent.parent


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a configuration mapping")
    return cast(dict[str, object], value)


def services(filename: str) -> dict[str, object]:
    return mapping(
        mapping(yaml.safe_load((ROOT / filename).read_text()))["services"]
    )


class ComposeContractTest(unittest.TestCase):
    def test_artifact_consumers_wait_only_for_registry(self) -> None:
        """Keeps lakeFS and S3 topology private to the Registry service."""
        for filename, service in (
            ("streaming.yaml", "intake"),
            ("streaming.yaml", "vision"),
            ("dashboard.yaml", "gateway"),
        ):
            dependencies = mapping(
                mapping(services(filename)[service])["depends_on"]
            )
            self.assertEqual(
                mapping(dependencies["registry"])["condition"],
                "service_started",
            )
            self.assertNotIn("lakefs", dependencies)

    def test_registry_alone_receives_lakefs_credentials(self) -> None:
        registry = mapping(services("streaming.yaml")["registry"])
        environment = mapping(registry["environment"])
        self.assertIn("LAKEFS_ACCESS_KEY_ID", environment)
        self.assertIn("LAKEFS_SECRET_ACCESS_KEY", environment)
        self.assertEqual(
            environment["REGISTRY_CONFIG_PATH"], "/connectors.yaml"
        )
        self.assertNotIn("REGISTRY_S3_ENDPOINT", environment)
        self.assertIn("/connectors.yaml:ro", mapping(registry)["volumes"][0])
        self.assertEqual(
            environment["REGISTRY_STORAGE_NAMESPACE"], "s3://lake/"
        )
        for filename, service in (
            ("streaming.yaml", "intake"),
            ("streaming.yaml", "vision"),
            ("dashboard.yaml", "gateway"),
        ):
            consumer = mapping(services(filename)[service])
            consumer_environment = mapping(consumer["environment"])
            self.assertFalse(
                any(key.startswith("LAKEFS_") for key in consumer_environment)
            )
            self.assertFalse(
                any(
                    key.startswith("REGISTRY_S3_")
                    for key in consumer_environment
                )
            )

    def test_lakefs_uses_s3_as_its_physical_store(self) -> None:
        lakefs = mapping(services("s3.yaml")["lakefs"])
        environment = mapping(lakefs["environment"])
        self.assertEqual(environment["LAKEFS_BLOCKSTORE_TYPE"], "s3")
        self.assertEqual(
            environment["LAKEFS_BLOCKSTORE_S3_ENDPOINT"],
            "http://minio:9000",
        )
        self.assertEqual(
            lakefs["image"], "${LAKEFS_IMAGE:-treeverse/lakefs:1.86.0}"
        )

    def test_minio_uses_pinned_official_images(self) -> None:
        """Prevents CI startup from depending on removed Docker Hub images."""
        minio = mapping(services("s3.yaml")["minio"])
        minio_init = mapping(services("s3.yaml")["minio-init"])
        self.assertEqual(
            minio["image"],
            "${MINIO_IMAGE:-quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z}",
        )
        self.assertEqual(
            minio_init["image"],
            "${MINIO_CLIENT_IMAGE:-quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z}",
        )

    def test_vision_uses_one_explicit_tenant_pipeline(self) -> None:
        vision = mapping(services("streaming.yaml")["vision"])
        command = vision["command"]
        self.assertIsInstance(command, list)
        self.assertNotIn("--pipeline-config", command)
        environment = mapping(vision["environment"])
        self.assertIn("VISION_TENANT_ID", environment)
        self.assertIn("VISION_PIPELINE_ID", environment)

    def test_services_mount_one_trusted_connector_file(self) -> None:
        for filename, service in (
            ("streaming.yaml", "intake"),
            ("streaming.yaml", "vision"),
            ("dashboard.yaml", "gateway"),
        ):
            volumes = mapping(services(filename)[service])["volumes"]
            self.assertIn(
                "${GALADRIL_CONNECTORS_PATH:-../../examples/connectors.yaml}:/connectors.yaml:ro",
                volumes,
            )


if __name__ == "__main__":
    unittest.main()
