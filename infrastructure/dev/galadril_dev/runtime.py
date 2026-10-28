"""Supervised local processes never evaluate dotenv values as shell commands."""

from __future__ import annotations

import asyncio
import os
import platform
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .bootstrap import Bootstrap
from .files import BootstrapError


class CommandError(BootstrapError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(detail)


def redact(text: str, environment: Mapping[str, str]) -> str:
    for name, value in environment.items():
        if value and any(
            part in name for part in ("KEY", "SECRET", "PASSWORD", "TOKEN")
        ):
            text = text.replace(value, "[redacted]")
    return text


async def run(
    command: tuple[str, ...],
    *,
    environment: Mapping[str, str],
    root: Path | None = None,
    timeout: float = 600,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=root,
        env={**os.environ, **environment},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        process.kill()
        await process.communicate()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise CommandError(
            124, "Command timed out and was terminated"
        ) from None
    if process.returncode:
        detail = redact(stderr.decode(errors="replace"), environment)[-4000:]
        raise CommandError(
            process.returncode,
            f"{command[0]} failed (exit {process.returncode}): {detail}",
        )
    return stdout.decode(errors="replace")


def trust_command(system: str, certificate: str, home: str) -> tuple[str, ...]:
    if system == "Darwin":
        return (
            "security",
            "add-trusted-cert",
            "-r",
            "trustRoot",
            "-k",
            str(Path(home) / "Library/Keychains/login.keychain-db"),
            certificate,
        )
    raise BootstrapError(
        "On Linux/WSL use the documented system CA installation; trust is never bypassed"
    )


async def trust(bootstrap: Bootstrap) -> None:
    certificate = bootstrap.state / "identity/ca.pem"
    if platform.system() == "Darwin":
        command = trust_command("Darwin", str(certificate), str(Path.home()))
        process = await asyncio.create_subprocess_exec(*command)
        if await process.wait():
            raise BootstrapError(
                "Development CA trust installation was declined"
            )
        return
    if platform.system() == "Linux":
        if shutil.which("update-ca-certificates") is None:
            raise BootstrapError(
                "Install the development CA using your distribution's trust store"
            )
        destination = (
            "/usr/local/share/ca-certificates/galadril-development.crt"
        )
        for command in (
            ("sudo", "install", "-m", "0644", str(certificate), destination),
            ("sudo", "update-ca-certificates"),
        ):
            process = await asyncio.create_subprocess_exec(*command)
            if await process.wait():
                raise BootstrapError(
                    "Development CA trust installation was declined"
                )
        return
    raise BootstrapError(
        "Development bootstrap supports Linux, macOS, and Windows through WSL"
    )


class ContainerStatus(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    name: str = Field(alias="Name")
    service: str = Field(alias="Service")
    state: str = Field(alias="State")
    health: str = Field(default="", alias="Health")


async def containers(bootstrap: Bootstrap) -> list[ContainerStatus]:
    text = await run(
        (*bootstrap.compose_arguments(), "ps", "--all", "--format", "json"),
        environment=bootstrap.environment(),
        root=bootstrap.root,
        timeout=30,
    )
    if text.strip().startswith("["):
        return TypeAdapter(list[ContainerStatus]).validate_json(text)
    return [
        ContainerStatus.model_validate_json(line)
        for line in text.splitlines()
        if line.strip()
    ]


async def pin_compiler_images(bootstrap: Bootstrap, *, pull: bool) -> None:
    environment = bootstrap.environment()
    updates: dict[str, str] = {}
    for name in (
        "GALADRIL_DOCUMENT_COMPILER_IMAGE",
        "GALADRIL_DOCUMENT_WORKER_IMAGE",
    ):
        image = environment[name]
        if "@sha256:" in image or re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            continue
        if pull:
            await run(("docker", "pull", image), environment=environment)
        raw = await run(
            (
                "docker",
                "image",
                "inspect",
                image,
                "--format",
                "{{json .RepoDigests}}",
            ),
            environment=environment,
            timeout=30,
        )
        digests = TypeAdapter(list[str]).validate_json(raw)
        repository = image.rsplit(":", 1)[0]
        digest = next(
            (
                item
                for item in digests
                if item.startswith(repository + "@sha256:")
            ),
            None,
        )
        if digest is None:
            raise BootstrapError(
                "Compiler image has no immutable registry digest; pull its published image first"
            )
        updates[name] = digest
    if updates:
        bootstrap.update_environment(updates)


async def validate_host(bootstrap: Bootstrap) -> None:
    if shutil.which("docker") is None:
        raise BootstrapError(
            "Install Docker with Compose before starting Galadril"
        )
    await run(
        ("docker", "info", "--format", "{{.OSType}}"),
        environment={},
        timeout=30,
    )
    operating_system = await run(
        ("docker", "info", "--format", "{{.OSType}} {{.KernelVersion}}"),
        environment={},
        timeout=30,
    )
    if not operating_system.startswith("linux "):
        raise BootstrapError("The compiler requires a Linux Docker engine")
    match = re.search(r"linux (\d+)\.(\d+)", operating_system)
    if match is None or tuple(int(item) for item in match.groups()) < (6, 2):
        raise BootstrapError(
            "The compiler requires Linux 6.2+ with fully enforced Landlock ABI v3"
        )
    await run(
        (*bootstrap.compose_arguments(), "config", "--quiet"),
        environment=bootstrap.environment(),
        root=bootstrap.root,
        timeout=30,
    )


async def start(bootstrap: Bootstrap, *, pull: bool = True) -> None:
    await validate_host(bootstrap)
    await pin_compiler_images(bootstrap, pull=pull)
    command = (
        *bootstrap.compose_arguments(),
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "180",
        "--pull",
        "always" if pull else "missing",
    )
    await run(
        command,
        environment=bootstrap.environment(),
        root=bootstrap.root,
        timeout=1200,
    )
    await wait_for_compiler(bootstrap)


async def wait_for_compiler(bootstrap: Bootstrap) -> None:
    environment = bootstrap.environment()
    for _attempt in range(90):
        status = await containers(bootstrap)
        compiler = next(
            (item for item in status if item.service == "document-compiler"),
            None,
        )
        if compiler is None:
            raise BootstrapError(
                "Document compiler was not included in the Compose project"
            )
        if compiler.state == "running":
            try:
                await run(
                    (
                        *bootstrap.compose_arguments(),
                        "exec",
                        "--no-TTY",
                        "document-compiler",
                        "/document-compiler",
                        "--check-ready",
                    ),
                    environment=environment,
                    root=bootstrap.root,
                    timeout=10,
                )
                return
            except CommandError:
                pass
        if compiler.state in {"restarting", "exited", "dead"}:
            raise BootstrapError(
                "Compiler startup failed; inspect its OTLP diagnostics"
            )
        await asyncio.sleep(1)
    raise BootstrapError(
        "Compiler did not become ready; inspect its kernel-policy and storage startup checks"
    )
