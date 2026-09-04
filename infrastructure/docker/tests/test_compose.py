"""Docker deployment contracts and black-box provisioning failure checks."""

from __future__ import annotations

import os
import subprocess
import tempfile
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
    def test_versioning_consumers_wait_for_database_and_provisioning(
        self,
    ) -> None:
        for filename, service in (
            ("streaming.yaml", "intake"),
            ("streaming.yaml", "vision"),
            ("dashboard.yaml", "gateway"),
        ):
            dependencies = mapping(
                mapping(services(filename)[service])["depends_on"]
            )
            self.assertEqual(
                mapping(dependencies["terminusdb"])["condition"],
                "service_healthy",
            )
            self.assertEqual(
                mapping(dependencies["terminusdb-init"])["condition"],
                "service_completed_successfully",
            )
        self.assertNotIn(
            "postgres",
            mapping(
                mapping(services("streaming.yaml")["intake"])["depends_on"]
            ),
        )
        self.assertNotIn(
            "terminusdb-init",
            mapping(
                mapping(services("dashboard.yaml")["studio"])["depends_on"]
            ),
        )

    def test_vision_discovers_all_published_tenant_pipelines(self) -> None:
        command = mapping(services("streaming.yaml")["vision"])["command"]
        self.assertIsInstance(command, list)
        self.assertNotIn("--tenant", command)
        self.assertNotIn("--pipeline-id", command)
        self.assertNotIn("--pipeline-config", command)

    def test_services_can_mount_a_deployment_connector_file(self) -> None:
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
        image = mapping(services("terminusdb.yaml")["terminusdb"])["image"]
        self.assertEqual(
            image, "${TERMINUSDB_IMAGE:-terminusdb/terminusdb-server:v12.0.7}"
        )


FAKE_CURL = """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$CURL_CALLS"
case " $* " in
    *" --max-time "*) ;;
    *) exit 90 ;;
esac
case " $* " in
    *" --write-out "*)
        case "$*" in
            *"/document/"*) printf '%s' "$CROSS_STATUS" ;;
            *) printf '%s' "$RESOURCE_STATUS" ;;
        esac ;;
esac
"""


class ProvisioningTest(unittest.TestCase):
    def run_provisioning(
        self, *, resource_status: str = "404", cross_status: str = "403"
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            curl = root / "curl"
            curl.write_text(FAKE_CURL)
            curl.chmod(0o700)
            calls = root / "calls"
            result = subprocess.run(
                ["sh", str(ROOT / "init-scripts/03-init-terminusdb.sh")],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "CURL_CALLS": str(calls),
                    "RESOURCE_STATUS": resource_status,
                    "CROSS_STATUS": cross_status,
                    "TERMINUSDB_ADMIN_PASS": "test-password",
                    "TMPDIR": directory,
                },
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            return result, calls.read_text().splitlines()

    def test_first_boot_provisions_three_distinct_databases(self) -> None:
        result, calls = self.run_provisioning()
        self.assertEqual(result.returncode, 0, result.stderr)
        for scope in ("tenant_a", "tenant_b", "bases"):
            self.assertTrue(
                any(
                    "--request POST" in call and f"/db/admin/{scope}" in call
                    for call in calls
                )
            )
        self.assertEqual(sum("/capabilities" in call for call in calls), 3)

    def test_restart_keeps_existing_users_and_databases(self) -> None:
        result, calls = self.run_provisioning(resource_status="200")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            any(
                "--request POST" in call
                and any(path in call for path in ("/db/", "/users", "/roles"))
                for call in calls
            )
        )

    def test_cross_tenant_access_aborts_startup(self) -> None:
        result, _ = self.run_provisioning(cross_status="200")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("isolation probe failed", result.stderr)

    def test_server_error_never_triggers_resource_creation(self) -> None:
        result, calls = self.run_provisioning(resource_status="503")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("--request POST" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
