"""Production secrets and public certificates belong to cluster operators, not local keys."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
KUBECTL = Path(sys.argv.pop(1)).resolve()


class KubernetesBootstrapTest(unittest.TestCase):
    def test_rendered_application_routes_remain_https_only(self) -> None:
        rendered = subprocess.run(
            [
                str(KUBECTL),
                "kustomize",
                "--load-restrictor",
                "LoadRestrictionsNone",
                str(ROOT / "infrastructure/kubernetes-overlays/production"),
            ],
            capture_output=True,
            check=True,
            timeout=60,
        )
        resources = list(yaml.safe_load_all(rendered.stdout))
        routes = [item for item in resources if item["kind"] == "HTTPRoute"]
        self.assertTrue(routes)
        for route in routes:
            for parent in route["spec"]["parentRefs"]:
                if parent["name"] == "public-gateway":
                    self.assertEqual(
                        parent.get("sectionName"),
                        "https",
                        route["metadata"]["name"],
                    )

    def test_public_tls_uses_acme_and_http_gateway_solver(self) -> None:
        resources = list(
            yaml.safe_load_all(
                (
                    ROOT
                    / "infrastructure/kubernetes-overlays/production/certificates.yaml"
                ).read_text()
            )
        )
        issuer = next(
            item for item in resources if item["kind"] == "ClusterIssuer"
        )
        self.assertEqual(
            issuer["spec"]["acme"]["server"],
            "https://acme-v02.api.letsencrypt.org/directory",
        )
        solver = issuer["spec"]["acme"]["solvers"][0]["http01"][
            "gatewayHTTPRoute"
        ]
        self.assertEqual(solver["parentRefs"][0]["name"], "public-gateway")
        certificate = next(
            item for item in resources if item["kind"] == "Certificate"
        )
        self.assertEqual(
            certificate["spec"]["secretName"], "galadril-public-tls"
        )
        self.assertEqual(
            certificate["spec"]["issuerRef"]["kind"], "ClusterIssuer"
        )
        self.assertFalse(any(item["kind"] == "Secret" for item in resources))

    def test_compiler_credentials_come_from_scoped_vault_role(self) -> None:
        resources = list(
            yaml.safe_load_all(
                (
                    ROOT
                    / "infrastructure/kubernetes-overlays/production/compiler-vault.yaml"
                ).read_text()
            )
        )
        auth = next(item for item in resources if item["kind"] == "VaultAuth")
        self.assertEqual(auth["metadata"]["namespace"], "document-compilation")
        self.assertEqual(
            auth["spec"]["kubernetes"]["role"], "galadril-document-compiler"
        )
        connection = next(
            item for item in resources if item["kind"] == "VaultConnection"
        )
        self.assertFalse(connection["spec"]["skipTLSVerify"])
        secret = next(
            item for item in resources if item["kind"] == "VaultStaticSecret"
        )
        self.assertEqual(
            secret["spec"]["path"], "galadril/document-compiler-storage"
        )
        self.assertEqual(
            secret["spec"]["destination"]["name"], "document-compiler-storage"
        )
        self.assertEqual(
            secret["spec"]["rolloutRestartTargets"][0]["name"],
            "document-compiler",
        )

    def test_bundle_initializer_has_no_credentials_or_api_token(self) -> None:
        resources = list(
            yaml.safe_load_all(
                (
                    ROOT
                    / "infrastructure/kubernetes-overlays/document-compiler/sandbox.yaml"
                ).read_text()
            )
        )
        deployment = next(
            item for item in resources if item["kind"] == "Deployment"
        )
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        initializer = next(
            item
            for item in pod["initContainers"]
            if item["name"] == "tex-bundle"
        )
        self.assertNotIn("envFrom", initializer)
        self.assertNotIn("env", initializer)
        self.assertEqual(
            initializer["securityContext"]["capabilities"], {"drop": ["ALL"]}
        )
        self.assertTrue(
            initializer["securityContext"]["readOnlyRootFilesystem"]
        )
        self.assertEqual(
            [mount["name"] for mount in initializer["volumeMounts"]],
            ["bundle", "bundle-bootstrap"],
        )
        self.assertEqual(len(pod["containers"]), 1)


if __name__ == "__main__":
    unittest.main()
