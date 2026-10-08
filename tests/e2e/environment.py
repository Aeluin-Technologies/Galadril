"""Bazel-runfiles-aware Docker Compose lifecycle for pipeline E2E tests."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import tarfile
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from infrastructure.envoy.testing import identity_files, jwks_bytes

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
PIPELINE_LIFECYCLE_TIMEOUT_SECONDS = 4800.0
_CONFIG_CATEGORIES = ("application", "observability", "database", "proxy")


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
    """Packages isolated configuration volumes without daemon host paths."""
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
                (
                    "application/"
                    if relative_path == "connectors.yaml"
                    else "observability/"
                )
                + relative_path,
            )
        for name, content in identity_files().items():
            if not name.startswith(("identity/gateway/", "identity/registry/")):
                continue
            destination = name.replace("identity/gateway/", "proxy/public/")
            destination = destination.replace(
                "identity/registry/", "proxy/identity/"
            )
            metadata = tarfile.TarInfo(destination)
            metadata.size = len(content)
            metadata.mode = 0o444
            archive.addfile(metadata, io.BytesIO(content))
        content = runfile("infrastructure/envoy/local.yaml").read_text()
        content = content.replace(
            "https://aeluin.gravitalia.com", "https://e2e.galadril.test"
        )
        content = content.replace("- galadril\n", "- galadril-e2e\n")
        payload = content.encode("utf-8")
        metadata = tarfile.TarInfo("proxy/envoy.yaml")
        metadata.size = len(payload)
        metadata.mode = 0o444
        archive.addfile(metadata, io.BytesIO(payload))
        jwks = jwks_bytes()
        metadata = tarfile.TarInfo("proxy/jwks.json")
        metadata.size = len(jwks)
        metadata.mode = 0o444
        archive.addfile(metadata, io.BytesIO(jwks))
        _add_archive_file(
            archive,
            runfile("tests/e2e/fixtures/e2e_inference_model.py"),
            "application/site-packages/e2e_inference_model.py",
        )
        _add_archive_file(
            archive,
            runfile("schemas/spicedb/schema.zed"),
            "application/spicedb/schema.zed",
        )
        for schema_name in _AVRO_SCHEMA_NAMES:
            _add_archive_file(
                archive,
                runfile(f"schemas/avro/{schema_name}"),
                f"application/avro/{schema_name}",
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
                "database/" + destination,
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
    timeout_seconds: float = 180.0,
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
        start_new_session=True,
    )
    communication = asyncio.create_task(process.communicate(input=input_bytes))
    try:
        output_bytes, _ = await asyncio.wait_for(
            asyncio.shield(communication),
            timeout=timeout_seconds,
        )
    except (TimeoutError, asyncio.CancelledError) as error:
        # OCI loaders spawn tar and Docker children that inherit output pipes.
        # Preserve the reader until the entire command group has been reaped.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            output_bytes, _ = await asyncio.wait_for(
                asyncio.shield(communication), timeout=5.0
            )
        except TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                output_bytes, _ = await asyncio.wait_for(
                    communication, timeout=5.0
                )
            except TimeoutError as cleanup_error:
                raise CommandFailure(
                    "Timed-out orchestration process could not be reaped"
                ) from cleanup_error
        if isinstance(error, asyncio.CancelledError):
            raise
        rendered = " ".join(command)
        output = output_bytes.decode("utf-8", errors="replace")[-100_000:]
        raise CommandFailure(
            f"Command timed out after {timeout_seconds:.2f}s: {rendered}\n{output}"
        ) from error
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
            str(runfile("infrastructure/docker/docker-compose")),
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
        """Loads the application images and bounded inference protocol fixture."""
        for target, timeout_seconds in (
            ("tests/e2e/load_gateway.sh", 600.0),
            ("tests/e2e/load_intake.sh", 600.0),
            ("tests/e2e/load_registry.sh", 600.0),
            ("tests/e2e/load_vision.sh", 1200.0),
            ("tests/e2e/load_scribe.sh", 600.0),
            ("tests/e2e/load_chat_model.sh", 600.0),
        ):
            print(f"E2E stage: loading {target}", flush=True)
            print(
                await _run(
                    image_loader_command(runfile(target)),
                    timeout_seconds=timeout_seconds,
                ),
                flush=True,
            )

    async def prepare_configuration(self) -> None:
        """Copies runfiles through Docker stdin into isolated daemon volumes."""
        print("E2E stage: preparing configuration", flush=True)
        print(await _run(("docker", "version")), flush=True)
        print(await _run((self._command[0], "version")), flush=True)
        mounts: list[str] = []
        for category in _CONFIG_CATEGORIES:
            volume = f"{self._config_volume}-{category}"
            await _run(("docker", "volume", "create", volume))
            mounts.extend(("--volume", f"{volume}:/config/{category}"))
        # Older remote Compose clients ignore volume subpaths. Separate volumes
        # also prevent application containers from reading proxy private keys.
        await _run(
            (
                "docker",
                "run",
                "--rm",
                "--interactive",
                *mounts,
                "--tmpfs",
                "/bundle",
                "busybox:1.37.0-musl",
                "sh",
                "-ec",
                "tar -xf - -C /bundle; "
                "for category in application observability database proxy; do "
                'cp -a "/bundle/$category/." "/config/$category/"; done',
            ),
            input_bytes=configuration_archive(),
        )

    async def start_core(self) -> None:
        """Starts infrastructure plus Gateway, Registry, and Intake."""
        print("E2E stage: starting Registry and SpiceDB", flush=True)
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
            timeout_seconds=1200.0,
        )
        print("E2E stage: starting Gateway and Intake", flush=True)
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
            timeout_seconds=1200.0,
        )

    async def start_vision(self) -> None:
        """Starts Vision after its exact Registry revision is published."""
        print("E2E stage: starting Vision", flush=True)
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
            timeout_seconds=90.0,
        )
        infrastructure_logs = await _run(
            (*self._command, "logs", "--no-color", "--tail", "100"),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
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
            timeout_seconds=90.0,
        )
        pipeline_state = await _run(
            (
                *self._command,
                "exec",
                "--no-TTY",
                "postgres",
                "psql",
                "--username",
                "postgres",
                "--dbname",
                "galadril_dev",
                "--tuples-only",
                "--command",
                """
                SELECT json_build_object(
                  'executions', COALESCE((
                    SELECT json_agg(row_to_json(execution))
                    FROM (
                      SELECT step, status, COUNT(*) AS count
                      FROM pipeline_executions
                      WHERE tenant_id = 'debug_tenant'
                      GROUP BY step, status
                      ORDER BY step, status
                    ) AS execution
                  ), '[]'::json),
                  'failed_executions', COALESCE((
                    SELECT json_agg(row_to_json(failure))
                    FROM (
                      SELECT step, attempt, error
                      FROM pipeline_executions
                      WHERE tenant_id = 'debug_tenant'
                        AND status = 'failed'
                      ORDER BY updated_at DESC
                      LIMIT 5
                    ) AS failure
                  ), '[]'::json),
                  'entity_count', (
                    SELECT COUNT(*) FROM entity_states
                    WHERE tenant_id = 'debug_tenant'
                  ),
                  'retail_state_count', (
                    SELECT COUNT(*) FROM entity_states
                    WHERE tenant_id = 'debug_tenant'
                      AND state_value->>'label' = 'retail-customer'
                  ),
                  'outbox_count', (
                    SELECT COUNT(*) FROM authz_outbox
                    WHERE tenant_id = 'debug_tenant'
                  ),
                  'outbox_rows', COALESCE((
                    SELECT json_agg(row_to_json(outbox_row))
                    FROM (
                      SELECT id, object_id, attempts, next_retry_at, updated_at
                      FROM authz_outbox
                      WHERE tenant_id = 'debug_tenant'
                      ORDER BY id
                    ) AS outbox_row
                  ), '[]'::json),
                  'maintenance_role', (
                    SELECT json_build_object(
                      'rolbypassrls', rolbypassrls,
                      'can_select', has_table_privilege(
                        'galadril_maintenance', 'authz_outbox', 'SELECT'
                      ),
                      'can_update', has_table_privilege(
                        'galadril_maintenance', 'authz_outbox', 'UPDATE'
                      ),
                      'can_delete', has_table_privilege(
                        'galadril_maintenance', 'authz_outbox', 'DELETE'
                      )
                    )
                    FROM pg_roles
                    WHERE rolname = 'galadril_maintenance'
                  ),
                  'maintenance_sessions', COALESCE((
                    SELECT json_agg(row_to_json(session))
                    FROM (
                      SELECT application_name, state, wait_event_type, wait_event
                      FROM pg_stat_activity
                      WHERE usename = 'galadril_maintenance'
                      ORDER BY pid
                    ) AS session
                  ), '[]'::json)
                );
                """,
            ),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
        )
        ray_logs = await _run(
            (
                *self._command,
                "exec",
                "--no-TTY",
                "vision",
                "/bin/sh",
                "-c",
                "for file in /tmp/ray/session_latest/logs/worker-*.err "
                "/tmp/ray/session_latest/logs/worker-*.out "
                "/tmp/ray/session_latest/logs/raylet.err; do "
                'if [ -f "$file" ]; then '
                "printf '\\n==> %s <==\\n' \"$file\"; "
                'tail -n 120 "$file"; fi; done',
            ),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
        )
        consumer_offsets = await _run(
            (
                *self._command,
                "exec",
                "--no-TTY",
                "redpanda",
                "rpk",
                "group",
                "describe",
                "galadril-e2e",
                "galadril-e2e-ingress",
                "galadril-e2e-cpu",
                "galadril-e2e-gpu",
                "galadril-e2e-causal",
            ),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
        )
        return (
            f"\nVision durable pipeline state:\n{pipeline_state}"
            f"\nDocker Compose state:\n{state}"
            f"\nDocker Compose infrastructure logs:\n{infrastructure_logs}"
            f"\nDocker Compose application logs:\n{application_logs}"
            f"\nVision Ray worker logs:\n{ray_logs}"
            f"\nIntake and Vision consumer offsets:\n{consumer_offsets}"
        )

    async def close(self) -> None:
        """Removes containers, networks, and volumes created by this test."""
        await _run(
            (
                *self._command,
                "down",
                "--timeout",
                "5",
                "--volumes",
                "--remove-orphans",
            ),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
        )
        await _run(
            (
                "docker",
                "volume",
                "rm",
                "--force",
                *(
                    f"{self._config_volume}-{category}"
                    for category in _CONFIG_CATEGORIES
                ),
            ),
            check=False,
            timeout_seconds=90.0,
        )


@asynccontextmanager
async def pipeline_environment() -> AsyncIterator[ComposeEnvironment]:
    """Yields one environment and emits diagnostics before teardown."""
    environment = ComposeEnvironment()
    primary_error: BaseException | None = None
    try:
        await environment.load_images()
        await environment.prepare_configuration()
        await environment.start_core()
        yield environment
    except BaseException as error:
        primary_error = error
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
        try:
            await environment.close()
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "Unable to clean up Docker Compose environment: "
                f"{cleanup_error}"
            )
