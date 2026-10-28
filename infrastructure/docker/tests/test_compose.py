"""Docker deployment contracts for Registry-owned artifact persistence."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).parent.parent
OBSERVABILITY_ROOT = ROOT.parent / "observability"


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a configuration mapping")
    return cast(dict[str, object], value)


def services(filename: str) -> dict[str, object]:
    return mapping(
        mapping(yaml.safe_load((ROOT / filename).read_text()))["services"]
    )


class ComposeContractTest(unittest.TestCase):
    def test_scribe_has_bounded_memory_and_cpu_thread_pools(self) -> None:
        scribe = mapping(services("dashboard.yaml")["scribe"])
        self.assertEqual(scribe["mem_limit"], "${SCRIBE_MEMORY_LIMIT:-5g}")
        self.assertEqual(scribe["memswap_limit"], "${SCRIBE_MEMORY_LIMIT:-5g}")
        self.assertEqual(scribe["cpus"], "${SCRIBE_CPUS:-3}")
        environment = mapping(scribe["environment"])
        self.assertEqual(
            environment["OMP_NUM_THREADS"], "${SCRIBE_INFERENCE_THREADS:-2}"
        )
        self.assertEqual(
            environment["GALADRIL_ONNX_INTRA_OP_THREADS"],
            "${SCRIBE_INFERENCE_THREADS:-2}",
        )

    def test_document_relations_use_the_configurable_economical_model(
        self,
    ) -> None:
        environment = mapping(
            mapping(services("dashboard.yaml")["scribe"])["environment"]
        )
        self.assertEqual(
            environment.get("SCRIBE_DOCUMENT_REMOTE_RELATIONS"),
            "${SCRIBE_DOCUMENT_REMOTE_RELATIONS:-true}",
        )
        self.assertEqual(
            environment.get("SCRIBE_DOCUMENT_RELATION_MODEL"),
            "${SCRIBE_DOCUMENT_RELATION_MODEL:-gpt-5.6-terra}",
        )

    def test_nuxt_dev_installs_at_the_workspace_root_before_starting_dashboard(
        self,
    ) -> None:
        studio = mapping(services("nuxt-dev.yaml")["studio"])
        self.assertEqual(studio["working_dir"], "/workspace")
        command = cast(list[str], studio["command"])
        self.assertIn("pnpm install --frozen-lockfile &&", command[-1])
        self.assertIn(
            "pnpm --dir dashboard dev --host 127.0.0.1 --port 3000",
            command[-1],
        )
        self.assertIn("../../front:/workspace", studio["volumes"])

    def test_sso_admin_endpoints_are_private_and_secrets_are_required(
        self,
    ) -> None:
        auth = services("auth.yaml")
        for name in ("hydra", "polis"):
            service = mapping(auth[name])
            self.assertNotIn("ports", service)
            self.assertEqual(service["network_mode"], "service:api-proxy")
        hydra = mapping(mapping(auth["hydra"])["environment"])
        self.assertEqual(hydra["STRATEGIES_ACCESS_TOKEN"], "jwt")
        self.assertIn("?", str(hydra["SECRETS_SYSTEM"]))

    def test_sso_telemetry_uses_native_otlp_without_retaining_login_payloads(
        self,
    ) -> None:
        auth = services("auth.yaml")
        hydra = mapping(mapping(auth["hydra"])["environment"])
        self.assertEqual(hydra["TRACING_PROVIDER"], "otel")
        self.assertEqual(
            hydra.get("TRACING_PROVIDERS_OTLP_SERVER_URL"),
            "otel-collector:4318",
        )
        self.assertEqual(hydra.get("TRACING_PROVIDERS_OTLP_INSECURE"), "true")
        polis = mapping(mapping(auth["polis"])["environment"])
        self.assertEqual(polis.get("SSO_TRACES_DISABLE"), "true")

    def test_proxy_requires_existing_identity_and_configuration_files(
        self,
    ) -> None:
        proxy = mapping(services("proxy.yaml")["api-proxy"])
        mounts = [
            mapping(item) for item in cast(list[object], proxy["volumes"])
        ]
        self.assertEqual(
            {mount["target"] for mount in mounts},
            {
                "/etc/envoy/envoy.yaml",
                "/etc/envoy/error-page.lua",
                "/etc/envoy/identity/ca.pem",
                "/etc/envoy/identity/cert.pem",
                "/etc/envoy/identity/key.pem",
                "/etc/envoy/public/cert.pem",
                "/etc/envoy/public/key.pem",
            },
        )
        for mount in mounts:
            with self.subTest(target=mount["target"]):
                self.assertEqual(mount["type"], "bind")
                self.assertTrue(mount["read_only"])
                self.assertFalse(mapping(mount["bind"])["create_host_path"])

    def test_postgres_readiness_requires_the_final_tcp_server(self) -> None:
        postgres = mapping(services("database.yaml")["postgres"])
        self.assertTrue(postgres["init"])
        self.assertIn("listen_addresses=*", postgres["command"])
        self.assertIn(
            "shared_preload_libraries=timescaledb,age,pg_cron,pg_stat_statements,pg_wait_sampling,pg_textsearch",
            postgres["command"],
        )
        self.assertIn(
            "--host=127.0.0.1",
            " ".join(mapping(postgres["healthcheck"])["test"]),
        )

    def test_s3_events_are_not_delayed_by_batching(self) -> None:
        storage = mapping(services("s3.yaml")["rustfs"])
        self.assertEqual(
            mapping(storage["environment"])[
                "RUSTFS_NOTIFY_WEBHOOK_ENDPOINT_PRIMARY"
            ],
            "http://s3-notifications:4195/events",
        )
        relay = mapping(services("s3.yaml")["s3-notifications"])
        self.assertNotIn("ports", relay)
        self.assertEqual(
            mapping(storage["environment"])["RUSTFS_OUTBOUND_ALLOW_ORIGINS"],
            "http://s3-notifications:4195",
        )
        config = mapping(
            yaml.safe_load((ROOT / "s3-notifications.yaml").read_text())
        )
        output = mapping(mapping(config["output"])["kafka_franz"])
        self.assertEqual(output["seed_brokers"], ["redpanda:9092"])
        self.assertEqual(output["topic"], "s3-notification")
        self.assertEqual(
            mapping(storage["environment"])[
                "RUSTFS_NOTIFY_WEBHOOK_QUEUE_DIR_PRIMARY"
            ],
            "/data/.rustfs.sys/notification-queue",
        )

    def test_local_images_select_the_host_architecture(self) -> None:
        for filename, name in (
            ("streaming.yaml", "registry"),
            ("streaming.yaml", "intake"),
            ("dashboard.yaml", "gateway"),
        ):
            self.assertNotIn("platform", mapping(services(filename)[name]))

    def test_vision_uses_the_stable_embedded_ray_resolver(self) -> None:
        environment = mapping(
            mapping(services("streaming.yaml")["vision"])["environment"]
        )
        self.assertEqual(environment["GRPC_DNS_RESOLVER"], "native")
        self.assertEqual(environment["RAY_raylet_start_wait_time_s"], "60")

    def test_gateway_waits_for_the_canonical_authorization_schema(self) -> None:
        dependencies = mapping(
            mapping(services("dashboard.yaml")["gateway"])["depends_on"]
        )
        self.assertEqual(
            mapping(dependencies["spicedb-schema"])["condition"],
            "service_completed_successfully",
        )

    def test_studio_defaults_use_encrypted_api_connections(self) -> None:
        source = (
            ROOT.parent.parent / "front/dashboard/apollo/default.ts"
        ).read_text()
        self.assertIn('httpEndpoint: "/graphql"', source)
        self.assertIn('wsEndpoint: "/graphql"', source)
        self.assertIn('credentials: "same-origin"', source)
        self.assertNotIn('"http://localhost:8080/graphql"', source)

    def test_application_apis_share_only_their_proxy_namespace(self) -> None:
        for filename, service in (
            ("dashboard.yaml", "gateway"),
            ("streaming.yaml", "registry"),
            ("streaming.yaml", "intake"),
            ("streaming.yaml", "vision"),
        ):
            application = mapping(services(filename)[service])
            self.assertEqual(application["network_mode"], "service:api-proxy")
            self.assertNotIn("ports", application)
        gateway = mapping(services("dashboard.yaml")["gateway"])
        self.assertFalse(
            any(
                "KEY_PEM" in key or key.startswith("JWT_")
                for key in mapping(gateway["environment"])
            )
        )

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
            "http://rustfs:9000",
        )
        self.assertEqual(
            lakefs["image"], "${LAKEFS_IMAGE:-treeverse/lakefs:1.88.0}"
        )

    def test_tempo_configuration_matches_version_three(self) -> None:
        """Rejects configuration blocks removed by the pinned Tempo image."""
        tempo = mapping(
            yaml.safe_load(
                (OBSERVABILITY_ROOT / "tempo.yaml").read_text(encoding="utf-8")
            )
        )
        self.assertNotIn("ingester", tempo)
        self.assertNotIn("compactor", tempo)
        overrides = mapping(tempo["overrides"])
        defaults = mapping(overrides["defaults"])
        self.assertEqual(
            mapping(defaults["compaction"])["block_retention"], "24h"
        )

    def test_rustfs_uses_pinned_available_images(self) -> None:
        """Prevents deployments from depending on removed or mutable images."""
        minio = mapping(services("s3.yaml")["rustfs"])
        minio_init = mapping(services("s3.yaml")["rustfs-init"])
        self.assertEqual(
            minio["image"],
            "${RUSTFS_IMAGE:-rustfs/rustfs:1.0.1@"
            "sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c}",
        )
        self.assertEqual(
            minio_init["image"],
            "${MINIO_CLIENT_IMAGE:-bitnamilegacy/minio-client:2025.7.21-debian-12-r3@"
            "sha256:73bd39f7899a0cef12b8dd5df13aa93a3ed1aaa44236542442e9ac76819ac158}",
        )
        self.assertEqual(
            minio["command"],
            ["rustfs", "/data"],
        )
        self.assertIn(
            "rustfs-data:/data",
            mapping(minio)["volumes"],
        )

    def test_vision_uses_one_explicit_tenant_pipeline(self) -> None:
        vision = mapping(services("streaming.yaml")["vision"])
        command = vision["command"]
        self.assertIsInstance(command, list)
        self.assertNotIn("--pipeline-config", command)
        environment = mapping(vision["environment"])
        self.assertIn("VISION_TENANT_ID", environment)
        self.assertIn("VISION_PIPELINE_ID", environment)

    def test_vision_reserves_shared_memory_for_ray(self) -> None:
        """Keeps Ray's object store out of the container filesystem."""
        vision = mapping(services("streaming.yaml")["vision"])
        self.assertEqual(vision["shm_size"], "${VISION_SHM_SIZE:-6gb}")

    def test_services_mount_one_trusted_connector_file(self) -> None:
        for filename, service in (
            ("streaming.yaml", "intake"),
            ("streaming.yaml", "vision"),
            ("dashboard.yaml", "gateway"),
        ):
            volumes = mapping(services(filename)[service])["volumes"]
            self.assertIn(
                "${GALADRIL_CONNECTORS_PATH:-./connectors.yaml}:/connectors.yaml:ro",
                volumes,
            )


if __name__ == "__main__":
    unittest.main()
