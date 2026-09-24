"""Bazel-runfiles-aware Docker Compose lifecycle for pipeline E2E tests."""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from authzed.api.v1 import AsyncClient, WriteSchemaRequest
from grpcutil import insecure_bearer_token_credentials

_AVRO_SCHEMA_NAMES = (
    "audio.avsc",
    "authz.avsc",
    "authz_tuple.avsc",
    "document.avsc",
    "image.avsc",
    "ingestion_manifest.avsc",
    "observation_context.avsc",
    "sensor.avsc",
    "text.avsc",
    "transaction.avsc",
    "video.avsc",
)


class CommandFailure(RuntimeError):
    """Reports a failed environment command with its bounded output."""


def image_loader_command(loader: Path) -> tuple[str, str]:
    """Builds a command that tolerates remote-cache mode-bit loss."""
    return ("/bin/bash", str(loader))


def _add_archive_file(
    archive: tarfile.TarFile,
    source: Path,
    destination: str,
    *,
    mode: int = 0o644,
) -> None:
    """Adds deterministic file content without preserving runfile symlinks."""
    content = source.read_bytes()
    metadata = tarfile.TarInfo(destination)
    metadata.size = len(content)
    metadata.mode = mode
    metadata.mtime = 0
    archive.addfile(metadata, io.BytesIO(content))


