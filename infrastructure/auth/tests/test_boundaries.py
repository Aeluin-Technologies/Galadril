"""Protects credential ownership and private authentication transport contracts."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).absolute().parents[3]


class AuthenticationBoundaryTest(unittest.TestCase):
    def test_kubernetes_studio_receives_only_explicit_secret_keys(self) -> None:
        documents = list(
            yaml.safe_load_all(
                (ROOT / "infrastructure/kubernetes/auth.yaml").read_text()
            )
        )
        deployment = next(
            document
            for document in documents
            if document.get("kind") == "Deployment"
        )
        studio = next(
            container
            for container in deployment["spec"]["template"]["spec"][
                "containers"
            ]
            if container["name"] == "studio"
        )
        self.assertNotIn("envFrom", studio)
        environment = {entry["name"]: entry for entry in studio["env"]}
        self.assertNotIn("AUTH_DATABASE_URL", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertEqual(
            environment["AUTH_GATEWAY_KEY"]["valueFrom"]["secretKeyRef"],
            {"name": "gateway-auth-client", "key": "AUTH_GATEWAY_KEY"},
        )
        self.assertEqual(
            environment["NUXT_AGGREGATE_METRICS_ENABLED"]["value"], "false"
        )

    def test_authentication_mesh_identity_can_call_only_the_private_graphql_operation(
        self,
    ) -> None:
        documents = list(
            yaml.safe_load_all(
                (ROOT / "infrastructure/kubernetes/security.yaml").read_text()
            )
        )
        policy = next(
            document
            for document in documents
            if document["metadata"]["name"] == "api-requests"
        )
        rules = [
            rule
            for rule in policy["spec"]["rules"]
            if any(
                "cluster.local/ns/galadril/sa/auth"
                in source["source"].get("principals", [])
                for source in rule.get("from", [])
            )
        ]
        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0]["to"],
            [
                {
                    "operation": {
                        "methods": ["POST"],
                        "paths": ["/internal/auth/graphql"],
                        "ports": ["8081"],
                    }
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
