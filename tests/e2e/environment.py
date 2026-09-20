"""Bazel-runfiles-aware Docker Compose lifecycle for pipeline E2E tests."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from authzed.api.v1 import AsyncClient, WriteSchemaRequest
from grpcutil import insecure_bearer_token_credentials


class CommandFailure(RuntimeError):
    """Reports a failed environment command with its bounded output."""


def image_loader_command(loader: Path) -> tuple[str, str]:
    """Builds a command that tolerates remote-cache mode-bit loss."""
    return ("/bin/bash", str(loader))


def runfile(relative_path: str) -> Path:
    """Resolves one workspace file from a Bazel test runfiles tree."""
    root = os.environ.get("RUNFILES_DIR") or os.environ.get("TEST_SRCDIR")
    workspace = os.environ.get("TEST_WORKSPACE", "_main")
    if root is None:
        return Path(__file__).resolve().parents[2] / relative_path
    candidate = Path(root) / workspace / relative_path
    if not candidate.exists():
        raise FileNotFoundError(f"Runfile is unavailable: {relative_path}")
    return candidate


async def _run(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
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
        env=merged_environment,
    )
    output_bytes, _ = await process.communicate()
    output = output_bytes.decode("utf-8", errors="replace")[-100_000:]
    if check and process.returncode != 0:
        rendered = " ".join(command)
        raise CommandFailure(
            f"Command failed with status {process.returncode}: {rendered}\n{output}"
        )
    return output


class ComposeEnvironment:
    """Loads current-source images and owns an isolated Compose project."""

    __slots__ = ("_command", "_environment")

    def __init__(self) -> None:
        compose_file = runfile("tests/e2e/environment/compose.yaml")
        fixtures = runfile("tests/e2e/fixtures/connectors.yaml").parent
        schemas = runfile("schemas/spicedb/schema.zed").parent.parent
        infrastructure = runfile(
            "infrastructure/docker/init-scripts/01-init-spicedb.sh"
        ).parent.parent
        project = f"galadril-e2e-{os.getpid()}"
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
            "E2E_FIXTURES_DIR": str(fixtures),
            "E2E_INFRA_DIR": str(infrastructure),
            "E2E_SCHEMAS_DIR": str(schemas),
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
        logs = await _run(
            (*self._command, "logs", "--no-color", "--tail", "300"),
            environment=self._environment,
            check=False,
        )
        return f"\nDocker Compose state:\n{state}\nDocker Compose logs:\n{logs}"

    async def close(self) -> None:
        """Removes containers, networks, and volumes created by this test."""
        await _run(
            (*self._command, "down", "--volumes", "--remove-orphans"),
            environment=self._environment,
            check=False,
        )


@asynccontextmanager
async def pipeline_environment() -> AsyncIterator[ComposeEnvironment]:
    """Yields one environment and emits diagnostics before teardown."""
    environment = ComposeEnvironment()
    try:
        await environment.load_images()
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
