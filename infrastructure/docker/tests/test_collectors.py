"""Runs upstream validation of shipped auxiliary service configurations."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from typing import cast

import httpx
from testcontainers.core.container import DockerContainer

from infrastructure.docker.tests.test_models import model

ROOT = Path(__file__).absolute().parents[2]


class CollectorConfigurationTest(unittest.TestCase):
    def test_default_search_service_serves_the_gateway_json_contract(
        self,
    ) -> None:
        services = model("infrastructure/docker/docker-compose.yaml")
        search = services["searxng"]
        container = DockerContainer(cast(str, search["image"]))
        for name, value in cast(dict[str, str], search["environment"]).items():
            container.with_env(name, value)
        container.with_kwargs(
            user=search["user"],
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            mem_limit="512m",
            pids_limit=128,
        )
        container.with_volume_mapping(
            str(ROOT / "docker/searxng/settings.yml"),
            "/etc/searxng/settings.yml",
            "ro",
        )
        container.with_exposed_ports(8080)
        with container, httpx.Client(timeout=3) as client:
            url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8080)}"
            deadline = time.monotonic() + 60
            while True:
                try:
                    response = client.get(url + "/healthz")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    self.fail(
                        "SearxNG did not become healthy: "
                        + container.get_wrapped_container()
                        .logs()
                        .decode(errors="replace")[-8000:]
                    )
                time.sleep(0.25)
            response = client.get(
                url + "/search", params={"q": "", "format": "json"}
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"], "No query")

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
