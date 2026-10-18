"""Only this private controller can create fixed, credential-free OCI workers."""

import asyncio
import base64
import errno
import re
import socket
import time
import uuid
from pathlib import Path
from typing import Literal

import httpx
import structlog
from opentelemetry import metrics, trace
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from galadril_sandbox.contracts import ExecutionRequest, ExecutionResult

logger = structlog.get_logger(__name__)
_meter = metrics.get_meter("galadril.python-sandbox")
_executions = _meter.create_counter("python.sandbox.executions")
_duration = _meter.create_histogram("python.sandbox.duration", unit="s")


class EngineVersion(BaseModel):
    api: str = Field(alias="ApiVersion", pattern=r"^\d+\.\d+$")
    minimum: str = Field(
        default="1.24", alias="MinAPIVersion", pattern=r"^\d+\.\d+$"
    )


async def negotiate_engine(engine: httpx.AsyncClient) -> None:
    response = await engine.get("http://docker/version")
    response.raise_for_status()
    version = EngineVersion.model_validate_json(response.content)
    selected = min(tuple(map(int, version.api.split("."))), (1, 47))
    if selected < tuple(map(int, version.minimum.split("."))):
        raise ValueError("Unsupported Docker API version")
    engine.base_url = httpx.URL(f"http://docker/v{selected[0]}.{selected[1]}/")


class ControllerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PYTHON_SANDBOX_", hide_input_in_errors=True
    )
    deployment: Literal["development", "production"] = "production"
    worker_image: str = Field(min_length=1, max_length=256)
    runtime: Literal["runc", "runsc", "kata-runtime"] = "runsc"
    docker_socket: str = "/var/run/docker.sock"
    socket_path: str = "/run/galadril-python/execute.sock"
    max_concurrent: int = Field(default=2, ge=1, le=8)

    @model_validator(mode="after")
    def isolated_production(self) -> "ControllerSettings":
        if self.deployment == "production" and (
            self.runtime == "runc"
            or re.fullmatch(
                r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", self.worker_image
            )
            is None
        ):
            raise ValueError(
                "Production requires gVisor/Kata isolation and a digest-pinned worker"
            )
        return self


def private_listener(path: str) -> socket.socket:
    """Filesystem permissions authenticate local callers before accepting connections."""
    location = Path(path)
    if not location.is_absolute() or location.parent.is_symlink():
        raise ValueError("Sandbox requires a private absolute socket path")
    location.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    location.parent.chmod(0o700)
    if location.exists():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.connect(path)
            except OSError as error:
                if error.errno != errno.ECONNREFUSED:
                    raise
                location.unlink()
            else:
                raise OSError("Sandbox socket is already active")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(path)
        location.chmod(0o600)
        listener.listen(16)
        listener.setblocking(False)
        return listener
    except OSError:
        listener.close()
        raise


def container_config(
    settings: ControllerSettings, request: ExecutionRequest
) -> dict[str, object]:
    return {
        "Image": settings.worker_image,
        "Labels": {"galadril.python-sandbox": "true"},
        "Cmd": [
            base64.b64encode(request.model_dump_json().encode()).decode("ascii")
        ],
        "User": "65532:65532",
        "WorkingDir": "/work",
        "NetworkDisabled": True,
        "Env": [
            "HOME=/work",
            "TMPDIR=/work",
            "UV_CACHE_DIR=/work/cache",
            "MPLCONFIGDIR=/work/matplotlib",
            "MPLBACKEND=Agg",
            "PYTHONDONTWRITEBYTECODE=1",
            "OMP_NUM_THREADS=1",
            "OPENBLAS_NUM_THREADS=1",
            "MKL_NUM_THREADS=1",
        ],
        "HostConfig": {
            "Runtime": settings.runtime,
            "NetworkMode": "none",
            "IpcMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "PidsLimit": 64,
            "Memory": 1073741824,
            "MemorySwap": 1073741824,
            "NanoCpus": 1000000000,
            "Tmpfs": {"/work": "rw,exec,nosuid,nodev,size=536870912,mode=1777"},
            "Ulimits": [
                {"Name": "core", "Soft": 0, "Hard": 0},
                # Native scientific wheels contain shared libraries larger than 1 MiB.
                {"Name": "fsize", "Soft": 67108864, "Hard": 67108864},
                {"Name": "nofile", "Soft": 256, "Hard": 256},
            ],
            "LogConfig": {
                "Type": "json-file",
                "Config": {"max-size": "1m", "max-file": "1"},
            },
        },
    }


