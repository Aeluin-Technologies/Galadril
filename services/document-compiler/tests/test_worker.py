"""Real Linux engine, filesystem/network attacks, cancellation, and watchdog."""

import concurrent.futures
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from worker_fixture import offline_bundle

MANIFEST = Path(sys.argv.pop(1)).resolve()
ENGINE = Path(sys.argv.pop(1)).resolve()
PROBE = Path(sys.argv.pop(1)).resolve()
SUPERVISOR = Path(sys.argv.pop(1)).resolve()
NATIVE = Path(sys.argv.pop(1)).resolve().parent
DIAGNOSTIC = Path(sys.argv.pop(1)).resolve()


def rendered(source: bytes) -> bytes:
    return json.dumps({"entrypoint": list(source), "assets": []}).encode()


def receive(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        piece = connection.recv(count - len(result))
        if not piece:
            break
        result.extend(piece)
    return bytes(result)


class WorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory(
            prefix="document-native-", dir="/tmp"
        )
        cls.root = Path(cls.directory.name)
        cls.runtime = cls.root / "runtime"
        cls.runtime.mkdir()
        for directory in ("usr/lib", "lib"):
            source = NATIVE / directory
            if source.exists():
                shutil.copytree(source, cls.runtime / directory)
        shutil.copy2(ENGINE, cls.runtime / "compiler-worker")
        offline_bundle(MANIFEST, cls.runtime / "bundle.ttb")
        cls.probe_runtime = cls.root / "probe"
        shutil.copytree(cls.runtime, cls.probe_runtime, copy_function=os.link)
        (cls.probe_runtime / "compiler-worker").unlink()
        shutil.copy2(PROBE, cls.probe_runtime / "compiler-worker")
        cls.diagnostic = cls.root / "engine-diagnostic"
        shutil.copy2(DIAGNOSTIC, cls.diagnostic)
        cls.supervisor = cls.root / "compiler-supervisor"
        shutil.copy2(SUPERVISOR, cls.supervisor)
        (cls.root / "private.cls").write_text("synthetic other-job secret")
        # Match the deployed unprivileged identity after preparing public fixtures.
        if os.getuid() == 0:
            for path in (cls.root, *cls.root.rglob("*")):
                os.chown(path, 1000, 1000)
            os.setgroups([])
            os.setgid(1000)
            os.setuid(1000)
        if os.getuid() != 1000:
            raise RuntimeError("Native integration tests require UID 1000")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.directory.cleanup()

    def environment(self, runtime: Path, workspace: Path) -> dict[str, str]:
        return {
            "PATH": "/",
            "LANG": "C.UTF-8",
            "DOCUMENT_COMPILER_WORKER_ROOTFS": str(runtime),
            "DOCUMENT_COMPILER_WORKSPACE": str(workspace),
            "DOCUMENT_COMPILER_PARENT_PID": str(os.getpid()),
            "TMPDIR": str(workspace),
            "HOME": str(workspace),
            "LD_LIBRARY_PATH": f"{runtime}/usr/lib/x86_64-linux-gnu:{runtime}/lib/x86_64-linux-gnu",
        }

    def test_real_latex_class_and_macro_produce_pdf(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.root) as workspace:
            project = {
                "entrypoint": list(
                    rb"\documentclass{letter}\begin{document}\reference{Test} Hello\end{document}"
                ),
                "assets": [
                    {
                        "path": "letter.cls",
                        "data": list(
                            rb"\NeedsTeXFormat{LaTeX2e}\ProvidesClass{letter}\LoadClass{article}\newcommand{\reference}[1]{\textbf{#1}}"
                        ),
                    }
                ],
            }
            result = subprocess.run(
                [str(self.runtime / "compiler-worker")],
                env=self.environment(self.runtime, Path(workspace)),
                input=json.dumps(project).encode(),
                capture_output=True,
                timeout=70,
                check=False,
            )
            diagnostic = b""
            if result.returncode != 0:
                with tempfile.TemporaryDirectory(
                    dir=self.root
                ) as diagnostic_workspace:
                    detail = subprocess.run(
                        [str(self.diagnostic)],
                        env=self.environment(
                            self.runtime, Path(diagnostic_workspace)
                        ),
                        input=json.dumps(project).encode(),
                        capture_output=True,
                        timeout=70,
                        check=False,
                    )
                    diagnostic = detail.stderr + detail.stdout[-4096:]
            self.assertEqual(
                result.returncode,
                0,
                (result.stderr + diagnostic).decode(errors="replace"),
            )
            self.assertTrue(result.stdout.startswith(b"%PDF-"))

    def test_concurrent_compilations_are_isolated_and_repeatable(self) -> None:
        def compile_one(_: int) -> bytes:
            with tempfile.TemporaryDirectory(dir=self.root) as workspace:
                result = subprocess.run(
                    [str(self.runtime / "compiler-worker")],
                    env=self.environment(self.runtime, Path(workspace)),
                    input=rendered(
                        rb"\documentclass{article}\begin{document}Repeatable\end{document}"
                    ),
                    capture_output=True,
                    timeout=70,
                    check=False,
                )
                self.assertEqual(
                    result.returncode, 0, result.stderr.decode(errors="replace")
                )
                self.assertTrue(result.stdout.startswith(b"%PDF-"))
                return result.stdout

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outputs = tuple(executor.map(compile_one, range(2)))
        self.assertEqual(outputs[0], outputs[1])

    def test_landlock_denies_other_jobs_and_runtime_writes(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.root) as workspace:
            result = subprocess.run(
                [str(self.probe_runtime / "compiler-worker")],
                env=self.environment(self.probe_runtime, Path(workspace)),
                input=rendered(b"filesystem"),
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertTrue(result.stdout.startswith(b"%PDF-"))

    def test_network_syscall_kills_the_process(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.root) as workspace:
            result = subprocess.run(
                [str(self.probe_runtime / "compiler-worker")],
                env=self.environment(self.probe_runtime, Path(workspace)),
                input=rendered(b"network"),
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(
                result.returncode, -signal.SIGSYS, result.stderr.decode()
            )

    def start_supervisor(self, state: Path) -> subprocess.Popen[bytes]:
        state.mkdir()
        (state / "private.cls").write_text("synthetic other-job secret")
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(state),
            "DOCUMENT_COMPILER_WORKER_ROOTFS": str(self.probe_runtime),
            "DOCUMENT_COMPILER_WORKER_STATE": str(state),
            "DOCUMENT_COMPILER_WORKER_SOCKET": str(state / "worker.sock"),
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
            "OTEL_EXPORTER_OTLP_TIMEOUT": "1000",
            "AWS_SECRET_ACCESS_KEY": "synthetic-credential-not-for-child",
        }
        process = subprocess.Popen(
            [str(self.supervisor)],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 10
        while not (state / "worker.sock").exists():
            if process.poll() is not None:
                self.fail(process.communicate(timeout=3)[1].decode())
            if time.monotonic() > deadline:
                process.kill()
                self.fail(process.communicate(timeout=3)[1].decode())
            time.sleep(0.02)
        return process

    def connect(self, state: Path, source: bytes) -> socket.socket:
        connection = socket.socket(socket.AF_UNIX)
        connection.settimeout(70)
        connection.connect(str(state / "worker.sock"))
        payload = rendered(source)
        connection.sendall(struct.pack(">I", len(payload)) + payload)
        return connection

    def stop(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=3)
            self.fail("Supervisor did not terminate")

    def wait_for_pid(self, state: Path) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            for path in state.glob("*/pid"):
                return int(path.read_text())
            time.sleep(0.01)
        self.fail("Worker did not start")

    def test_supervisor_prunes_credentials_and_cancels_a_blocked_child(
        self,
    ) -> None:
        state = self.root / "cancellation"
        process = self.start_supervisor(state)
        try:
            with self.connect(state, b"filesystem") as connection:
                size = struct.unpack(">I", receive(connection, 4))[0]
                self.assertTrue(receive(connection, size).startswith(b"%PDF-"))
            connection = self.connect(state, b"sleep")
            pid = self.wait_for_pid(state)
            connection.close()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.02)
            else:
                self.fail("Cancelled worker remained alive")
        finally:
            self.stop(process)

    def test_wall_watchdog_kills_a_sleeping_child(self) -> None:
        state = self.root / "watchdog"
        process = self.start_supervisor(state)
        try:
            started = time.monotonic()
            with self.connect(state, b"sleep") as connection:
                pid = self.wait_for_pid(state)
                self.assertEqual(receive(connection, 4), b"")
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 55)
            self.assertLess(elapsed, 68)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            self.stop(process)


if __name__ == "__main__":
    unittest.main()
