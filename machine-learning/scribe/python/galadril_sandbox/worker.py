"""A fresh offline virtual environment lives only inside one isolated OCI worker."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal

from packaging.requirements import Requirement
from packaging.version import Version
from pydantic import BaseModel, ConfigDict, Field

from galadril_sandbox.contracts import (
    ExecutionRequest,
    ExecutionResult,
    InstalledLibrary,
    Library,
)


class Wheel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    filename: str = Field(pattern=r"^[A-Za-z0-9_.+-]+\.whl$")
    version: str = Field(min_length=1, max_length=64)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    requirements: tuple[str, ...] = Field(default=(), max_length=128)


class WheelCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    wheels: dict[Annotated[str, Field(pattern=r"^[a-z0-9-]+$")], Wheel] = Field(
        max_length=64
    )


def requirements_for(
    libraries: tuple[Library, ...], catalog: WheelCatalog
) -> str:
    """Resolve only the locked wheel closure; no index or model-supplied specifier exists."""
    selected: set[str] = set()
    pending: list[str] = list(libraries)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        wheel = catalog.wheels.get(name)
        if wheel is None:
            raise ValueError("Approved wheel unavailable")
        selected.add(name)
        for raw in wheel.requirements:
            requirement = Requirement(raw)
            if (
                requirement.marker is not None
                and not requirement.marker.evaluate({"extra": ""})
            ):
                continue
            dependency = requirement.name.lower().replace("_", "-")
            artifact = catalog.wheels.get(dependency)
            if (
                requirement.url is not None
                or requirement.extras
                or artifact is None
                or Version(artifact.version) not in requirement.specifier
            ):
                raise ValueError(
                    "Dependency is outside the locked wheel policy"
                )
            pending.append(dependency)
    return "\n".join(
        f"{name}=={catalog.wheels[name].version} --hash=sha256:{catalog.wheels[name].sha256}"
        for name in sorted(selected)
    )


_WRAPPER = """import ast
import sys
from pathlib import Path

source = Path('/work/job.py').read_text()
tree = ast.parse(source, filename='calculation.py')
if tree.body and isinstance(tree.body[-1], ast.Expr):
    tree.body[-1] = ast.Assign(targets=[ast.Name(id='__calculation_result__', ctx=ast.Store())], value=tree.body[-1].value)
    ast.fix_missing_locations(tree)
scope = {'__name__':'__main__'}
exec(compile(tree, 'calculation.py', 'exec'), scope)
result = scope.get('__calculation_result__')
if result is not None:
    print(result)
pyplot = sys.modules.get('matplotlib.pyplot')
if pyplot is not None and pyplot.get_fignums():
    figure = pyplot.figure(pyplot.get_fignums()[0])
    figure.set_size_inches(6, 4)
    figure.savefig('/work/calculation.png', format='png', dpi=100)
"""


def execute(
    request: ExecutionRequest, catalog: WheelCatalog
) -> ExecutionResult:
    workspace = Path("/work")
    virtualenv = workspace / "venv"
    uv_binary = "/usr/local/bin/uv"
    environment = {
        "HOME": "/work",
        "TMPDIR": "/work",
        "UV_CACHE_DIR": "/work/cache",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": "/work/matplotlib",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
    }
    with (workspace / "installation.log").open("wb") as log:
        subprocess.run(
            [
                uv_binary,
                "venv",
                "--offline",
                "--no-python-downloads",
                "--python",
                sys.executable,
                str(virtualenv),
            ],
            env=environment,
            stdout=log,
            stderr=log,
            check=True,
            timeout=10,
        )
        if request.libraries:
            requirements = workspace / "requirements.txt"
            requirements.write_text(
                requirements_for(request.libraries, catalog)
            )
            subprocess.run(
                [
                    uv_binary,
                    "--no-config",
                    "--no-cache",
                    "pip",
                    "install",
                    "--offline",
                    "--no-index",
                    "--find-links",
                    "/opt/galadril/wheels",
                    "--python",
                    str(virtualenv / "bin/python"),
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "-r",
                    str(requirements),
                ],
                env=environment,
                stdout=log,
                stderr=log,
                check=True,
                timeout=25,
            )
    (workspace / "job.py").write_text(request.code)
    wrapper = workspace / "execute.py"
    wrapper.write_text(_WRAPPER)
    with (
        (workspace / "stdout").open("w+b") as stdout,
        (workspace / "stderr").open("w+b") as stderr,
    ):
        result = subprocess.run(
            [
                uv_binary,
                "run",
                "--offline",
                "--no-project",
                "--no-config",
                "--python",
                str(virtualenv / "bin/python"),
                "--",
                str(virtualenv / "bin/python"),
                "-I",
                str(wrapper),
            ],
            cwd=workspace,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            check=False,
            timeout=10,
        )
        stdout.seek(0)
        output = stdout.read(65537)
        stderr.seek(0)
        diagnostics = stderr.read(8192)
    if result.returncode != 0:
        return ExecutionResult(
            status="failed",
            failure="execution",
            text=diagnostics.decode("utf-8", errors="replace"),
        )
    if len(output) > 65536:
        return ExecutionResult(status="failed", failure="execution")
    image: str | None = None
    for path in sorted(workspace.glob("*.png")):
        # Never follow a user-created symlink while collecting generated artifacts.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            data = source.read(131073)
        if len(data) <= 131072:
            image = base64.b64encode(data).decode("ascii")
            break
    return ExecutionResult(
        status="completed",
        text=output.decode("utf-8", errors="replace"),
        image=image,
        libraries=tuple(
            InstalledLibrary(name=name, version=catalog.wheels[name].version)
            for name in request.libraries
        ),
    )


def main() -> None:
    phase: Literal["input", "catalog", "environment"] = "input"
    try:
        encoded = sys.argv[1] if len(sys.argv) == 2 else ""
        if len(encoded) > 65536:
            raise ValueError("Input exceeds limit")
        request = ExecutionRequest.model_validate_json(
            base64.b64decode(encoded, validate=True)
        )
        phase = "catalog"
        catalog = WheelCatalog.model_validate_json(
            Path(__file__).with_name("wheels.json").read_bytes()
        )
        phase = "environment"
        result = execute(request, catalog)
    except (ValueError, OSError, subprocess.SubprocessError):
        result = ExecutionResult(status="failed", failure=phase)
    print(result.model_dump_json())


if __name__ == "__main__":
    main()