def stdout_frames(data: bytes) -> bytes:
    """Docker multiplexing is length-framed; stderr never enters public output."""
    output = bytearray()
    offset = 0
    while offset < len(data):
        header = data[offset : offset + 8]
        if len(header) != 8:
            raise ValueError("Incomplete worker frame")
        size = int.from_bytes(header[4:8])
        offset += 8
        if size > len(data) - offset:
            raise ValueError("Incomplete worker frame")
        if header[0] == 1:
            if len(output) + size > 524288:
                raise ValueError("Worker output exceeds limit")
            output.extend(memoryview(data)[offset : offset + size])
        offset += size
    return bytes(output)


class Controller:
    __slots__ = ("settings", "engine", "active")

    def __init__(
        self, settings: ControllerSettings, engine: httpx.AsyncClient
    ) -> None:
        self.settings = settings
        self.engine = engine
        self.active = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if self.active >= self.settings.max_concurrent:
            raise RuntimeError("Sandbox capacity exceeded")
        self.active += 1
        started_at = time.monotonic()
        _executions.add(1)
        name = "galadril-python-" + uuid.uuid4().hex
        try:
            with trace.get_tracer(
                "galadril.python-sandbox"
            ).start_as_current_span(
                "python.sandbox",
                record_exception=False,
                set_status_on_exception=False,
                attributes={"sandbox.library_count": len(request.libraries)},
            ):
                async with asyncio.timeout(60):
                    response = await self.engine.post(
                        "containers/create",
                        params={"name": name},
                        json=container_config(self.settings, request),
                    )
                    response.raise_for_status()
                    started = await self.engine.post(f"containers/{name}/start")
                    started.raise_for_status()
                    finished = await self.engine.post(f"containers/{name}/wait")
                    finished.raise_for_status()
                    if finished.json().get("StatusCode") != 0:
                        return ExecutionResult(status="failed")
                    body = bytearray()
                    async with self.engine.stream(
                        "GET",
                        f"containers/{name}/logs",
                        params={"stdout": "true", "stderr": "false"},
                    ) as logs:
                        logs.raise_for_status()
                        async for chunk in logs.aiter_bytes():
                            if len(body) + len(chunk) > 1048576:
                                raise ValueError("Worker logs exceed limit")
                            body.extend(chunk)
                    result = ExecutionResult.model_validate_json(
                        stdout_frames(bytes(body))
                    )
                    logger.info(
                        "sandbox.completed",
                        status=result.status,
                        library_count=len(result.libraries),
                    )
                    return result
        except (
            httpx.HTTPError,
            TimeoutError,
            ValueError,
            ValidationError,
        ) as error:
            logger.warning(
                "sandbox.unavailable", error_type=type(error).__name__
            )
            return ExecutionResult(status="failed")
        finally:
            try:
                async with asyncio.timeout(10):
                    deleted = await self.engine.delete(
                        f"containers/{name}",
                        params={"force": "true", "v": "true"},
                    )
                    if deleted.status_code not in {204, 404}:
                        logger.error(
                            "sandbox.cleanup.rejected",
                            status_code=deleted.status_code,
                        )
            except (httpx.HTTPError, TimeoutError):
                logger.error("sandbox.cleanup.unavailable")
            finally:
                self.active -= 1
                _duration.record(time.monotonic() - started_at)
