"""Exercises the shipped E2E proxy mounts against the actual Docker runtime."""

import ssl
import tempfile
import unittest
from pathlib import Path

import httpx
from environment import ComposeEnvironment, _run

from infrastructure.envoy.testing import identity_files


class ProxyStartupTest(unittest.IsolatedAsyncioTestCase):
    """Keeps proxy startup independent of the expensive application image build."""

    async def test_readonly_configuration_supports_proxy_startup(
        self,
    ) -> None:
        """Catches runc mount failures before starting Registry and SpiceDB."""
        environment = ComposeEnvironment()
        try:
            await environment.prepare_configuration()
            mounts = tuple(
                argument
                for category in (
                    "application",
                    "observability",
                    "database",
                    "proxy",
                )
                for argument in (
                    "--volume",
                    f"{environment._config_volume}-{category}:/config/{category}:ro",
                )
            )
            await _run(
                (
                    "docker",
                    "run",
                    "--rm",
                    *mounts,
                    "busybox:1.37.0-musl",
                    "sh",
                    "-ec",
                    "test -s /config/application/connectors.yaml; "
                    "test -s /config/observability/otel-collector.yaml; "
                    "test -x /config/database/003-install-extensions.sh; "
                    "test -s /config/proxy/envoy.yaml; "
                    "test -s /config/proxy/jwks.json; "
                    "test -s /config/proxy/identity/key.pem; "
                    "test -s /config/proxy/public/key.pem; "
                    'test -z "$(find /config/application /config/observability '
                    '/config/database -name key.pem)"',
                )
            )
            with tempfile.TemporaryDirectory(prefix="proxy-startup-") as path:
                override = Path(path) / "ports.yaml"
                override.write_text(
                    "services:\n  api-proxy:\n    ports: !override\n"
                    "      - 127.0.0.1::8080\n"
                )
                command = (*environment._command, "--file", str(override))
                await _run(
                    (
                        *command,
                        "up",
                        "--detach",
                        "--wait",
                        "--no-deps",
                        "api-proxy",
                    ),
                    environment=environment._environment,
                )
                address = await _run(
                    (*command, "port", "api-proxy", "8080"),
                    environment=environment._environment,
                )
                trust = ssl.create_default_context()
                trust.load_verify_locations(
                    cadata=identity_files()["identity/gateway/ca.pem"].decode()
                )
                async with httpx.AsyncClient(
                    verify=trust, timeout=5.0
                ) as client:
                    response = await client.post(
                        f"https://{address.strip()}/graphql",
                        json={"query": "{ __typename }"},
                    )
            self.assertEqual(response.status_code, 401)
        finally:
            await environment.close()


if __name__ == "__main__":
    unittest.main()
