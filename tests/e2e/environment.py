"""Bazel-runfiles-aware Docker Compose lifecycle for pipeline E2E tests."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import secrets
import signal
import tarfile
from collections import deque
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import NotRequired, TypedDict
from uuid import UUID, uuid4

from assertions import require_mapping
from galadril_pipeline.telemetry_contract import (
    ATTR_ERROR_CODE,
    ATTR_ERROR_OCCURRENCE_ID,
    ATTR_PIPELINE_ATTEMPT,
    ATTR_VISION_PHASE,
    ATTR_VISION_RETRY_SCHEDULED,
    EVENT_GOL_WORKER_FAILED,
    EVENT_VISION_STEP_FAILED,
    ErrorCode,
    VisionPhase,
)

from infrastructure.envoy.testing import identity_files

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
# The two empirical cohorts run through the same authorizing worker pipeline.
EMPIRICAL_STATE_TIMEOUT_SECONDS = 2 * 60 * 60.0
PIPELINE_LIFECYCLE_TIMEOUT_SECONDS = 5 * 60 * 60.0
_CONFIG_CATEGORIES = (
    "application",
    "observability",
    "database",
    "proxy",
    "hydra",
    "gol-secrets",
    "registry-secrets",
    "worker-secrets",
)


CONFIGURATION_IMAGE = (
    "busybox:1.37.0-musl@sha256:"
    "f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478"
)


class CommandFailure(RuntimeError):
    """Reports a failed environment command with its bounded output."""


def _trace_id(value: object) -> str | None:
    """Normalizes OTLP JSON trace identifiers without exporting arbitrary strings."""
    if not isinstance(value, str):
        return None
    try:
        raw = (
            bytes.fromhex(value)
            if len(value) == 32
            else base64.b64decode(value, validate=True)
        )
    except ValueError:
        return None
    return raw.hex() if len(raw) == 16 and any(raw) else None


class PipelineFailureEvent(TypedDict):
    """Carries catalog fields that can safely be attached to a failed test."""

    event: str
    code: ErrorCode
    occurrence_id: str
    trace_id: NotRequired[str]
    phase: NotRequired[VisionPhase]
    attempt: NotRequired[int]
    retry_scheduled: NotRequired[bool]


def _pipeline_failure_events(
    lines: Iterable[str],
) -> list[PipelineFailureEvent]:
    """Extracts only catalog values from Collector OTLP JSON log batches."""
    recent: deque[PipelineFailureEvent] = deque(maxlen=24)
    for line in lines:
        try:
            envelope = json.loads(line)
        except json.JSONDecodeError:
            continue
        for resource in envelope.get("resourceLogs", []):
            for scope in resource.get("scopeLogs", []):
                for record in scope.get("logRecords", []):
                    event = record.get("eventName")
                    if event not in (
                        EVENT_GOL_WORKER_FAILED,
                        EVENT_VISION_STEP_FAILED,
                    ):
                        continue
                    attributes = {
                        attr["key"]: attr["value"]
                        for attr in record.get("attributes", [])
                    }
                    try:
                        details = PipelineFailureEvent(
                            event=event,
                            code=ErrorCode(
                                attributes[ATTR_ERROR_CODE]["stringValue"]
                            ),
                            occurrence_id=str(
                                UUID(
                                    attributes[ATTR_ERROR_OCCURRENCE_ID][
                                        "stringValue"
                                    ]
                                )
                            ),
                        )
                        trace_id = _trace_id(record.get("traceId"))
                        if trace_id is not None:
                            details["trace_id"] = trace_id
                        if event == EVENT_VISION_STEP_FAILED:
                            details["phase"] = VisionPhase(
                                attributes[ATTR_VISION_PHASE]["stringValue"]
                            )
                            details["attempt"] = int(
                                attributes[ATTR_PIPELINE_ATTEMPT]["intValue"]
                            )
                            retry = attributes.get(
                                ATTR_VISION_RETRY_SCHEDULED, {}
                            ).get("boolValue")
                            if isinstance(retry, bool):
                                details["retry_scheduled"] = retry
                    except (KeyError, TypeError, ValueError):
                        continue
                    recent.append(details)
    return list(recent)


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


def configuration_archive(*, streaming: bool = True) -> bytes:
    """Packages isolated configuration volumes without daemon host paths."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for relative_path in (
            "connectors.yaml",
            "otel-collector.yaml",
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
        _add_archive_file(
            archive,
            runfile("tests/e2e/fixtures/gol-authorities.json"),
            "application/gol-authorities.json",
        )
        _add_archive_file(
            archive,
            runfile("tests/e2e/fixtures/gol-runtime.json"),
            "application/gol-runtime.json",
        )
        worker_key = secrets.token_bytes(32)
        worker_metadata = tarfile.TarInfo("worker-secrets/worker-key")
        worker_metadata.size = len(worker_key)
        worker_metadata.mode = 0o444
        archive.addfile(worker_metadata, io.BytesIO(worker_key))
        gateway_key = secrets.token_bytes(32)
        gateway_metadata = tarfile.TarInfo("registry-secrets/gateway-key")
        gateway_metadata.size = len(gateway_key)
        gateway_metadata.mode = 0o444
        archive.addfile(gateway_metadata, io.BytesIO(gateway_key))
        effect_key = secrets.token_bytes(32)
        effect_metadata = tarfile.TarInfo("gol-secrets/gol-effect-key")
        effect_metadata.size = len(effect_key)
        effect_metadata.mode = 0o444
        archive.addfile(effect_metadata, io.BytesIO(effect_key))
        _add_archive_file(
            archive,
            runfile("infrastructure/observability/tempo.yaml"),
            "observability/tempo.yaml",
        )
        _add_archive_file(
            archive,
            runfile("infrastructure/docker/s3-notifications.yaml"),
            "application/s3-notifications.yaml",
        )
        for name, content in identity_files().items():
            if not name.startswith(
                ("identity/gateway/", "identity/registry/", "identity/hydra/")
            ):
                continue
            destination = name.replace("identity/gateway/", "proxy/public/")
            destination = destination.replace(
                "identity/registry/", "proxy/identity/"
            )
            destination = destination.replace("identity/hydra/", "hydra/tls/")
            metadata = tarfile.TarInfo(destination)
            metadata.size = len(content)
            metadata.mode = 0o444
            archive.addfile(metadata, io.BytesIO(content))
        _add_archive_file(
            archive,
            runfile("infrastructure/envoy/error-page.lua"),
            "proxy/error-page.lua",
        )
        content = runfile("infrastructure/envoy/local.yaml").read_text()
        payload = content.encode("utf-8")
        metadata = tarfile.TarInfo("proxy/envoy.yaml")
        metadata.size = len(payload)
        metadata.mode = 0o444
        archive.addfile(metadata, io.BytesIO(payload))
        for name in ("hydra.yaml", "kratos.yaml", "identity.schema.json"):
            destination = (
                "hydra/hydra.yaml"
                if name == "hydra.yaml"
                else "application/auth/" + name
            )
            _add_archive_file(
                archive, runfile(f"infrastructure/auth/{name}"), destination
            )
        ca = identity_files()["identity/hydra/ca.pem"]
        metadata = tarfile.TarInfo("application/auth/ca.pem")
        metadata.size = len(ca)
        archive.addfile(metadata, io.BytesIO(ca))
        _add_archive_file(
            archive,
            runfile("schemas/spicedb/schema.zed"),
            "application/spicedb/schema.zed",
        )
        if streaming:
            _add_archive_file(
                archive,
                runfile("tests/e2e/fixtures/e2e_inference_model.py"),
                "application/site-packages/e2e_inference_model.py",
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
            (
                "infrastructure/docker/init-scripts/03-init-auth.sh",
                "030-init-auth.sh",
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

    __slots__ = ("_command", "_config_volume", "_environment", "_images")

    def __init__(self) -> None:
        compose_file = runfile("tests/e2e/environment/compose.yaml")
        project = f"galadril-e2e-{uuid4().hex}"
        self._images = tuple(os.environ.get("GALADRIL_E2E_IMAGES", "").split())
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
            "E2E_SCRIBE_ENABLED": "true"
            if "scribe" in self._images
            else "false",
            "E2E_CONFIG_VOLUME": self._config_volume,
            "GALADRIL_AUTH_PASSWORD": "e2e-auth-only",
            "HYDRA_DATABASE_PASSWORD": "e2e-hydra-only",
            "POLIS_DATABASE_PASSWORD": "e2e-polis-only",
            "POLIS_API_KEY": "e2e-polis-only-012345678901234567890123456789",
            "POLIS_ENCRYPTION_KEY": "01234567890123456789012345678901",
            "POLIS_CLIENT_SECRET_VERIFIER": "e2e-verifier-only-01234567890123456789",
            "SSO_PROVISIONING_KEY": "e2e-provisioning-only-01234567890123456789",
            "AUTH_PUBLIC_ORIGIN": "https://127.0.0.1:18080",
            "AUTH_LOCAL_LOGIN_ENABLED": "true",
            "AUTH_CLIENT_SECRET": "e2e-client-only-01234567890123456789",
            "AUTH_SESSION_SECRET": "e2e-session-only-01234567890123456789",
            "AUTH_GATEWAY_KEY": "e2e-private-gateway-only-01234567890123456789",
            "HYDRA_SYSTEM_SECRET": "e2e-hydra-system-01234567890123456789",
            "KRATOS_DATABASE_PASSWORD": "e2e-kratos-only",
            "KRATOS_COOKIE_SECRET": "e2e-kratos-cookie-01234567890123456789",
            "KRATOS_CIPHER_SECRET": "01234567890123456789012345678901",
        }

    async def load_images(self) -> None:
        """Loads the application images and bounded inference protocol fixture."""
        for service in self._images:
            target = f"tests/e2e/load_{service}.sh"
            timeout_seconds = 1200.0 if service == "vision" else 600.0
            started = monotonic()
            print(f"E2E stage: loading {target}", flush=True)
            print(
                await _run(
                    image_loader_command(runfile(target)),
                    timeout_seconds=timeout_seconds,
                ),
                flush=True,
            )
            print(
                f"E2E image import: {service} elapsed={monotonic() - started:.2f}s",
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
                CONFIGURATION_IMAGE,
                "sh",
                "-ec",
                "tar -xf - -C /bundle; "
                "for category in application observability database proxy hydra gol-secrets registry-secrets worker-secrets; do "
                'cp -a "/bundle/$category/." "/config/$category/"; done',
            ),
            input_bytes=configuration_archive(
                streaming=any(
                    service in self._images for service in ("vision", "intake")
                )
            ),
        )

    async def start_core(self) -> None:
        """Starts the control plane and the selected scenario entry points."""
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
        services = ("gateway", "hydra", "kratos", "studio") + tuple(
            service
            for service in ("intake", "scribe")
            if service in self._images
        )
        print(f"E2E stage: starting {', '.join(services)}", flush=True)
        await _run(
            (
                *self._command,
                "up",
                "--detach",
                "--wait",
                *services,
            ),
            environment=self._environment,
            timeout_seconds=1200.0,
        )

    async def identity_request(
        self, url: str, body: Mapping[str, object] | None, method: str
    ) -> Mapping[str, object]:
        """Calls loopback identity APIs without publishing administrative ports."""
        script = "const fs=require('node:fs');const x=JSON.parse(fs.readFileSync(0,'utf8'));fetch(x.url,{method:x.method,headers:{'content-type':'application/json'},body:x.body===null?undefined:JSON.stringify(x.body)}).then(async r=>{if(!r.ok)throw Error('Identity request failed '+r.status);console.log((await r.text())||'{}')}).catch(()=>process.exit(1))"
        output = await _run(
            (
                *self._command,
                "exec",
                "--no-TTY",
                "studio",
                "/nodejs/bin/node",
                "-e",
                script,
            ),
            environment=self._environment,
            input_bytes=json.dumps(
                {"url": url, "body": body, "method": method}
            ).encode(),
        )
        return require_mapping(json.loads(output), "identity response")

    async def authentication_ready(self) -> bool | None:
        """Wait for the public sign-in path as well as private identity services."""
        for url in (
            "http://127.0.0.1:8081/healthz",
            "http://127.0.0.1:4434/health/ready",
            "http://127.0.0.1:4445/health/ready",
        ):
            await self.identity_request(url, None, "GET")
        # Private readiness does not prove that Envoy can reach the Nuxt auth server.
        studio = await self.identity_request(
            "https://127.0.0.1:8080/healthz", None, "GET"
        )
        return True if studio.get("status") == "ok" else None

    async def start_vision(self) -> None:
        """Starts Vision before provisioning to exercise publication waiting."""
        if "vision" not in self._images:
            raise AssertionError("Vision must be declared in the Bazel runtime")
        print("E2E stage: starting Vision", flush=True)
        await _run(
            (*self._command, "up", "--detach", "vision"),
            environment=self._environment,
        )

    async def assert_vision_did_not_restart(self) -> None:
        """Rejects recovery through a restart that would mask cold-start failures."""
        container_id = await _run(
            (*self._command, "ps", "--all", "--quiet", "vision"),
            environment=self._environment,
        )
        state = await _run(
            (
                "docker",
                "inspect",
                "--format",
                "{{.State.Status}} {{.RestartCount}}",
                container_id.strip(),
            )
        )
        assert state.strip() == "running 0", (
            f"Vision restarted or exited: {state}"
        )

    async def vision_waiting_for_publication(self) -> bool | None:
        """Observes the worker waiting without depending on the collector tail."""
        await self.assert_vision_did_not_restart()
        logs = await _run(
            (*self._command, "logs", "--no-color", "--tail", "50", "vision"),
            environment=self._environment,
        )
        event = '{"event.name":"pipeline.publication.pending"}'
        return next(
            (
                True
                for line in logs.splitlines()
                if line.partition("|")[2].strip() == event
            ),
            None,
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
                "scribe",
                "registry",
                "intake",
                "vision",
            ),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
        )
        application_logs += await _run(
            (*self._command, "logs", "--no-color", "--tail", "100", "mcp"),
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
        collector_logs = await _run(
            (
                *self._command,
                "logs",
                "--no-color",
                "--tail",
                "12000",
                "otel-collector",
            ),
            environment=self._environment,
            check=False,
            timeout_seconds=90.0,
        )
        infrastructure_logs += "\n".join(collector_logs.splitlines()[-1000:])
        failure_events = await self.pipeline_failure_events()
        return (
            f"\nVision durable pipeline state:\n{pipeline_state}"
            f"\nPipeline failure events:\n{json.dumps(failure_events, indent=2)}"
            f"\nDocker Compose state:\n{state}"
            f"\nDocker Compose infrastructure logs:\n{infrastructure_logs}"
            f"\nDocker Compose application logs:\n{application_logs}"
            f"\nVision Ray worker logs:\n{ray_logs}"
            f"\nIntake and Vision consumer offsets:\n{consumer_offsets}"
        )

    async def pipeline_failure_events(self) -> list[PipelineFailureEvent]:
        """Reads the bounded Collector file for correlated pipeline events."""
        with TemporaryDirectory() as temporary:
            log_path = Path(temporary) / "otlp.jsonl"
            await _run(
                (
                    *self._command,
                    "cp",
                    "otel-collector:/tmp/galadril-e2e-logs.jsonl",
                    str(log_path),
                ),
                environment=self._environment,
                check=False,
                timeout_seconds=90.0,
            )
            if log_path.is_file():
                with log_path.open(encoding="utf-8") as logs:
                    return _pipeline_failure_events(logs)
        return []

    async def _postgres_diagnostics(self, filename: str) -> object:
        """Reads bounded summaries without serializing SQL text or parameters."""
        statement = runfile(f"database/diagnostics/{filename}.sql").read_text(
            encoding="utf-8"
        )
        if filename == "statements":
            query = statement.rstrip().removesuffix(";")
            statement = (
                "SELECT coalesce(json_agg(ranked ORDER BY total_rank), '[]'::json) "
                f"FROM (\n{query}\n) AS ranked"
            )
        output = await _run(
            (
                *self._command,
                "exec",
                "--no-TTY",
                "postgres",
                "psql",
                "--username=postgres",
                "--dbname=galadril_dev",
                "--no-psqlrc",
                "--no-align",
                "--tuples-only",
                "--set=ON_ERROR_STOP=1",
                "--command",
                statement,
            ),
            environment=self._environment,
            timeout_seconds=10.0,
        )
        return json.loads(output)

    async def failure_artifact(self) -> dict[str, object]:
        """Collects operational failure identifiers and bounded SQL data before teardown."""
        keys = ("failure_events", "postgres", "dominant_statements")
        events, snapshot, statements = await asyncio.gather(
            self.pipeline_failure_events(),
            self._postgres_diagnostics("snapshot"),
            self._postgres_diagnostics("statements"),
            return_exceptions=True,
        )
        values: tuple[object, ...] = (events, snapshot, statements)
        return {
            key: value
            for key, value in zip(keys, values, strict=True)
            if not isinstance(value, BaseException)
        }

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
    output = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    primary_error: BaseException | None = None
    phases: dict[str, float] = {}
    started = monotonic()
    try:
        for name, run in (
            ("load_images", environment.load_images),
            ("prepare_configuration", environment.prepare_configuration),
            ("start_core", environment.start_core),
        ):
            phase_started = monotonic()
            try:
                await run()
            finally:
                phases[name] = monotonic() - phase_started
        phase_started = monotonic()
        try:
            yield environment
        finally:
            phases["scenario"] = monotonic() - phase_started
    except BaseException as error:
        primary_error = error
        if output:
            error.add_note(
                "Structured diagnostics are available in integration-performance.json"
            )
        else:
            try:
                diagnostics = await environment.logs()
            except Exception as diagnostic_error:
                error.add_note(
                    f"Unable to collect Docker Compose diagnostics: {diagnostic_error}"
                )
            else:
                error.add_note(diagnostics)
        raise
    finally:
        if output:
            failure_artifact: dict[str, object] = {}
            if primary_error is not None:
                try:
                    failure_artifact = await environment.failure_artifact()
                except Exception:
                    print("Unable to collect incident diagnostics", flush=True)
            try:
                (Path(output) / "integration-performance.json").write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "outcome": "failed"
                            if primary_error is not None
                            else "passed",
                            "phases_seconds": phases,
                            "elapsed_seconds": sum(phases.values()),
                            "wall_seconds_before_teardown": monotonic()
                            - started,
                            "diagnostics": failure_artifact,
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            except OSError:
                print(
                    "Unable to publish integration performance diagnostics",
                    flush=True,
                )
        try:
            await environment.close()
        except Exception as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(
                "Unable to clean up Docker Compose environment: "
                f"{cleanup_error}"
            )
