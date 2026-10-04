"""Runs adversarial snippets against the real subprocess sandbox."""

import asyncio
import sys
from importlib.metadata import PathDistribution
from pathlib import Path

import pytest
from galadril_scribe.sandbox import Sandbox, SandboxFailure


def test_worker_resolution_does_not_require_installer_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from galadril_scribe.sandbox import runtime_binary

    prefix = tmp_path / "runtime"
    metadata = (
        prefix
        / "lib/python3.13/site-packages/pydantic_monty_runtime-1.0.0.dist-info"
    )
    metadata.mkdir(parents=True)
    worker = (
        prefix / "bin" / ("monty.exe" if sys.platform == "win32" else "monty")
    )
    worker.parent.mkdir()
    worker.write_bytes(b"wheel-worker")
    runtime = PathDistribution(metadata)
    assert runtime.files is None
    monkeypatch.setattr(
        "galadril_scribe.sandbox.distribution", lambda _: runtime
    )
    assert Path(runtime_binary()).resolve() == worker.resolve()
    worker.unlink()
    with pytest.raises(SandboxFailure, match="Sandbox runtime unavailable"):
        runtime_binary()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_calculation_and_session_isolation() -> None:
    async with Sandbox() as sandbox:
        assert (
            await sandbox.execute("sum(i * i for i in range(100))") == "328350"
        )
        assert await sandbox.execute("secret = 42\nsecret") == "42"
        with pytest.raises(SandboxFailure):
            await sandbox.execute("secret")


@pytest.mark.anyio
async def test_sandbox_denies_host_files_credentials_and_network() -> None:
    async with Sandbox() as sandbox:
        for snippet in (
            "open('/etc/passwd').read()",
            "import os\nos.environ",
            "import urllib.request\nurllib.request.urlopen('http://localhost')",
        ):
            with pytest.raises(
                SandboxFailure, match="Sandbox execution failed"
            ):
                await sandbox.execute(snippet)


@pytest.mark.anyio
async def test_sandbox_bounds_execution_memory_input_and_output() -> None:
    async with Sandbox() as sandbox:
        for snippet in (
            "while True:\n    pass",
            "[0] * 100000000",
            "'x' * 65537",
            "1" * 8193,
        ):
            with pytest.raises(SandboxFailure):
                await asyncio.wait_for(sandbox.execute(snippet), timeout=5)
        assert await sandbox.execute("1 + 1") == "2"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
