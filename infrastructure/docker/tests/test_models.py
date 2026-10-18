"""Checks native Compose merging for shipped and E2E deployment parity."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

ROOT = Path(__file__).absolute().parents[3]


def model(
    path: str, *, profiles: tuple[str, ...] = ()
) -> dict[str, dict[str, object]]:
    result = subprocess.run(
        [
            str(ROOT / "infrastructure/docker/docker-compose"),
            *(
                argument
                for profile in profiles
                for argument in ("--profile", profile)
            ),
            "--file",
            str(ROOT / path),
            "config",
            "--format",
            "json",
        ],
        env=os.environ
        | {
            "E2E_CONFIG_VOLUME": "galadril-model-test",
            "SCRIBE_ENABLED": "false",
            "SEARXNG_SECRET": "model-search-only-01234567890123456789",
            "GALADRIL_AUTH_PASSWORD": "model-test-only",
            "HYDRA_DATABASE_PASSWORD": "model-test-only",
            "POLIS_DATABASE_PASSWORD": "model-test-only",
            "HYDRA_SYSTEM_SECRET": "model-test-only-01234567890123456789",
            "POLIS_ENCRYPTION_KEY": "model-test-only-012345678901234567",
            "POLIS_API_KEY": "model-test-only",
            "POLIS_CLIENT_SECRET_VERIFIER": "model-test-only",
            "AUTH_CLIENT_SECRET": "model-test-only",
            "AUTH_SESSION_SECRET": "model-test-only-01234567890123456789",
            "AUTH_GATEWAY_KEY": "model-private-gateway-key-01234567890123456789",
            "SSO_PROVISIONING_KEY": "model-provisioning-only-01234567890123456789",
        },
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError(result.stderr)
    return cast(
        dict[str, dict[str, object]], json.loads(result.stdout)["services"]
    )


class ComposeModelTest(unittest.TestCase):
    def test_local_search_has_no_public_listener_or_paid_provider_fallback(
        self,
    ) -> None:
        services = model(
            "infrastructure/docker/docker-compose.yaml",
            profiles=("local-search",),
        )
        self.assertNotIn("ports", services["searxng"])
        self.assertIn("@sha256:", str(services["searxng"]["image"]))
        gateway = cast(dict[str, str], services["gateway"]["environment"])
        scribe = cast(dict[str, str], services["scribe"]["environment"])
        self.assertEqual(gateway["PENGOLO_WEB_SEARCH_PROVIDER"], "searxng")
        self.assertEqual(scribe["SCRIBE_WEB_SEARCH_PROVIDER"], "gateway")

    def test_development_environment_is_private_complete_and_repeatable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            command = [
                "bash",
                str(ROOT / "infrastructure/docker/dev-environment.sh"),
                str(environment),
            ]
            subprocess.run(command, check=True, capture_output=True, timeout=30)
            initial = environment.read_bytes()
            self.assertEqual(environment.stat().st_mode & 0o777, 0o600)
            subprocess.run(command, check=True, capture_output=True, timeout=30)
            self.assertEqual(environment.read_bytes(), initial)
            result = subprocess.run(
                [
                    str(ROOT / "infrastructure/docker/docker-compose"),
                    "--env-file",
                    str(environment),
                    "--file",
                    str(ROOT / "infrastructure/docker/docker-compose.yaml"),
                    "--profile",
                    "local-auth",
                    "config",
                    "--format",
                    "json",
                ],
                env={"PATH": os.environ["PATH"]},
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            deployment = json.loads(result.stdout)["services"]
            self.assertEqual(
                deployment["studio"]["environment"]["AUTH_LOCAL_LOGIN_ENABLED"],
                "true",
            )
            self.assertIn("kratos", deployment)
            secrets = deployment["postgres"]["environment"]
            passwords = [
                secrets[name]
                for name in (
                    "POLIS_DATABASE_PASSWORD",
                    "HYDRA_DATABASE_PASSWORD",
                    "KRATOS_DATABASE_PASSWORD",
                    "GALADRIL_AUTH_PASSWORD",
                )
            ]
            self.assertTrue(all(len(value) == 64 for value in passwords))
            self.assertEqual(len(set(passwords)), len(passwords))

    def test_development_environment_preserves_existing_configuration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / ".env"
            existing = "POLIS_DATABASE_PASSWORD=existing-secret\nHYDRA_DATABASE_PASSWORD=\nCUSTOM_VALUE=keep-me\n"
            environment.write_text(existing)
            subprocess.run(
                [
                    "bash",
                    str(ROOT / "infrastructure/docker/dev-environment.sh"),
                    str(environment),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            source = environment.read_text()
            self.assertIn("POLIS_DATABASE_PASSWORD=existing-secret\n", source)
            self.assertIn("CUSTOM_VALUE=keep-me\n", source)
            self.assertNotIn("HYDRA_DATABASE_PASSWORD=\n", source)

    def test_only_gateway_receives_authentication_database_credentials(
        self,
    ) -> None:
        services = model("infrastructure/docker/docker-compose.yaml")
        studio = services["studio"]["environment"]
        gateway = services["gateway"]["environment"]
        self.assertNotIn("AUTH_DATABASE_URL", studio)
        self.assertIn("AUTH_DATABASE_URL", gateway)
        self.assertEqual(
            studio["AUTH_GATEWAY_KEY"], gateway["AUTH_GATEWAY_KEY"]
        )
        self.assertEqual(
            studio["AUTH_GATEWAY_URL"],
            "http://127.0.0.1:8081/internal/auth/graphql",
        )

    def test_sso_administration_uses_private_broker_and_separate_provisioning_key(
        self,
    ) -> None:
        services = model("infrastructure/docker/docker-compose.yaml")
        gateway = services["gateway"]["environment"]
        studio = services["studio"]["environment"]
        self.assertEqual(gateway["SSO_POLIS_URL"], "http://127.0.0.1:5225")
        self.assertEqual(
            gateway["SSO_POLIS_API_KEY"], studio["AUTH_POLIS_API_KEY"]
        )
        self.assertGreaterEqual(len(gateway["SSO_PROVISIONING_KEY"]), 32)

    def test_e2e_uses_the_shipped_tempo_image_and_restart_policy(self) -> None:
        shipped = model("infrastructure/docker/docker-compose.yaml")["tempo"]
        fixture = model("tests/e2e/environment/compose.yaml")["tempo"]
        self.assertEqual(fixture["image"], shipped["image"])
        self.assertEqual(fixture.get("restart"), shipped["restart"])

    def test_production_topology_has_explicit_names_and_one_local_ray(
        self,
    ) -> None:
        shipped = model("infrastructure/docker/docker-compose.yaml")
        for name, service in shipped.items():
            with self.subTest(service=name):
                self.assertTrue(service.get("container_name"))
        self.assertNotIn("ray-head", shipped)
        self.assertNotIn("ray-gpu-worker", shipped)
        self.assertEqual(
            sorted(name for name in shipped if name.startswith("vision")),
            ["vision"],
        )
        self.assertEqual(shipped["vision"]["command"][-2:], ["--role", "all"])

    def test_e2e_gateway_enables_the_chatbot_runtime(self) -> None:
        """Prevents inherited production defaults from masking fixture YAML."""
        fixture = model("tests/e2e/environment/compose.yaml")
        environment = cast(dict[str, str], fixture["gateway"]["environment"])
        self.assertEqual(environment["SCRIBE__ENABLED"], "true")

    def test_proxy_settings_and_application_isolation_are_shared(self) -> None:
        shipped = model("infrastructure/docker/docker-compose.yaml")
        fixture = model("tests/e2e/environment/compose.yaml")
        self.assertEqual(
            fixture["postgres"]["command"], shipped["postgres"]["command"]
        )
        for deployment in (shipped, fixture):
            self.assertEqual(
                [name for name in deployment if name.endswith("-proxy")],
                ["api-proxy"],
            )
        for name in ("gateway", "registry", "intake", "vision", "scribe"):
            with self.subTest(service=name):
                shipped_dependencies = cast(
                    dict[str, object], shipped[name].get("depends_on", {})
                )
                fixture_dependencies = cast(
                    dict[str, object], fixture[name].get("depends_on", {})
                )
                for dependency, requirement in shipped_dependencies.items():
                    self.assertEqual(
                        fixture_dependencies[dependency], requirement
                    )
                self.assertNotIn("ports", fixture[name])
                self.assertEqual(
                    fixture[name]["network_mode"], shipped[name]["network_mode"]
                )
                self.assertEqual(fixture[name]["init"], shipped[name]["init"])
                for key in (
                    "image",
                    "command",
                    "user",
                    "read_only",
                    "cap_drop",
                    "security_opt",
                    "init",
                    "stop_grace_period",
                ):
                    self.assertEqual(
                        fixture["api-proxy"][key],
                        shipped["api-proxy"][key],
                    )
                volumes = cast(
                    list[dict[str, object]], fixture["api-proxy"]["volumes"]
                )
                self.assertEqual(len(volumes), 1)
                self.assertEqual(volumes[0]["target"], "/etc/envoy")
                self.assertEqual(volumes[0]["source"], "e2e-proxy")
                self.assertTrue(volumes[0]["read_only"])
        for service in fixture.values():
            for volume in cast(
                list[dict[str, object]], service.get("volumes", [])
            ):
                self.assertNotIn("subpath", volume.get("volume", {}))
        self.assertEqual(
            cast(dict[str, object], fixture["gateway"]["depends_on"])[
                "spicedb-schema"
            ],
            cast(dict[str, object], shipped["gateway"]["depends_on"])[
                "spicedb-schema"
            ],
        )


if __name__ == "__main__":
    unittest.main()
