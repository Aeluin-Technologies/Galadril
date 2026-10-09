"""Checks that proxy configuration cannot create an authentication bypass."""

from __future__ import annotations

import json
import re
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
    def test_local_proxy_keeps_internal_listeners_on_shared_loopback(
        self,
    ) -> None:
        config = mapping(yaml.safe_load((ROOT / "local.yaml").read_text()))
        listeners = cast(
            list[dict[str, object]],
            mapping(config["static_resources"])["listeners"],
        )
        ports = {
            str(item["name"]): mapping(
                mapping(item["address"])["socket_address"]
            )
            for item in listeners
        }
        self.assertEqual(ports["public"]["port_value"], 8080)
        for name, port in (
            ("local_registry", 50054),
            ("egress_scribe", 8092),
            ("egress_tools", 8082),
        ):
            self.assertEqual(
                ports[name], {"address": "127.0.0.1", "port_value": port}
            )
        self.assertNotIn(
            "envoy.transport_sockets.tls",
            json.dumps(mapping(config["static_resources"])["clusters"]),
        )

    def test_kubernetes_uses_ambient_and_shared_waypoint(self) -> None:
        root = ROOT.parent / "kubernetes"
        namespace = mapping(
            yaml.safe_load((root / "namespace.yaml").read_text())
        )
        self.assertEqual(
            mapping(mapping(namespace["metadata"])["labels"])[
                "istio.io/dataplane-mode"
            ],
            "ambient",
        )
        for name in ("gateway", "registry", "intake", "vision", "scribe"):
            documents = [
                mapping(item)
                for item in yaml.safe_load_all(
                    (root / f"{name}.yaml").read_text()
                )
            ]
            deployment = next(
                item for item in documents if item["kind"] == "Deployment"
            )
            pod = mapping(
                mapping(mapping(deployment["spec"])["template"])["spec"]
            )
            self.assertEqual(pod["serviceAccountName"], name)
            self.assertEqual(len(cast(list[object], pod["containers"])), 1)
        security = (root / "security.yaml").read_text()
        self.assertIn("mode: STRICT", security)
        self.assertIn("api-waypoint", security)
        self.assertIn("targetRefs:", security)
        self.assertIn("/galadril.registry.v1.Registry/GetPipeline", security)
        self.assertNotIn("paths: ['*']", security)
        ingress = (root / "ingress.yaml").read_text()
        self.assertIn("RequestAuthentication", ingress)
        self.assertIn("outputClaimToHeaders", ingress)
        self.assertIn("envoy.filters.http.header_mutation", ingress)
        self.assertIn("require_expiration: true", ingress)
        self.assertIn("certificateRefs", ingress)

    def test_ambient_preserves_native_jwt_and_rpc_contracts(self) -> None:
        root = ROOT.parent / "kubernetes"
        ingress = [
            mapping(item)
            for item in yaml.safe_load_all((root / "ingress.yaml").read_text())
        ]
        authentication = next(
            item for item in ingress if item["kind"] == "RequestAuthentication"
        )
        rule = cast(
            list[dict[str, object]], mapping(authentication["spec"])["jwtRules"]
        )[0]
        extension = next(
            item for item in ingress if item["kind"] == "EnvoyFilter"
        )
        patches = cast(
            list[dict[str, object]], mapping(extension["spec"])["configPatches"]
        )
        typed = mapping(
            mapping(mapping(patches[1]["patch"])["value"])["typed_config"]
        )
        provider = mapping(mapping(typed["providers"])["origins-0"])
        self.assertEqual(provider["issuer"], rule["issuer"])
        self.assertEqual(provider["audiences"], rule["audiences"])
        self.assertEqual(provider["payload_in_metadata"], "payload")
        self.assertTrue(provider["require_expiration"])
        self.assertEqual(provider["clock_skew_seconds"], 0)
        headers = cast(list[dict[str, str]], rule["outputClaimToHeaders"])
        self.assertEqual(
            provider["claim_to_headers"],
            [
                {"header_name": item["header"], "claim_name": item["claim"]}
                for item in headers
            ],
        )
        security = [
            mapping(item)
            for item in yaml.safe_load_all((root / "security.yaml").read_text())
        ]
        policy = next(
            item
            for item in security
            if mapping(item["metadata"])["name"] == "api-requests"
        )
        rules = cast(list[dict[str, object]], mapping(policy["spec"])["rules"])
        native = (
            (ROOT / "registry.yaml")
            .read_text()
            .split("                  gateway:\n", 1)[1]
        )
        gateway, runtime = native.split("                  runtime:\n", 1)
        for source, mesh_rule in (
            (gateway, rules[2]),
            (runtime.split("          - name:", 1)[0], rules[3]),
        ):
            expected = set(re.findall(r"exact: (/galadril[^\n]+)", source))
            operations = cast(list[dict[str, object]], mesh_rule["to"])
            operation = mapping(operations[0]["operation"])
            self.assertEqual(
                set(operation["paths"]) - {"/grpc.health.v1.Health/Check"},
                expected,
            )
            self.assertEqual(operation["ports"], ["50053"])
        values = mapping(
            yaml.safe_load((root / "istio/istiod.yaml").read_text())
        )
        providers = cast(
            list[dict[str, object]],
            mapping(values["meshConfig"])["extensionProviders"],
        )
        logging = mapping(
            next(item for item in providers if item["name"] == "otlp-logs")[
                "envoyOtelAls"
            ]
        )
        format = mapping(logging["logFormat"])
        self.assertEqual(format["text"], "galadril-api")
        self.assertNotIn("AUTHORIZATION", json.dumps(format).upper())
        self.assertNotIn(":PATH", json.dumps(format).upper())

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
