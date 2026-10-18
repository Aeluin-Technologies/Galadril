"""The controller cannot grant model code host files, credentials or networking."""

import json
import stat
import sys
from pathlib import Path

import httpx
import pytest
from galadril_sandbox.contracts import ExecutionRequest
from galadril_sandbox.controller import (
    Controller,
    ControllerSettings,
    container_config,
    private_listener,
)
from pydantic import ValidationError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_local_execution_uses_a_private_socket_without_an_api_key(
    tmp_path: Path,
) -> None:
    settings = ControllerSettings(
        deployment="development", worker_image="worker:local"
    )
    assert settings.socket_path == "/run/galadril-python/execute.sock"
    path = tmp_path / "private" / "execute.sock"
    with private_listener(str(path)) as listener:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert listener.getsockname() == str(path)
        with pytest.raises(OSError):
            private_listener(str(path))


def test_packages_are_names_from_a_closed_policy_not_install_arguments() -> (
    None
):
    for libraries in [
        ["requests"],
        ["numpy==1.0"],
        ["git+https://example.com"],
        ["--index-url=https://example.com"],
        ["numpy", "numpy"],
    ]:
        with pytest.raises(ValidationError):
            ExecutionRequest(code="print(42)", libraries=libraries)
    assert ExecutionRequest(
        code="import pandas", libraries=["pandas"]
    ).libraries == ("pandas",)


def test_each_worker_is_ephemeral_unprivileged_offline_and_has_no_host_mounts() -> (
    None
):
    settings = ControllerSettings(
        deployment="development", worker_image="worker:local"
    )
    config = container_config(
        settings, ExecutionRequest(code="print(42)", libraries=["pandas"])
    )
    host = config["HostConfig"]
    assert host["NetworkMode"] == "none" and config["NetworkDisabled"] is True
    assert config["User"] == "65532:65532"
    assert host["ReadonlyRootfs"] is True and host["CapDrop"] == ["ALL"]
    assert "no-new-privileges" in host["SecurityOpt"]
    assert "Binds" not in host and "Mounts" not in host
    assert "Privileged" not in host and "PidMode" not in host
    file_limit = next(
        limit for limit in host["Ulimits"] if limit["Name"] == "fsize"
    )
    assert file_limit["Hard"] >= 64 * 1024 * 1024
    assert "size=536870912" in host["Tmpfs"]["/work"]
    assert "exec" in host["Tmpfs"]["/work"].split(",")
    assert "PYTHON_SANDBOX_API_KEY" not in " ".join(config["Env"])
    with pytest.raises(ValidationError):
        ControllerSettings(worker_image="worker:latest", runtime="runc")


@pytest.mark.anyio
async def test_worker_is_destroyed_when_execution_or_delivery_fails() -> None:
    calls: list[httpx.Request] = []

    def engine(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/containers/create"):
            return httpx.Response(201, json={"Id": "worker1"})
        if request.url.path.endswith("/start"):
            return httpx.Response(204)
        if request.url.path.endswith("/wait"):
            return httpx.Response(200, json={"StatusCode": 1})
        if request.method == "DELETE":
            return httpx.Response(204)
        raise AssertionError(request.url)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(engine), base_url="http://docker/v1.47/"
    ) as client:
        controller = Controller(
            ControllerSettings(
                deployment="development",
                worker_image="worker:local",
            ),
            client,
        )
        result = await controller.execute(
            ExecutionRequest(code="raise RuntimeError()")
        )
    assert result.status == "failed"
    assert (
        calls[-1].method == "DELETE" and calls[-1].url.params["force"] == "true"
    )
    created = json.loads(calls[0].content)
    assert created["HostConfig"]["NetworkMode"] == "none"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
