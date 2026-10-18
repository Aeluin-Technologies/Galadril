"""Contract tests for the PostgreSQL runtime image."""

import os
import subprocess
import tempfile
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

    def test_ldconfig_retries_only_emulator_segfaults(self) -> None:
        wrapper = DOCKERFILE.parent / "ldconfig-retry.sh"
        for failures, status, expected, attempts in [
            (2, 139, 0, 3),
            (9, 139, 139, 5),
            (1, 42, 42, 1),
            (0, 0, 0, 1),
        ]:
            with self.subTest(status=status, failures=failures):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    binary = root / "ldconfig.real.distrib"
                    binary.write_text(
                        "#!/bin/sh\ncount=0\n"
                        'if [ -f "$STATE" ]; then read -r count < "$STATE"; fi\n'
                        'count=$((count + 1))\nprintf "%s\\n" "$count" > "$STATE"\n'
                        'if [ "$count" -le "$FAILURES" ]; then exit "$STATUS"; fi\n'
                        'test "$1" = -X\n'
                    )
                    binary.chmod(0o755)
                    executable = root / "ldconfig.real"
                    executable.write_text(wrapper.read_text())
                    result = subprocess.run(
                        ["sh", str(executable), "-X"],
                        env=dict(
                            os.environ,
                            STATE=str(root / "state"),
                            FAILURES=str(failures),
                            STATUS=str(status),
                        ),
                        capture_output=True,
                        check=False,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr)
                    self.assertEqual(
                        int((root / "state").read_text()), attempts
                    )

    def test_ldconfig_workaround_is_removed_after_package_installation(
        self,
    ) -> None:
        dockerfile = DOCKERFILE.read_text()
        self.assertEqual(
            dockerfile.count(
                "dpkg-divert --local --rename --add /sbin/ldconfig.real"
            ),
            2,
        )
        self.assertEqual(
            dockerfile.count(
                "dpkg-divert --rename --remove /sbin/ldconfig.real"
            ),
            2,
        )

    def test_bm25_is_packaged_and_preloaded_for_native_retrieval(self) -> None:
        dockerfile = DOCKERFILE.read_text()
        self.assertIn("AS textsearch-builder", dockerfile)
        command = next(
            line for line in dockerfile.splitlines() if line.startswith("CMD ")
        )
        self.assertIn("pg_textsearch", command)
        self.assertIn(
            "CREATE EXTENSION IF NOT EXISTS pg_textsearch;",
            EXTENSION_INITIALIZATION.read_text(),
        )

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
