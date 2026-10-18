"""Exercise the actual OCI sandbox, offline installation and disposal on Linux."""

import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from galadril_sandbox.contracts import ExecutionRequest
from galadril_sandbox.controller import (
    Controller,
    ControllerSettings,
    negotiate_engine,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_offline_scientific_execution_and_disposable_isolation(
    tmp_path: Path,
) -> None:
    runfiles = Path(os.environ["TEST_SRCDIR"]) / os.environ["TEST_WORKSPACE"]
    image = (
        runfiles
        / "machine-learning/scribe/python/galadril_sandbox/worker_load/tarball.tar"
    )
    transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")

    async def chunks() -> AsyncIterator[bytes]:
        with image.open("rb") as source:
            while block := source.read(1048576):
                yield block

    async with httpx.AsyncClient(
        transport=transport, base_url="http://docker/v1.47/", timeout=120
    ) as engine:
        await negotiate_engine(engine)
        loaded = await engine.post("images/load", content=chunks())
        assert loaded.is_success, loaded.text
        loaded.raise_for_status()
        controller = Controller(
            ControllerSettings(
                worker_image="local/galadril-python-worker:test",
                deployment="development",
                runtime="runc",
            ),
            engine,
        )
        result = await controller.execute(
            ExecutionRequest(
                code="import pandas as pd\nimport numpy as np\nimport matplotlib.pyplot as plt\ndf=pd.DataFrame({'x':np.arange(5), 'y':np.arange(5)**2})\nplt.plot(df.x,df.y)\nprint(int(df.y.sum()))",
                libraries=("pandas", "numpy", "matplotlib"),
            )
        )
        if result.status != "completed":
            print(result.text)
        assert result.status == "completed" and result.text.strip() == "30", (
            result
        )
        assert result.image is not None
        assert {library.name for library in result.libraries} == {
            "pandas",
            "numpy",
            "matplotlib",
        }
        marker = tmp_path / "host-only-secret"
        marker.write_text("must never be mounted")
        fresh = await controller.execute(
            ExecutionRequest(
                code=f"import os, importlib.util, socket\nfrom pathlib import Path\nassert importlib.util.find_spec('pandas') is None\nassert not Path({str(marker)!r}).exists()\nassert not Path('/var/run/docker.sock').exists()\nassert os.getenv('PYTHON_SANDBOX_API_KEY') is None\ntry:\n socket.create_connection(('1.1.1.1',80),timeout=0.2)\n raise AssertionError('network escaped')\nexcept OSError:\n pass\nprint('isolated')"
            )
        )
        assert fresh.status == "completed" and fresh.text.strip() == "isolated"
        timed_out = await controller.execute(
            ExecutionRequest(code="while True: pass")
        )
        assert timed_out.status == "failed"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
