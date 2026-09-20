"""Contract tests for the PostgreSQL runtime image."""

import unittest
from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"


class DockerfileContractTest(unittest.TestCase):
    """Protects the network contract of the database image."""

    def test_postgres_accepts_compose_network_connections(self) -> None:
        """Keeps dependent services able to reach PostgreSQL over the network."""
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn('"listen_addresses=*"', dockerfile)


if __name__ == "__main__":
    unittest.main()
