"""Checks native Compose merging for shipped and E2E deployment parity."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from typing import cast

ROOT = Path(__file__).absolute().parents[3]


def model(path: str) -> dict[str, dict[str, object]]:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--file",
            str(ROOT / path),
            "config",
            "--format",
            "json",
        ],
        env=os.environ | {"E2E_CONFIG_VOLUME": "galadril-model-test"},
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
                identity = next(
                    item
                    for item in volumes
                    if item["target"] == "/etc/envoy/public"
                )
                self.assertEqual(
                    cast(dict[str, object], identity["volume"])["subpath"],
                    "identity/gateway",
                )
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
