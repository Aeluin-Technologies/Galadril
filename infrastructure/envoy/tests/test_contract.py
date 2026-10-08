"""Checks that proxy configuration cannot create an authentication bypass."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import cast

import yaml

ROOT = Path(__file__).absolute().parents[1]


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("Expected a configuration mapping")
    return cast(dict[str, object], value)


class TrustBoundaryTest(unittest.TestCase):
    def test_kubernetes_exposes_only_the_registry_sidecar(self) -> None:
        documents = [
            mapping(item)
            for item in yaml.safe_load_all(
                (ROOT.parent / "kubernetes" / "registry.yaml").read_text()
            )
        ]
        deployment = next(
            item for item in documents if item["kind"] == "Deployment"
        )
        pod = mapping(mapping(mapping(deployment["spec"])["template"])["spec"])
        containers = cast(list[dict[str, object]], pod["containers"])
        app = next(item for item in containers if item["name"] == "registry")
        self.assertNotIn("ports", app)
        self.assertIn("envoy", {item["name"] for item in containers})
        for service in (
            item for item in documents if item["kind"] == "Service"
        ):
            ports = cast(
                list[dict[str, object]], mapping(service["spec"])["ports"]
            )
            self.assertEqual(next(iter(ports))["targetPort"], "grpc")
        self.assertIn("NetworkPolicy", {item["kind"] for item in documents})

    def test_each_proxy_exports_metrics_over_otlp(self) -> None:
        for name in ("gateway", "registry", "egress", "scribe"):
            config = mapping(
                yaml.safe_load((ROOT / f"{name}.yaml").read_text())
            )
            self.assertIn(
                "envoy.stat_sinks.open_telemetry",
                json.dumps(config["stats_sinks"]),
            )

    def test_public_listener_removes_identity_before_verifying_jwt(
        self,
    ) -> None:
        gateway = mapping(yaml.safe_load((ROOT / "gateway.yaml").read_text()))
        listeners = cast(
            list[dict[str, object]],
            mapping(gateway["static_resources"])["listeners"],
        )
        public = next(item for item in listeners if item["name"] == "public")
        chains = cast(list[dict[str, object]], public["filter_chains"])
        chain = next(iter(chains))
        self.assertIn("transport_socket", chain)
        filters = cast(list[dict[str, object]], chain["filters"])
        manager = mapping(next(iter(filters))["typed_config"])
        http_filters = cast(list[dict[str, object]], manager["http_filters"])
        names = [item["name"] for item in http_filters]
        self.assertLess(
            names.index("envoy.filters.http.header_mutation"),
            names.index("envoy.filters.http.jwt_authn"),
        )
        jwt = mapping(
            next(
                item
                for item in http_filters
                if item["name"] == "envoy.filters.http.jwt_authn"
            )["typed_config"]
        )
        provider = mapping(mapping(jwt["providers"])["identity"])
        self.assertTrue(provider["require_expiration"])
        self.assertEqual(provider["clock_skew_seconds"], 0)
        self.assertFalse(provider["forward"])
        self.assertNotIn("allow_missing", json.dumps(jwt))
        routes = json.dumps(manager["route_config"])
        self.assertIn('"path": "/graphql"', routes)
        self.assertNotIn("/internal/chat/tools", routes)

    def test_all_remote_api_clusters_verify_exact_workload_identity(
        self,
    ) -> None:
        for name in ("gateway", "registry", "egress", "scribe"):
            config = mapping(
                yaml.safe_load((ROOT / f"{name}.yaml").read_text())
            )
            resources = mapping(config["static_resources"])
            clusters = cast(list[dict[str, object]], resources["clusters"])
            for cluster in clusters:
                if cluster["name"] not in {
                    "registry",
                    "scribe",
                    "gateway_tools",
                }:
                    continue
                context = mapping(
                    mapping(cluster["transport_socket"])["typed_config"]
                )
                tls = mapping(context["common_tls_context"])
                self.assertIn("tls_certificates", tls)
                validation = mapping(tls["validation_context"])
                self.assertIn("trusted_ca", validation)
                matchers = cast(
                    list[dict[str, object]],
                    validation["match_typed_subject_alt_names"],
                )
                self.assertEqual(len(matchers), 1)
                self.assertTrue(
                    str(
                        mapping(next(iter(matchers))["matcher"])["exact"]
                    ).startswith("spiffe://galadril/")
                )
                self.assertIn("outlier_detection", cluster)

    def test_internal_servers_require_client_identity_and_deny_by_default(
        self,
    ) -> None:
        for name in ("gateway", "registry", "scribe"):
            config = mapping(
                yaml.safe_load((ROOT / f"{name}.yaml").read_text())
            )
            listeners = cast(
                list[dict[str, object]],
                mapping(config["static_resources"])["listeners"],
            )
            internal = next(
                item for item in listeners if item["name"] == "internal"
            )
            chains = cast(list[dict[str, object]], internal["filter_chains"])
            chain = next(iter(chains))
            context = mapping(
                mapping(chain["transport_socket"])["typed_config"]
            )
            self.assertTrue(context["require_client_certificate"])
            self.assertIn(
                "match_typed_subject_alt_names",
                mapping(
                    mapping(context["common_tls_context"])["validation_context"]
                ),
            )
            self.assertIn("envoy.filters.http.rbac", json.dumps(chain))

    def test_admin_and_egress_listeners_are_loopback_only(self) -> None:
        for name in ("gateway", "registry", "egress", "scribe"):
            config = mapping(
                yaml.safe_load((ROOT / f"{name}.yaml").read_text())
            )
            self.assertEqual(
                mapping(
                    mapping(mapping(config["admin"])["address"])[
                        "socket_address"
                    ]
                )["address"],
                "127.0.0.1",
            )
            listeners = cast(
                list[dict[str, object]],
                mapping(config["static_resources"])["listeners"],
            )
            for listener in listeners:
                if str(listener["name"]).startswith("egress"):
                    self.assertEqual(
                        mapping(mapping(listener["address"])["socket_address"])[
                            "address"
                        ],
                        "127.0.0.1",
                    )


if __name__ == "__main__":
    unittest.main()
