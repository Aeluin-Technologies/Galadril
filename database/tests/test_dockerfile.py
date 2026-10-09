"""Contract tests for the PostgreSQL runtime image."""

import unittest
from pathlib import Path

DOCKERFILE = Path(__file__).parent.parent / "Dockerfile"
ROLE_INITIALIZATION = (
    Path(__file__).parent.parent
    / "docker-entrypoint-initdb.d"
    / "004-create-galadril-app.sh"
)
EXTENSION_INITIALIZATION = (
    DOCKERFILE.parent / "docker-entrypoint-initdb.d/003-install-extensions.sh"
)


class DockerfileContractTest(unittest.TestCase):
    """Protects the network contract of the database image."""

    def test_database_publication_keeps_latest_and_commit_tags(self) -> None:
        tags = (DOCKERFILE.parent / "tags.txt").read_text().splitlines()
        self.assertEqual(len(tags), 2)
        self.assertEqual(tags[0], "latest")
        self.assertRegex(tags[1], r"^[0-9a-f]{40}$")

    def test_extension_errors_abort_database_initialization(self) -> None:
        """A healthy PostgreSQL process must not hide missing extensions."""
        initialization = EXTENSION_INITIALIZATION.read_text(encoding="utf-8")
        for line in initialization.splitlines():
            if line.startswith("psql "):
                self.assertIn("ON_ERROR_STOP=1", line)

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
