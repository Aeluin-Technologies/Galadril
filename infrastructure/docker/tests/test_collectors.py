"""Runs upstream validation of the shipped observability configurations."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast

from testcontainers.core.container import DockerContainer

from infrastructure.docker.tests.test_models import model

ROOT = Path(__file__).absolute().parents[2]


class CollectorConfigurationTest(unittest.TestCase):
    def test_shipped_collectors_accept_their_configuration(self) -> None:
        services = model("infrastructure/docker/docker-compose.yaml")
        for name, source, target, command in (
            (
                "tempo",
                "tempo.yaml",
                "/etc/tempo.yaml",
                ["-config.file=/etc/tempo.yaml", "-config.verify=true"],
            ),
            (
                "profiler",
                "alloy.config",
                "/etc/alloy/config.alloy",
                ["validate", "/etc/alloy/config.alloy"],
            ),
            (
                "otel-collector",
                "otel-collector.yaml",
                "/etc/otelcol-contrib/config.yaml",
                ["validate", "--config=/etc/otelcol-contrib/config.yaml"],
            ),
        ):
            with self.subTest(service=name):
                container = DockerContainer(cast(str, services[name]["image"]))
                container.with_volume_mapping(
                    str(ROOT / "observability" / source), target, "ro"
                )
                with container.with_command(command):
                    wrapped = container.get_wrapped_container()
                    result = wrapped.wait(timeout=90)
                    self.assertEqual(
                        result["StatusCode"],
                        0,
                        wrapped.logs().decode(errors="replace"),
                    )


if __name__ == "__main__":
    unittest.main()
