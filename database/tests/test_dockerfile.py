"""Contract tests for the PostgreSQL runtime image."""

import unittest
from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"
ROLE_INITIALIZATION = (
    Path(__file__).parent.parent
    / "docker-entrypoint-initdb.d"
    / "004-create-galadril-app.sh"
)


class DockerfileContractTest(unittest.TestCase):
    """Protects the network contract of the database image."""

    def test_postgres_accepts_compose_network_connections(self) -> None:
        """Keeps dependent services able to reach PostgreSQL over the network."""
        dockerfile = DOCKERFILE.read_text(encoding="utf-8")
        self.assertIn('"listen_addresses=*"', dockerfile)

    def test_application_role_can_provision_the_configured_age_graph(
        self,
    ) -> None:
        """Keeps Vision bootstrap compatible with its non-superuser role."""
        initialization = ROLE_INITIALIZATION.read_text(encoding="utf-8")
        self.assertIn(
            'GRANT CREATE ON DATABASE :"target_database" TO galadril_app;',
            initialization,
        )
        self.assertIn(
            "GRANT USAGE ON SCHEMA ag_catalog TO galadril_app;",
            initialization,
        )
        self.assertIn(
            "GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO galadril_app;",
            initialization,
        )


if __name__ == "__main__":
    unittest.main()
