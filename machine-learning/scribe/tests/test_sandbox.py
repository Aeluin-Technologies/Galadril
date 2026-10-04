"""Runs adversarial snippets against the real subprocess sandbox."""

import asyncio
import sys

import pytest
from galadril_scribe.sandbox import Sandbox, SandboxFailure


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