def configuration_archive() -> bytes:
    """Packages E2E configuration for a Docker-daemon-owned volume."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for relative_path in (
            "connectors.yaml",
            "otel-collector.yaml",
            "tempo.yaml",
        ):
            _add_archive_file(
                archive,
                runfile(f"tests/e2e/fixtures/{relative_path}"),
                relative_path,
            )
        _add_archive_file(
            archive,
            runfile("tests/e2e/fixtures/e2e_inference_model.py"),
            "site-packages/e2e_inference_model.py",
        )
        _add_archive_file(
            archive,
            runfile("schemas/spicedb/schema.zed"),
            "spicedb/schema.zed",
        )
        for schema_name in _AVRO_SCHEMA_NAMES:
            _add_archive_file(
                archive,
                runfile(f"schemas/avro/{schema_name}"),
                f"avro/{schema_name}",
            )
        for source_path, destination in (
            (
                "database/docker-entrypoint-initdb.d/003-install-extensions.sh",
                "003-install-extensions.sh",
            ),
            (
                "database/docker-entrypoint-initdb.d/004-create-galadril-app.sh",
                "004-create-galadril-app.sh",
            ),
            (
                "infrastructure/docker/init-scripts/01-init-spicedb.sh",
                "010-init-spicedb.sh",
            ),
            (
                "infrastructure/docker/init-scripts/02-init-galadril-roles.sh",
                "020-init-galadril-roles.sh",
            ),
        ):
            _add_archive_file(
                archive,
                runfile(source_path),
                destination,
                mode=0o755,
            )
    return output.getvalue()


def runfile(relative_path: str) -> Path:
    """Resolves one workspace file from a Bazel test runfiles tree."""
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Runfile path must remain workspace-relative")
    # Keeping the unresolved module path preserves Bazel's runfiles symlink tree
    # while avoiding caller-controlled environment roots.
    candidate = Path(__file__).absolute().parents[2] / relative
    if not candidate.exists():
        raise FileNotFoundError(f"Runfile is unavailable: {relative_path}")
    return candidate


async def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> str:
    """Runs one bounded orchestration command without blocking the event loop."""
    merged_environment = os.environ.copy()
    if environment is not None:
        merged_environment.update(environment)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=(
            asyncio.subprocess.PIPE
            if input_bytes is not None
            else asyncio.subprocess.DEVNULL
        ),
        env=merged_environment,
    )
    output_bytes, _ = await process.communicate(input=input_bytes)
    output = output_bytes.decode("utf-8", errors="replace")[-100_000:]
    if check and process.returncode != 0:
        rendered = " ".join(command)
        raise CommandFailure(
            f"Command failed with status {process.returncode}: {rendered}\n{output}"
        )
    return output


class ComposeEnvironment:
    """Loads current-source images and owns an isolated Compose project."""

    __slots__ = ("_command", "_config_volume", "_environment")

    def __init__(self) -> None:
        compose_file = runfile("tests/e2e/environment/compose.yaml")
        project = f"galadril-e2e-{os.getpid()}"
        self._config_volume = f"{project}-config"
        self._command = (
            "docker",
            "compose",
            "--project-name",
            project,
            "--file",
            str(compose_file),
        )
        self._environment = {
            "COMPOSE_PROJECT_NAME": project,
            "E2E_CONFIG_VOLUME": self._config_volume,
        }

    async def load_images(self) -> None:
        """Loads the four Bazel-built application images into Docker."""
        for target in (
            "tests/e2e/load_gateway.sh",
            "tests/e2e/load_intake.sh",
            "tests/e2e/load_registry.sh",
            "tests/e2e/load_vision.sh",
        ):
            await _run(image_loader_command(runfile(target)))

    async def prepare_configuration(self) -> None:
        """Copies runfiles through Docker stdin into a daemon-owned volume."""
        await _run(("docker", "volume", "create", self._config_volume))
        await _run(
            (
                "docker",
                "run",
                "--rm",
                "--interactive",
                "--volume",
                f"{self._config_volume}:/e2e",
                "busybox:1.37.0-musl",
                "tar",
                "-x",
                "-f",
                "-",
                "-C",
                "/e2e",
            ),
            input_bytes=configuration_archive(),
        )

    async def start_core(self) -> None:
        """Starts infrastructure plus Gateway, Registry, and Intake."""
        await _run(
            (
                *self._command,
                "up",
                "--detach",
                "--wait",
                "registry",
                "spicedb",
            ),
            environment=self._environment,
        )
        await self._install_spicedb_schema()
        await _run(
            (
                *self._command,
                "up",
                "--detach",
                "--wait",
                "gateway",
                "intake",
            ),
            environment=self._environment,
        )

    async def _install_spicedb_schema(self) -> None:
        """Installs the canonical schema before Gateway verifies it."""
        schema = runfile("schemas/spicedb/schema.zed").read_text(
            encoding="utf-8"
        )
        credentials = insecure_bearer_token_credentials("secret_key")
        client = AsyncClient("127.0.0.1:15051", credentials)
        await client.WriteSchema(WriteSchemaRequest(schema=schema))

    async def start_vision(self) -> None:
        """Starts Vision after its exact Registry revision is published."""
        await _run(
            (*self._command, "up", "--detach", "vision"),
            environment=self._environment,
        )

    async def logs(self) -> str:
        """Returns bounded service state and logs for a failed test."""
        state = await _run(
            (*self._command, "ps", "--all"),
            environment=self._environment,
            check=False,
        )
        infrastructure_logs = await _run(
            (*self._command, "logs", "--no-color", "--tail", "100"),
            environment=self._environment,
            check=False,
        )
        application_logs = await _run(
            (
                *self._command,
                "logs",
                "--no-color",
                "--tail",
                "300",
                "gateway",
                "registry",
                "intake",
                "vision",
            ),
            environment=self._environment,
            check=False,
        )
        return (
            f"\nDocker Compose state:\n{state}"
            f"\nDocker Compose infrastructure logs:\n{infrastructure_logs}"
            f"\nDocker Compose application logs:\n{application_logs}"
        )

    async def close(self) -> None:
        """Removes containers, networks, and volumes created by this test."""
        await _run(
            (*self._command, "down", "--volumes", "--remove-orphans"),
            environment=self._environment,
            check=False,
        )
        await _run(
            ("docker", "volume", "rm", "--force", self._config_volume),
            check=False,
        )


@asynccontextmanager
async def pipeline_environment() -> AsyncIterator[ComposeEnvironment]:
    """Yields one environment and emits diagnostics before teardown."""
    environment = ComposeEnvironment()
    try:
        await environment.load_images()
        await environment.prepare_configuration()
        await environment.start_core()
        yield environment
    except BaseException as error:
        try:
            diagnostics = await environment.logs()
        except Exception as diagnostic_error:
            error.add_note(
                f"Unable to collect Docker Compose diagnostics: "
                f"{diagnostic_error}"
            )
        else:
            error.add_note(diagnostics)
        raise
    finally:
        await environment.close()
