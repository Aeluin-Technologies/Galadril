"""Keep the public tunnel on a bounded Envoy ingress, away from PISTE secrets."""

import json
import re
import unittest
from pathlib import Path

import yaml


class MCPDeploymentTest(unittest.TestCase):
    def test_workspace_mcp_starts_without_legal_provider_credentials(
        self,
    ) -> None:
        root = Path(__file__).absolute().parents[2]
        base = yaml.safe_load(
            (root / "infrastructure/docker/mcp.yaml").read_text()
        )
        service = base["services"]["mcp"]
        self.assertEqual(service["network_mode"], "service:api-proxy")
        self.assertTrue(service["read_only"])
        self.assertNotIn("ports", service)
        self.assertEqual(
            service["environment"]["LAW_PISTE_API_KEY"],
            "${LAW_PISTE_API_KEY:-}",
        )
        self.assertTrue(
            all("piste" not in str(volume) for volume in service["volumes"])
        )
        self.assertIn(
            "core.mcp.workspace", service["environment"]["MCP_NAMESPACES"]
        )
        namespaces = json.loads(service["environment"]["MCP_NAMESPACES"])
        root_namespace = next(
            item for item in namespaces if item["path"] == "/"
        )
        self.assertEqual(root_namespace["provider"], "unified")
        compose = yaml.safe_load(
            (root / "infrastructure/docker/docker-compose.yaml").read_text()
        )
        self.assertIn({"path": "mcp.yaml"}, compose["include"])

    def test_tunnel_only_reaches_envoy_and_secrets_only_reach_the_service(
        self,
    ) -> None:
        root = Path(__file__).absolute().parents[2]
        compose = yaml.safe_load(
            (root / "infrastructure/docker/law-mcp.yaml").read_text()
        )
        self.assertEqual(set(compose["services"]), {"mcp"})
        services = yaml.safe_load(
            (root / "infrastructure/docker/mcp.yaml").read_text()
        )["services"]
        for name in ("mcp",):
            self.assertNotIn("ports", services[name])
            self.assertTrue(services[name]["read_only"])
        for volume in [
            *services["mcp"]["volumes"],
            *compose["services"]["mcp"]["volumes"],
        ]:
            self.assertTrue(volume["read_only"])
            self.assertFalse(volume["bind"]["create_host_path"])
        proxy = yaml.safe_load(
            (root / "infrastructure/envoy/local.yaml").read_text()
        )
        listener = next(
            item
            for item in proxy["static_resources"]["listeners"]
            if item["name"] == "cloudflare_mcp"
        )
        self.assertEqual(
            listener["address"]["socket_address"]["address"], "127.0.0.1"
        )
        host = listener["filter_chains"][0]["filters"][0]["typed_config"][
            "route_config"
        ]["virtual_hosts"][0]
        self.assertEqual(host["domains"], ["mcp.gravitalia.com"])
        self.assertIn("cookie", host["request_headers_to_remove"])
        routes = listener["filter_chains"][0]["filters"][0]["typed_config"][
            "route_config"
        ]["virtual_hosts"][0]["routes"]
        paths = {route["match"].get("path") for route in routes}
        self.assertIn("/", paths)
        self.assertIn("/.well-known/oauth-protected-resource", paths)
        legacy = next(
            route for route in routes if "safe_regex" in route["match"]
        )
        pattern = legacy["match"]["safe_regex"]["regex"]
        self.assertIsNotNone(re.fullmatch(pattern, "/law/mcp"))
        self.assertIsNotNone(re.fullmatch(pattern, "/another/mcp"))
        self.assertIsNone(re.fullmatch(pattern, "/admin"))
        self.assertNotIn("prefix_rewrite", legacy["route"])
        self.assertEqual(routes[-1]["direct_response"]["status"], 404)


if __name__ == "__main__":
    unittest.main()
