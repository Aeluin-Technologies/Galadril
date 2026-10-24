"""Email deployment needs private durable state rather than a stateless worker."""

import json
import stat
import tempfile
import unittest
from pathlib import Path

import yaml
from galadril_plugins.utils.provision import initialize


class Deployment(unittest.TestCase):
    def test_initialization_creates_private_keys_without_overwriting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            initialize(root)
            key = (root / "service-key").read_text()
            self.assertGreaterEqual(len(key.strip()), 32)
            self.assertEqual(
                stat.S_IMODE((root / "service-key").stat().st_mode), 0o600
            )
            self.assertIn(
                "active", json.loads((root / "keyring.json").read_text())
            )
            with self.assertRaises(FileExistsError):
                initialize(root)
            self.assertEqual((root / "service-key").read_text(), key)

    def test_compose_worker_has_persistence_and_no_public_port(self) -> None:
        root = Path(__file__).resolve().parents[2]
        config = yaml.safe_load(
            (root / "infrastructure/docker/utils-email.yaml").read_text()
        )
        worker = config["services"]["utils-email"]
        self.assertEqual(worker["command"], ["utils", "email"])
        self.assertTrue(worker["read_only"])
        self.assertNotIn("ports", worker)
        self.assertEqual(worker["network_mode"], "service:api-proxy")
        state = next(
            item
            for item in worker["volumes"]
            if isinstance(item, dict)
            and item.get("target") == "/var/lib/galadril/utils"
        )
        self.assertFalse(state["bind"]["create_host_path"])


if __name__ == "__main__":
    unittest.main()
