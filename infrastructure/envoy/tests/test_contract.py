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
    def test_scribe_detached_routes_keep_gateway_workload_identity(
        self,
    ) -> None:
        scribe = (ROOT / "scribe.yaml").read_text()
        self.assertIn("path: /generations", scribe)
        self.assertIn("spiffe://galadril/gateway", scribe)
        self.assertIn("^/generations/[a-f0-9]{32}/cancel$", scribe)

    def test_ambient_catalog_keeps_gateway_identity_and_exact_method(
        self,
    ) -> None:
        policies = [
            mapping(item)
            for item in yaml.safe_load_all(
                (ROOT.parent / "kubernetes/security.yaml").read_text()
            )
        ]
        operations = [
            (rule["from"], target["operation"])
            for policy in policies
            if policy["kind"] == "AuthorizationPolicy"
            for rule in policy["spec"].get("rules", [])
            for target in rule.get("to", [])
            if "/models" in target.get("operation", {}).get("paths", [])
        ]
        self.assertEqual(len(operations), 1)
        sources, operation = operations[0]
        self.assertEqual(
            sources,
            [
                {
                    "source": {
                        "principals": ["cluster.local/ns/galadril/sa/gateway"]
                    }
                }
            ],
        )
        self.assertEqual(
            operation,
            {"methods": ["GET"], "paths": ["/models"], "ports": ["8091"]},
        )

    def test_model_catalog_is_routed_only_on_the_private_scribe_path(
        self,
    ) -> None:
        for name, listener_name in (
            ("local", "egress_scribe"),
            ("gateway", "egress_scribe"),
            ("scribe", "internal"),
        ):
            config = mapping(
                yaml.safe_load((ROOT / f"{name}.yaml").read_text())
            )
            listeners = mapping(config["static_resources"])["listeners"]
            listener = next(
                item for item in listeners if item["name"] == listener_name
            )
            manager = listener["filter_chains"][0]["filters"][0]["typed_config"]
            routes = manager["route_config"]["virtual_hosts"][0]["routes"]
            self.assertIn(
                "/models", [route["match"].get("path") for route in routes]
            )
            if name == "scribe":
                rbac = next(
                    item
                    for item in manager["http_filters"]
                    if item["name"] == "envoy.filters.http.rbac"
                )
                policies = rbac["typed_config"]["rules"]["policies"]
                catalog = policies["models"]
                self.assertIn(
                    "spiffe://galadril/gateway",
                    json.dumps(catalog["principals"]),
                )
                self.assertIn(
                    '"exact": "GET"', json.dumps(catalog["permissions"])
                )
                self.assertIn(
                    '"exact": "/models"', json.dumps(catalog["permissions"])
                )

    def test_development_certificate_signing_explicitly_uses_sha256(
        self,
    ) -> None:
        source = (ROOT / "dev-certificates.sh").read_text()
        commands = source.replace("\\\n", " ").splitlines()
        signing = [
            command
            for command in commands
            if "openssl req -x509" in command or "openssl x509 -req" in command
        ]
        self.assertEqual(len(signing), 2)
        for command in signing:
            self.assertIn("-sha256", command)

    def test_mfa_claim_headers_cannot_be_supplied_by_callers(self) -> None:
        for name in (
            "local.yaml",
            "gateway.yaml",
            "registry.yaml",
            "scribe.yaml",
        ):
            source = (ROOT / name).read_text()
            for header in ("x-galadril-acr", "x-galadril-mfa-generation"):
                self.assertIn(f"remove: {header}", source, name)

    def test_kubernetes_sso_telemetry_does_not_retain_login_payloads(
        self,
    ) -> None:
        documents = yaml.safe_load_all(
            (ROOT.parent / "kubernetes/auth.yaml").read_text()
        )
        deployment = next(
            item for item in documents if item["kind"] == "Deployment"
        )
        containers = deployment["spec"]["template"]["spec"]["containers"]
        hydra = next(item for item in containers if item["name"] == "hydra")
        environment = {item["name"]: item["value"] for item in hydra["env"]}
        self.assertEqual(
            environment.get("TRACING_PROVIDERS_OTLP_SERVER_URL"),
            "otel-collector.observability.svc.cluster.local:4318",
        )
        self.assertEqual(
            environment.get("TRACING_PROVIDERS_OTLP_INSECURE"), "true"
        )
        polis = next(item for item in containers if item["name"] == "polis")
        environment = {item["name"]: item["value"] for item in polis["env"]}
        self.assertEqual(environment.get("SSO_TRACES_DISABLE"), "true")

    def test_browser_graphql_stays_on_the_cookie_origin(self) -> None:
        config = (
            ROOT.parents[1] / "front/dashboard/apollo/default.ts"
        ).read_text()
        self.assertIn('httpEndpoint: "/graphql"', config)
        self.assertIn('wsEndpoint: "/graphql"', config)
        self.assertNotIn('tokenName: "token"', config)

    def test_sso_jwks_are_fetched_from_a_pinned_tls_upstream(self) -> None:
        config = mapping(yaml.safe_load((ROOT / "local.yaml").read_text()))
        resources = mapping(config["static_resources"])
        listeners = cast(list[dict[str, object]], resources["listeners"])
        public = mapping(
            mapping(
                cast(list[dict[str, object]], listeners[0]["filter_chains"])[0]
            )["filters"][0]
        )["typed_config"]
        filters = cast(list[dict[str, object]], mapping(public)["http_filters"])
        jwt = next(
            item
            for item in filters
            if item["name"] == "envoy.filters.http.jwt_authn"
        )
        identity = mapping(
            mapping(mapping(jwt["typed_config"])["providers"])["identity"]
        )
        self.assertNotIn("local_jwks", identity)
        remote = mapping(identity["remote_jwks"])
        self.assertNotIn("async_fetch", remote)
        self.assertEqual(mapping(remote["http_uri"])["cluster"], "hydra")
        self.assertEqual(identity["from_cookies"], ["__Host-galadril-token"])
        clusters = cast(list[dict[str, object]], resources["clusters"])
        application = next(
            item for item in clusters if item["name"] == "application"
        )
        self.assertEqual(
            application["health_checks"][0]["no_traffic_interval"], "3s"
        )
        hydra = next(item for item in clusters if item["name"] == "hydra")
        tls = mapping(mapping(hydra["transport_socket"])["typed_config"])
        self.assertEqual(tls["sni"], "hydra-proxy")
        self.assertIn(
            "trusted_ca",
            mapping(mapping(tls["common_tls_context"])["validation_context"]),
        )

    def test_vault_synchronizes_only_workload_secrets_and_restarts_consumers(
        self,
    ) -> None:
        root = ROOT.parent / "kubernetes/vault"
        documents = [
            mapping(item)
            for item in yaml.safe_load_all((root / "secrets.yaml").read_text())
        ]
        secrets = {
            mapping(mapping(item["spec"])["destination"])["name"]: mapping(
                item["spec"]
            )
            for item in documents
            if item["kind"] == "VaultStaticSecret"
        }
        self.assertEqual(
            set(secrets),
            {
                "gateway-connectors",
                "registry-connectors",
                "intake-connectors",
                "vision-connectors",
                "gateway-runtime",
                "registry-runtime",
                "scribe-runtime",
                "spicedb-bootstrap",
            },
        )
        for name, spec in secrets.items():
            with self.subTest(secret=name):
                self.assertEqual(spec["vaultAuthRef"], "galadril")
                self.assertEqual(spec["type"], "kv-v2")
                self.assertEqual(spec["path"], f"galadril/{name}")
                self.assertTrue(spec["rolloutRestartTargets"])
                destination = mapping(spec["destination"])
                self.assertTrue(
                    mapping(destination["transformation"])["excludeRaw"]
                )

    def test_vault_provisions_ingress_tls_with_kubernetes_authentication(
        self,
    ) -> None:
        root = ROOT.parent / "kubernetes/vault"
        documents = [
            mapping(item)
            for item in yaml.safe_load_all((root / "secrets.yaml").read_text())
        ]
        authentication = next(
            item for item in documents if item["kind"] == "VaultAuth"
        )
        spec = mapping(authentication["spec"])
        self.assertEqual(spec["method"], "kubernetes")
        kubernetes = mapping(spec["kubernetes"])
        self.assertEqual(kubernetes["serviceAccount"], "vault-secrets")
        self.assertEqual(kubernetes["audiences"], ["vault"])
        connection = next(
            item for item in documents if item["kind"] == "VaultConnection"
        )
        self.assertTrue(
            str(mapping(connection["spec"])["address"]).startswith("https://")
        )
        self.assertFalse(mapping(connection["spec"])["skipTLSVerify"])
        certificate = next(
            item for item in documents if item["kind"] == "VaultPKISecret"
        )
        spec = mapping(certificate["spec"])
        self.assertEqual(spec["vaultAuthRef"], "galadril")
        self.assertIn("expiryOffset", spec)
        destination = mapping(spec["destination"])
        self.assertEqual(destination["name"], "galadril-public-tls")
        self.assertEqual(destination["type"], "kubernetes.io/tls")
        self.assertTrue(destination["create"])

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
        clusters = cast(
            list[dict[str, object]],
            mapping(config["static_resources"])["clusters"],
        )
        for cluster in clusters:
            if cluster["name"] != "hydra":
                self.assertNotIn("transport_socket", cluster)

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
        self.assertEqual(
            rule["jwksUri"],
            "http://auth.galadril.svc.cluster.local:4444/.well-known/jwks.json",
        )
        istiod = mapping(
            yaml.safe_load((root / "istio/istiod.yaml").read_text())
        )
        self.assertEqual(
            mapping(mapping(istiod["pilot"])["env"])[
                "PILOT_JWT_ENABLE_REMOTE_JWKS"
            ],
            "envoy",
        )
        extension = next(
            item for item in ingress if item["kind"] == "EnvoyFilter"
        )
        patches = cast(
            list[dict[str, object]], mapping(extension["spec"])["configPatches"]
        )
        typed = mapping(
            mapping(
                mapping(
                    next(
                        item["patch"]
                        for item in patches
                        if mapping(item["patch"])["operation"] == "MERGE"
                    )
                )["value"]
            )["typed_config"]
        )
        provider = mapping(mapping(typed["providers"])["origins-0"])
        self.assertEqual(provider["issuer"], rule["issuer"])
        self.assertEqual(provider["audiences"], rule["audiences"])
        self.assertEqual(provider["payload_in_metadata"], "payload")
        self.assertTrue(provider["require_expiration"])
        self.assertEqual(provider["clock_skew_seconds"], 1)
        expiration = next(
            item
            for item in patches
            if mapping(mapping(item["patch"])["value"]).get("name")
            == "galadril.jwt_expiration"
        )
        self.assertEqual(
            mapping(expiration["patch"])["operation"], "INSERT_AFTER"
        )
        self.assertEqual(
            mapping(
                mapping(
                    mapping(mapping(expiration["match"])["listener"])[
                        "filterChain"
                    ]
                )["filter"]
            )["subFilter"],
            {"name": "envoy.filters.http.jwt_authn"},
        )
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
        registry_rules = [
            item
            for item in rules
            if any(
                mapping(operation["operation"]).get("ports") == ["50053"]
                for operation in cast(list[dict[str, object]], item["to"])
            )
        ]
        self.assertEqual(len(registry_rules), 2)
        for source, principal in (
            (gateway, "cluster.local/ns/galadril/sa/gateway"),
            (
                runtime.split("          - name:", 1)[0],
                "cluster.local/ns/galadril/sa/intake",
            ),
        ):
            mesh_rule = next(
                item
                for item in registry_rules
                if any(
                    principal in mapping(origin["source"]).get("principals", [])
                    for origin in cast(list[dict[str, object]], item["from"])
                )
            )
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
        self.assertLess(
            names.index("envoy.filters.http.jwt_authn"),
            names.index("galadril.jwt_expiration"),
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
        self.assertEqual(provider["clock_skew_seconds"], 1)
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
