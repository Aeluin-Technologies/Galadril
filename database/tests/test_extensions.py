"""Exercises every initialized extension in the image produced by Bazel."""

from __future__ import annotations

import subprocess
import time
import unittest
from pathlib import Path

from testcontainers.core.container import DockerContainer

ROOT = Path(__file__).absolute().parents[2]
EXTENSIONS = {
    "timescaledb",
    "vector",
    "vectorscale",
    "age",
    "postgis",
    "plpython3u",
    "pg_stat_statements",
    "pg_wait_sampling",
    "pg_repack",
    "pg_trgm",
}


class DatabaseExtensionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        subprocess.run(
            ["/bin/bash", str(ROOT / "database/load.sh")],
            check=True,
            timeout=600,
        )
        cls.container = (
            DockerContainer("local/galadril-database:test")
            .with_env("POSTGRES_PASSWORD", "galadril-test")
            .with_env("PGPASSWORD", "galadril-test")
            .with_env("POSTGRES_DB", "galadril_test")
        )
        cls.addClassCleanup(cls.container.stop)
        cls.container.start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            code, _output = cls.container.exec(
                [
                    "pg_isready",
                    "--host=127.0.0.1",
                    "--username=postgres",
                    "--dbname=galadril_test",
                ]
            )
            if code == 0:
                return
            time.sleep(1)
        logs = (
            cls.container.get_wrapped_container()
            .logs()
            .decode(errors="replace")
        )
        raise RuntimeError(
            "Database initialization timed out:\n" + logs[-20000:]
        )

    def sql(self, statement: str, database: str = "galadril_test") -> str:
        code, output = self.container.exec(
            [
                "psql",
                "--host=127.0.0.1",
                "--username=postgres",
                "--dbname=" + database,
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--set=ON_ERROR_STOP=1",
                "--command",
                statement,
            ]
        )
        text = output.decode(errors="replace").strip()
        self.assertEqual(code, 0, text)
        return text

    def test_extensions_are_installed_in_both_databases(self) -> None:
        for database in ("postgres", "galadril_test"):
            with self.subTest(database=database):
                installed = set(
                    self.sql(
                        "SELECT extname FROM pg_extension", database
                    ).splitlines()
                )
                self.assertTrue(EXTENSIONS <= installed, EXTENSIONS - installed)
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM pg_extension WHERE extname='pg_cron'",
                "postgres",
            ),
            "1",
        )

    def test_timescaledb_creates_and_queries_a_hypertable(self) -> None:
        self.sql(
            "CREATE TABLE samples (at timestamptz NOT NULL, value integer); SELECT create_hypertable('samples', by_range('at')); INSERT INTO samples VALUES (now(), 42)"
        )
        self.assertEqual(self.sql("SELECT sum(value) FROM samples"), "42")
        self.assertEqual(
            self.sql(
                "SELECT count(*) FROM timescaledb_information.hypertables WHERE hypertable_name='samples'"
            ),
            "1",
        )

    def test_vector_and_vectorscale_execute_indexed_search(self) -> None:
        self.sql(
            "CREATE TABLE embeddings (id integer, embedding vector(3)); INSERT INTO embeddings SELECT n, ARRAY[n::real, 1, 0]::vector FROM generate_series(1, 100) n; CREATE INDEX embeddings_diskann ON embeddings USING diskann (embedding vector_cosine_ops)"
        )
        self.assertEqual(
            self.sql("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector"), "0"
        )
        self.assertEqual(
            self.sql(
                "SET enable_seqscan=off; SELECT id FROM embeddings ORDER BY embedding <=> '[1,1,0]'::vector LIMIT 1"
            ).splitlines()[-1],
            "1",
        )
        self.assertIn(
            "embeddings_diskann",
            self.sql(
                "SET enable_seqscan=off; EXPLAIN SELECT id FROM embeddings ORDER BY embedding <=> '[1,1,0]'::vector LIMIT 1"
            ),
        )

    def test_age_creates_and_queries_a_graph(self) -> None:
        self.sql(
            "LOAD 'age'; SET search_path=ag_catalog,public; SELECT create_graph('extension_test'); SELECT * FROM cypher('extension_test', $$ CREATE (:Entity {value: 42}) $$) AS (value agtype)"
        )
        self.assertEqual(
            self.sql(
                "LOAD 'age'; SET search_path=ag_catalog,public; SELECT * FROM cypher('extension_test', $$ MATCH (n:Entity) RETURN n.value $$) AS (value agtype)"
            ).splitlines()[-1],
            "42",
        )

    def test_postgis_executes_spatial_queries(self) -> None:
        self.assertEqual(
            self.sql("SELECT ST_Distance(ST_Point(0,0), ST_Point(3,4))"), "5"
        )

    def test_plpython_executes_a_function(self) -> None:
        self.sql(
            "CREATE FUNCTION python_answer() RETURNS integer AS $$return 42$$ LANGUAGE plpython3u"
        )
        self.assertEqual(self.sql("SELECT python_answer()"), "42")

    def test_pg_stat_statements_records_queries(self) -> None:
        self.sql("SELECT count(*) FROM pg_class")
        self.assertEqual(
            self.sql(
                "SELECT count(*) > 0 FROM pg_stat_statements WHERE query LIKE '%pg_class%'"
            ),
            "t",
        )

    def test_pg_wait_sampling_collects_wait_events(self) -> None:
        self.sql("SELECT pg_sleep(0.2)")
        self.assertEqual(
            self.sql(
                "SELECT count(*) > 0 FROM pg_wait_sampling_profile WHERE event='PgSleep'"
            ),
            "t",
        )

    def test_pg_repack_reorganizes_a_table(self) -> None:
        self.sql(
            "CREATE TABLE repack_test (id integer PRIMARY KEY); INSERT INTO repack_test SELECT generate_series(1, 100)"
        )
        code, output = self.container.exec(
            [
                "pg_repack",
                "--host=127.0.0.1",
                "--username=postgres",
                "--dbname=galadril_test",
                "--table=public.repack_test",
                "--no-order",
            ]
        )
        self.assertEqual(code, 0, output.decode(errors="replace"))
        self.assertEqual(self.sql("SELECT count(*) FROM repack_test"), "100")

    def test_pg_trgm_compares_strings(self) -> None:
        self.assertEqual(
            self.sql("SELECT similarity('galadril','galadril')"), "1"
        )

    def test_pg_cron_executes_a_job(self) -> None:
        self.sql(
            "CREATE TABLE cron_result (answer integer); SELECT cron.schedule('extension-test', '1 second', 'INSERT INTO cron_result VALUES (42)')",
            "postgres",
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.sql("SELECT count(*) FROM cron_result", "postgres") != "0":
                self.sql("SELECT cron.unschedule('extension-test')", "postgres")
                return
            time.sleep(1)
        self.fail("pg_cron did not execute the scheduled SQL job")

    def test_application_role_has_no_superuser_or_rls_bypass(self) -> None:
        self.assertEqual(
            self.sql(
                "SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='galadril_app'"
            ),
            "f",
        )


if __name__ == "__main__":
    unittest.main()
