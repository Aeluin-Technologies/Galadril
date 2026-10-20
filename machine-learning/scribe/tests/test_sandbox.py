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
async def test_reviewed_scientific_modules_capture_prints_and_render_png_without_files() -> (
    None
):
    async with Sandbox() as sandbox:
        result = await sandbox.evaluate(
            "import numpy as np\nimport matplotlib.pyplot as plt\nx = np.linspace(0, 6, 100)\nplt.plot(x, np.sin(x), label='Sine')\nplt.title('A calculation')\nplt.legend()\nplt.savefig('curve.png')\nprint(np.mean([2, 4, 6]))"
        )
        assert "4.0" in result.text
        assert result.image is not None and result.image.startswith(
            b"\x89PNG\r\n\x1a\n"
        )
        for code in (
            "import numpy as np\nnp.load('/etc/passwd')",
            "import numpy as np\nnp.linspace(0, 1, 100000000)",
            "import matplotlib.pyplot as plt\nplt.savefig('/tmp/leak.png')",
            "import matplotlib.pyplot as plt\nplt.rcParams['text.usetex'] = True",
        ):
            with pytest.raises(SandboxFailure):
                await sandbox.evaluate(code)
        fresh = await sandbox.evaluate("print('fresh')")
        assert fresh.image is None and fresh.text.strip() == "fresh"


@pytest.mark.anyio
async def test_user_sinusoid_with_styled_axes_renders_in_monty() -> None:
    code = """import numpy as np
import matplotlib.pyplot as plt
x = np.linspace(0, 2*np.pi, 400)
y = np.sin(x)
fig, ax = plt.subplots(figsize=(8, 4.5))
ax.plot(x, y, color="#4C78A8", linewidth=2.5, label="sin(x)")
ax.axhline(0, color="gray", linewidth=0.8)
ax.set(title="Une courbe sinusoïdale", xlabel="x (radians)", ylabel="sin(x)")
ax.grid(True, alpha=0.25)
ax.legend()
fig.tight_layout()
plt.show()
"""
    async with Sandbox() as sandbox:
        result = await sandbox.evaluate(code)
        assert result.image is not None and result.image.startswith(
            b"\x89PNG\r\n\x1a\n"
        )
        for style in (
            "linewidth=1e100",
            "alpha=2",
            "color='x'*10000",
            "transform='host'",
        ):
            with pytest.raises(SandboxFailure):
                await sandbox.evaluate(
                    "import matplotlib.pyplot as plt\nplt.plot([0, 1], [1, 0], "
                    + style
                    + ")"
                )


@pytest.mark.anyio
async def test_matplotlib_figure_axes_workflow_returns_a_png_in_the_default_sandbox() -> (
    None
):
    async with Sandbox() as sandbox:
        result = await sandbox.evaluate(
            "import matplotlib.pyplot as plt\nfig, ax = plt.subplots(figsize=(6, 4))\nax.plot([1, 2, 3], [2, 4, 8], label='Evidence')\nax.set_title('Local chart')\nax.set_xlabel('Time')\nax.set_ylabel('Value')\nax.legend()\nax.grid(True)\nfig.tight_layout()\nfig.savefig('chart.png')\nplt.show()\nprint('chart ready')"
        )
        assert result.text.strip() == "chart ready"
        assert result.image is not None and result.image.startswith(
            b"\x89PNG\r\n\x1a\n"
        )
        for code in (
            "import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nfig.savefig('/tmp/escape.png')",
            "import matplotlib.pyplot as plt\nfig, ax = plt.subplots()\nax.figure.canvas",
        ):
            with pytest.raises(SandboxFailure):
                await sandbox.evaluate(code)


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
