"""Keep the public tunnel on a bounded Envoy ingress, away from PISTE secrets."""

import re
import unittest
from pathlib import Path

import yaml


class MCPDeploymentTest(unittest.TestCase):
    def test_tunnel_only_reaches_envoy_and_secrets_only_reach_the_service(
        self,
    ) -> None:
        root = Path(__file__).absolute().parents[2]
        compose = yaml.safe_load(
            (root / "infrastructure/docker/law-mcp.yaml").read_text()
        )
        services = compose["services"]
        for name in ("law-mcp", "law-mcp-envoy", "law-mcp-tunnel"):
            self.assertNotIn("ports", services[name])
            self.assertTrue(services[name]["read_only"])
        tunnel = services["law-mcp-tunnel"]
        self.assertEqual(tunnel["profiles"], ["mcp-public"])
        self.assertNotIn("LAW_PISTE_API_KEY", tunnel.get("environment", {}))
        self.assertIn("law-mcp-envoy", tunnel["depends_on"])
        for volume in services["law-mcp"]["volumes"]:
            self.assertTrue(volume["read_only"])
            self.assertFalse(volume["bind"]["create_host_path"])
        proxy = yaml.safe_load(
            (root / "infrastructure/envoy/law-mcp.yaml").read_text()
        )
        listener = proxy["static_resources"]["listeners"][0]
        self.assertEqual(
            listener["address"]["socket_address"]["address"], "127.0.0.1"
        )
        routes = listener["filter_chains"][0]["filters"][0]["typed_config"][
            "route_config"
        ]["virtual_hosts"][0]["routes"]
        pattern = routes[1]["match"]["safe_regex"]["regex"]
        self.assertIsNotNone(re.fullmatch(pattern, "/law/mcp"))
        self.assertIsNotNone(re.fullmatch(pattern, "/another/mcp"))
        self.assertIsNone(re.fullmatch(pattern, "/admin"))
        self.assertNotIn("prefix_rewrite", routes[1]["route"])
        self.assertEqual(routes[-1]["direct_response"]["status"], 404)


if __name__ == "__main__":
    unittest.main()
