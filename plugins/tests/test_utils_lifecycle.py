"""Uvicorn must preserve the plugin worker's cooperative signal ownership."""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener


class Lifecycle(unittest.TestCase):
    def test_sigterm_releases_http_server_and_database_without_timeout(
        self,
    ) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.dict(
                os.environ,
                {
                    "http_proxy": "http://127.0.0.1:1",
                    "https_proxy": "http://127.0.0.1:1",
                    "no_proxy": "",
                    "NO_PROXY": "",
                },
            ),
        ):
            directory = Path(temporary)
            (directory / "key").write_text("k" * 48)
            (directory / "keys").write_text(
                json.dumps(
                    {
                        "active": "v1",
                        "keys": {
                            "v1": "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="
                        },
                    }
                )
            )
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            environment = {
                **{
                    name: value
                    for name, value in os.environ.items()
                    if not name.startswith("UTILS_")
                },
                "PYTHONPATH": os.pathsep.join(sys.path),
                "UTILS_DATABASE_PATH": str(directory / "mail.db"),
                "UTILS_KEYRING_FILE": str(directory / "keys"),
                "UTILS_SERVICE_KEY_FILE": str(directory / "key"),
                "UTILS_SCRIBE_URL": "http://scribe",
                "UTILS_GATEWAY_URL": "http://gateway",
                "UTILS_HOST": "127.0.0.1",
                "UTILS_PORT": str(port),
            }
            code = "import asyncio,os,signal\nfrom galadril_plugins.utils.service import Worker\nasync def run():\n stop=asyncio.Event()\n asyncio.get_running_loop().add_signal_handler(signal.SIGTERM,stop.set)\n await Worker(os.environ).run(stop)\nasyncio.run(run())"
            process = subprocess.Popen(
                [sys.executable, "-c", code],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                # Cold imports on CI have a separate budget from graceful shutdown.
                deadline = time.monotonic() + 30
                probe = build_opener(ProxyHandler({}))
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        _, errors = process.communicate(timeout=5)
                        self.fail(
                            f"Email service exited before readiness ({process.returncode}):\n"
                            + errors.decode(errors="replace")
                        )
                    try:
                        with probe.open(
                            f"http://127.0.0.1:{port}/healthz", timeout=0.2
                        ) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.05)
                else:
                    process.kill()
                    _, errors = process.communicate(timeout=5)
                    self.fail(
                        "Email service did not become ready:\n"
                        + errors.decode(errors="replace")
                    )
                process.send_signal(signal.SIGTERM)
                try:
                    process.communicate(timeout=4)
                except subprocess.TimeoutExpired:
                    self.fail("Mail server replaced the worker signal handler")
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
