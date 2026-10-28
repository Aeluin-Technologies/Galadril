"""The generated development proxy must be accepted by the real pinned Envoy binary."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

import yaml

from infrastructure.dev.galadril_dev.bootstrap import Bootstrap

ROOT = Path(__file__).resolve().parents[2]


class EnvoyBootstrapTest(unittest.TestCase):
    def test_generated_configuration_validates_with_real_envoy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in (
                "infrastructure/docker/.env.example",
                "infrastructure/docker/connectors.yaml",
                "infrastructure/envoy/local.yaml",
            ):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            bootstrap = Bootstrap(root)
            asyncio.run(bootstrap.initialize())
            bundle = root / "proxy"
            shutil.copytree(
                bootstrap.state / "identity/registry", bundle / "identity"
            )
            shutil.copytree(
                bootstrap.state / "identity/gateway", bundle / "public"
            )
            shutil.copyfile(
                bootstrap.state / "envoy.yaml", bundle / "envoy.yaml"
            )
            shutil.copyfile(
                ROOT / "infrastructure/envoy/error-page.lua",
                bundle / "error-page.lua",
            )
            image = yaml.safe_load(
                (ROOT / "infrastructure/docker/proxy.yaml").read_text()
            )["services"]["api-proxy"]["image"]
            name = "galadril-proxy-bootstrap-" + uuid.uuid4().hex
            subprocess.run(
                [
                    "docker",
                    "create",
                    "--name",
                    name,
                    "--network",
                    "none",
                    "--cap-drop",
                    "ALL",
                    "--entrypoint",
                    "/usr/local/bin/envoy",
                    image,
                    "--mode",
                    "validate",
                    "-c",
                    "/etc/envoy/envoy.yaml",
                ],
                check=True,
                capture_output=True,
                timeout=90,
            )
            try:
                subprocess.run(
                    ["docker", "cp", str(bundle) + "/.", name + ":/etc/envoy"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                result = subprocess.run(
                    ["docker", "start", "--attach", name],
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stderr.decode(errors="replace")[:1200],
                )
            finally:
                subprocess.run(
                    ["docker", "rm", "--force", name],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )


if __name__ == "__main__":
    unittest.main()
