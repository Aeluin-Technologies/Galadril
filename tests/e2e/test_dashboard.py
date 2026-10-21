"""Runs Chromium against Nuxt and Ory with output paths owned by the Bazel runner."""

from __future__ import annotations

import io
import os
import sys
import tarfile
import unittest
from pathlib import Path

from infrastructure.auth.tests.test_sso import (
    ECHO,
    NODE,
    SsoIntegrationTest,
    archive,
)

PLAYWRIGHT = "mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27"
SUITE = Path(__file__).with_name("dashboard")
ACTORS = (
    "auth",
    "pengolo",
    "pengolo-interactive",
    "pengolo-models",
    "pengolo-history",
    "member",
    "people",
    "provider",
    "permissions",
    "mobile",
    "enable-passkey",
    "guided-permissions",
    "admin-errors",
    "people-refresh",
    "people-pagination",
    "session-renewal",
    "session-restoration",
)


class DashboardBrowserTest(SsoIntegrationTest):
    origin_host = "localhost"

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        for actor in ACTORS:
            user = "browser-" + actor
            identity = cls.private_request(
                "http://127.0.0.1:4434/admin/identities",
                {
                    "schema_id": "local",
                    "traits": {"email": user + "@demo.invalid"},
                    "metadata_admin": {
                        "galadril": {"tenant_id": "demo", "user_id": user}
                    },
                    "credentials": {
                        "password": {
                            "config": {
                                "password": "OnlyForBrowserIntegration-9347!"
                            }
                        }
                    },
                },
            )
            subject = str(identity["id"]).replace("'", "''")
            cls.sql(
                f"INSERT INTO iam_users(tenant_id,user_id) VALUES('demo','{user}');"
                "INSERT INTO iam_external_identities(tenant_id,connection_id,external_subject,user_id) "
                f"VALUES('demo','kratos','{subject}','{user}');"
            )
        for container in cls.containers:
            if container.attrs["Config"]["Image"] == ECHO:
                container.stop(timeout=5)
        backend = cls.create(
            NODE,
            command=["/tmp/gateway-fixture.mjs"],
            network_mode=f"container:{cls.proxy.id}",
        )
        backend.put_archive(
            "/tmp",
            archive(
                {
                    "gateway-fixture.mjs": (
                        SUITE / "gateway-fixture.mjs"
                    ).read_bytes()
                }
            ),
        )
        backend.start()

    def test_compiled_dashboard_in_chromium(self) -> None:
        self.docker.images.pull(PLAYWRIGHT)
        browser = self.create(
            PLAYWRIGHT,
            command=["sleep", "infinity"],
            network_mode="host",
            extra_hosts={"host.docker.internal": "host-gateway"},
            shm_size="1g",
            environment={
                "GALADRIL_TEST_ORIGIN": self.origin,
                "NODE_EXTRA_CA_CERTS": "/tmp/test-ca.pem",
                "CI": "true",
            },
        )
        browser.put_archive(
            "/tmp",
            archive(
                {
                    **{
                        "suite/" + path.name: path.read_bytes()
                        for path in SUITE.iterdir()
                        if path.is_file()
                    },
                    "test-ca.pem": self.files["identity/hydra/ca.pem"],
                }
            ),
        )
        browser.start()
        setup = browser.exec_run(
            [
                "bash",
                "-euc",
                "\n".join(
                    (
                        "command -v certutil >/dev/null || (apt-get update -qq && apt-get install -y -qq --no-install-recommends libnss3-tools)",
                        "mkdir -p /root/.pki/nssdb /tmp/results",
                        "certutil -N -d sql:/root/.pki/nssdb --empty-password",
                        "npm ci --ignore-scripts --no-audit --no-fund",
                        "npx --no-install tsc --noEmit",
                        "node certificate-check.mjs",
                        "certutil -A -d sql:/root/.pki/nssdb -n galadril-test -t 'C,,' -i /tmp/test-ca.pem",
                    )
                ),
            ],
            workdir="/tmp/suite",
        )
        self.assertEqual(setup.exit_code, 0, setup.output.decode())
        print(
            "Chromium rejected untrusted TLS before explicit fixture CA trust."
        )
        execution = self.docker.api.exec_create(
            browser.id,
            ["npx", "--no-install", "playwright", "test"]
            + (
                ["--grep", os.environ["GALADRIL_BROWSER_GREP"]]
                if "GALADRIL_BROWSER_GREP" in os.environ
                else []
            ),
            workdir="/tmp/suite",
        )
        # Stream progress so Bazel timeouts retain the last failing browser scenario.
        browser_output = bytearray()
        for chunk in self.docker.api.exec_start(execution["Id"], stream=True):
            browser_output.extend(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        status = self.docker.api.exec_inspect(execution["Id"])["ExitCode"]
        output = Path(
            os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", self.directory.name)
        )
        output.mkdir(parents=True, exist_ok=True)
        chunks, _metadata = browser.get_archive("/tmp/results")
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks))) as artifacts:
            artifacts.extractall(output, filter="data")
        xml_output = os.environ.get("XML_OUTPUT_FILE")
        if xml_output:
            Path(xml_output).write_bytes(
                (output / "results/junit.xml").read_bytes()
            )
        self.assertEqual(status, 0, browser_output.decode(errors="replace"))


def load_tests(
    _loader: unittest.TestLoader,
    _tests: unittest.TestSuite,
    _pattern: str | None,
) -> unittest.TestSuite:
    return unittest.TestSuite(
        [DashboardBrowserTest("test_compiled_dashboard_in_chromium")]
    )


if __name__ == "__main__":
    unittest.main()
