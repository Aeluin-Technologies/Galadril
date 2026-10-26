"""Contracts for deterministic E2E orchestration helpers."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import signal
import ssl
import sys
import tarfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace

import authentication as e2e_authentication
import clients as e2e_clients
import environment as e2e_environment
import httpx
import pytest
from assertions import complete_tempo_trace, eventually, tempo_service_names
from clients import (
    VISION_RUNTIME_READY_TIMEOUT_SECONDS,
    _vision_consumer_groups_have_members,
    _vision_consumers_ready,
    lineage_statuses_complete,
    pipeline_execution_failure,
    statuses_by_step,
)
from environment import (
    PIPELINE_LIFECYCLE_TIMEOUT_SECONDS,
    CommandFailure,
    ComposeEnvironment,
    _run,
    configuration_archive,
    image_loader_command,
    pipeline_environment,
    runfile,
)

E2E_COMPOSE = Path(__file__).parent / "environment" / "compose.yaml"
E2E_CONNECTORS = Path(__file__).parent / "fixtures" / "connectors.yaml"
E2E_OTEL_COLLECTOR = Path(__file__).parent / "fixtures" / "otel-collector.yaml"


def _authentication_response(
    headers: dict[str, str], *, path: str = "/auth/start", status: int = 200
) -> httpx.Response:
    return httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("GET", "https://dashboard.invalid" + path),
    )


def test_authentication_budget_merges_only_current_authentication_windows() -> (
    None
):
    """Out-of-order responses cannot replenish a shared source-IP budget."""
    budget = e2e_authentication.AuthenticationRateBudget()

    async def observe(
        reset: str, remaining: str, path: str = "/auth/start"
    ) -> None:
        await budget.observe(
            _authentication_response(
                {
                    "X-RateLimit-Reset": reset,
                    "X-RateLimit-Remaining": remaining,
                },
                path=path,
            )
        )

    async def exercise() -> None:
        await observe("2000", "24")
        await observe("2000", "40")
        assert budget.remaining_requests == 24
        await observe("2000", "10")
        await observe("1000", "0")
        assert budget.reset_at_ms == 2000
        assert budget.remaining_requests == 10
        await observe("3000", "49")
        assert budget.reset_at_ms == 3000
        assert budget.remaining_requests == 49
        await observe("4000", "0", "/login")
        assert budget.reset_at_ms == 3000
        assert budget.remaining_requests == 49

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "headers",
    (
        {},
        {"X-RateLimit-Reset": "3000"},
        {"X-RateLimit-Remaining": "0"},
        {"X-RateLimit-Reset": "invalid", "X-RateLimit-Remaining": "0"},
        {"X-RateLimit-Reset": "3000", "X-RateLimit-Remaining": "NaN"},
        {"X-RateLimit-Reset": "3000", "X-RateLimit-Remaining": "-1"},
        {"X-RateLimit-Reset": "-1", "X-RateLimit-Remaining": "0"},
        {"X-RateLimit-Reset": "3000", "X-RateLimit-Remaining": "1.5"},
    ),
)
def test_authentication_budget_keeps_valid_state_on_missing_or_invalid_headers(
    headers: dict[str, str],
) -> None:
    budget = e2e_authentication.AuthenticationRateBudget()

    async def exercise() -> None:
        await budget.observe(
            _authentication_response(
                {
                    "X-RateLimit-Reset": "2000",
                    "X-RateLimit-Remaining": "12",
                }
            )
        )
        await budget.observe(_authentication_response(headers))

    asyncio.run(exercise())
    assert budget.reset_at_ms == 2000
    assert budget.remaining_requests == 12


@pytest.mark.parametrize(
    "remaining,reset,now,expected",
    (
        (30, 61000, 1.0, []),
        (29, 61000, 1.0, [60.1]),
        (0, 2000, 1.0, [1.1]),
        (0, 1000, 2.0, []),
    ),
)
def test_authentication_budget_reserves_a_complete_login_before_sending_requests(
    remaining: int,
    reset: int,
    now: float,
    expected: list[float],
) -> None:
    budget = e2e_authentication.AuthenticationRateBudget()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def exercise() -> None:
        await budget.observe(
            _authentication_response(
                {
                    "X-RateLimit-Reset": str(reset),
                    "X-RateLimit-Remaining": str(remaining),
                }
            )
        )
        await budget.reserve(clock=lambda: now, sleep=sleep)

    asyncio.run(exercise())
    assert sleeps == pytest.approx(expected)
    assert budget.remaining_requests == (30 if remaining == 30 else None)


def test_authentication_budget_rejects_an_unbounded_server_reset_deadline() -> (
    None
):
    budget = e2e_authentication.AuthenticationRateBudget()
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def exercise() -> None:
        await budget.observe(
            _authentication_response(
                {
                    "X-RateLimit-Reset": "61001",
                    "X-RateLimit-Remaining": "0",
                }
            )
        )
        with pytest.raises(AssertionError, match="rate limit reset"):
            await budget.reserve(clock=lambda: 1.0, sleep=sleep)

    asyncio.run(exercise())
    assert sleeps == []
    assert budget.remaining_requests == 0


@pytest.mark.parametrize("throttled", (False, True))
def test_authentication_login_observes_every_auth_response_and_never_retries_429(
    monkeypatch: pytest.MonkeyPatch,
    throttled: bool,
) -> None:
    """The transport hook accounts for the final session check as well as redirects."""
    requests: list[str] = []
    claims = {
        "sub": "actor",
        "tenant_id": "tenant",
        "token_use": "access",
        "acr": "urn:galadril:webauthn:uv",
        "mfa_generation": 0,
        "iss": "https://aeluin.gravitalia.com",
        "aud": "galadril",
    }
    token = (
        "header."
        + base64.urlsafe_b64encode(json.dumps(claims).encode())
        .decode()
        .rstrip("=")
        + ".signature"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        headers = {
            "X-RateLimit-Reset": "1000",
            "X-RateLimit-Remaining": str(50 - len(requests)),
        }
        if throttled:
            return httpx.Response(429, headers=headers)
        if request.url.path == "/auth/start":
            headers.update(
                {
                    "Location": "/",
                    "Set-Cookie": f"__Host-galadril-token={token}; Secure; Path=/",
                }
            )
            return httpx.Response(303, headers=headers)
        assert request.url.path == "/auth/session"
        return httpx.Response(200, headers=headers)

    original_client = httpx.AsyncClient

    def client(
        *,
        verify: ssl.SSLContext,
        timeout: float,
        event_hooks: dict[
            str, list[Callable[[httpx.Response], Awaitable[None]]]
        ],
    ) -> httpx.AsyncClient:
        return original_client(
            verify=verify,
            timeout=timeout,
            event_hooks=event_hooks,
            transport=httpx.MockTransport(respond),
        )

    async def private(
        _url: str, _body: Mapping[str, object] | None, _method: str
    ) -> dict[str, object]:
        raise AssertionError("Unexpected private provisioning")

    async def provision(
        _session: e2e_authentication.AuthenticationSessions,
        _user: str,
        _tenant: str,
    ) -> tuple[str, str]:
        return "actor@example.invalid", "test-password"

    monkeypatch.setattr(e2e_authentication.httpx, "AsyncClient", client)
    monkeypatch.setattr(
        e2e_authentication.AuthenticationSessions, "provision", provision
    )
    session = e2e_authentication.AuthenticationSessions(
        private,
        "https://dashboard.invalid",
        "unused",
        ssl.create_default_context(),
    )

    async def exercise() -> None:
        if throttled:
            with pytest.raises(AssertionError, match="HTTP 429"):
                await session.login("actor", "tenant")
        else:
            assert await session.login("actor", "tenant") == token

    asyncio.run(exercise())
    assert requests == (
        ["/auth/start"] if throttled else ["/auth/start", "/auth/session"]
    )
    assert session.rate_budget.remaining_requests == 50 - len(requests)


def test_pipeline_fixture_retains_the_production_hydra_contract() -> None:
    with tarfile.open(fileobj=io.BytesIO(configuration_archive())) as archive:
        config = archive.extractfile("proxy/envoy.yaml")
        assert config is not None
        source = config.read().decode()
        assert "remote_jwks:" in source
        assert "issuer: https://aeluin.gravitalia.com" in source
        assert "local_jwks:" not in source


@pytest.mark.parametrize(
    "errors,status,accepted",
    (
        (
            [
                {
                    "message": "Wording can change",
                    "extensions": {"code": "FORBIDDEN"},
                    "path": ["administration", "createRole"],
                }
            ],
            200,
            True,
        ),
        ([{"message": "Authorization denied"}], 200, False),
        (
            [
                {
                    "extensions": {"code": "INTERNAL_ERROR"},
                    "path": ["administration", "createRole"],
                }
            ],
            200,
            False,
        ),
        (
            [
                {
                    "extensions": {"code": "FORBIDDEN"},
                    "path": ["administration", "createUser"],
                }
            ],
            200,
            False,
        ),
        (
            [
                {
                    "extensions": {"code": "FORBIDDEN"},
                    "path": ["administration", "createRole"],
                }
            ],
            503,
            False,
        ),
        (
            [
                {
                    "extensions": {"code": "FORBIDDEN"},
                    "path": ["administration", "createRole"],
                }
            ],
            401,
            False,
        ),
        (
            [
                {
                    "extensions": {"code": "FORBIDDEN"},
                    "path": ["administration", "createRole"],
                },
                {
                    "extensions": {"code": "INTERNAL_ERROR"},
                    "path": ["administration", "createRole"],
                },
            ],
            200,
            False,
        ),
        ([], 200, False),
        (["FORBIDDEN"], 200, False),
    ),
)
def test_graphql_forbidden_requires_public_code_exact_path_and_successful_transport(
    errors: list[object],
    status: int,
    accepted: bool,
) -> None:
    """An outage, unrelated field failure or legacy text cannot prove access denial."""

    def check() -> None:
        error = e2e_clients.GraphQLOperationError(errors, status)
        assert isinstance(error, AssertionError)
        error.require_forbidden(("administration", "createRole"))

    if accepted:
        check()
    else:
        with pytest.raises(AssertionError):
            check()


@pytest.mark.parametrize("status", (200, 503))
def test_gateway_client_preserves_structured_graphql_error_contract(
    status: int,
) -> None:
    errors = [
        {
            "message": "Safe public message",
            "extensions": {"code": "FORBIDDEN"},
            "path": ["administration", "createRole"],
        }
    ]

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    status, json={"data": None, "errors": errors}
                )
            )
        ) as client:
            gateway = object.__new__(e2e_clients.GatewayClient)
            gateway._client = client
            with pytest.raises(e2e_clients.GraphQLOperationError) as denied:
                await gateway.execute(
                    "fixture-token",
                    'mutation { administration { createRole(roleName: "role") } }',
                )
            assert "GraphQL operation failed" in str(denied.value)
            assert "Safe public message" in str(denied.value)
            if status == 200:
                denied.value.require_forbidden(("administration", "createRole"))
            else:
                with pytest.raises(AssertionError):
                    denied.value.require_forbidden(
                        ("administration", "createRole")
                    )

    asyncio.run(exercise())


def test_orchestration_uses_the_declared_compose_tool() -> None:
    """Prevents remote executor versions from changing the deployment graph."""
    environment = ComposeEnvironment()
    assert Path(environment._command[0]).name == "docker-compose"
    assert Path(environment._command[0]).is_file()


def test_proxy_volume_contains_its_own_server_credentials() -> None:
    """Avoids overlapping mounts and keeps private keys out of app volumes."""
    with tarfile.open(fileobj=io.BytesIO(configuration_archive())) as archive:
        for directory in ("identity", "public"):
            for filename in ("cert.pem", "key.pem", "ca.pem"):
                credential = archive.getmember(f"proxy/{directory}/{filename}")
                assert credential.isfile()
                assert credential.mode == 0o444
        assert not any(
            name.endswith("key.pem")
            and not name.startswith(("proxy/", "hydra/"))
            for name in archive.getnames()
        )


def test_application_ports_are_published_only_by_proxies() -> None:
    """Rejects Docker's incompatible port publishing and shared network mode."""
    source = E2E_COMPOSE.read_text()
    for name in ("gateway", "registry", "intake", "vision", "scribe"):
        block = re.split(
            r"\n  [^ ]", source.split(f"\n  {name}:\n", 1)[1], maxsplit=1
        )[0]
        assert "ports:" not in block
        assert "network_mode: service:api-proxy" in block


def test_e2e_inherits_the_shipped_compose_services() -> None:
    source = E2E_COMPOSE.read_text()
    for name in (
        "gateway",
        "registry",
        "intake",
        "vision",
        "scribe",
        "api-proxy",
        "tempo",
    ):
        block = re.split(
            r"\n  [^ ]", source.split(f"\n  {name}:\n", 1)[1], maxsplit=1
        )[0]
        assert "extends:" in block


def test_e2e_target_reserves_sufficient_remote_resources_and_time() -> None:
    """Keeps Docker, Ray, and infrastructure viable on remote executors."""
    build = runfile("tests/e2e/BUILD.bazel").read_text(encoding="utf-8")
    testing = runfile("build/testing.bzl").read_text(encoding="utf-8")

    lifecycle = re.search(
        r'py_test\(\s*name = "pipeline_lifecycle_test",(?P<attributes>.*?)\n\)',
        build,
        re.DOTALL,
    )
    assert lifecycle is not None
    assert 'timeout = "eternal"' in lifecycle.group("attributes")
    assert (
        "exec_properties = DOCKER_E2E_TEST_EXEC_PROPERTIES"
        in lifecycle.group("attributes")
    )
    assert '"test.EstimatedComputeUnits": "6"' in testing
    assert PIPELINE_LIFECYCLE_TIMEOUT_SECONDS == 4800.0


def test_vision_runtime_gets_a_cold_start_budget() -> None:
    """Allows embedded Ray to register and import its actors once."""
    assert VISION_RUNTIME_READY_TIMEOUT_SECONDS == 300.0


def test_ci_fails_on_the_first_test_failure() -> None:
    """Prevents a retry from turning an infrastructure failure into green CI."""
    configuration = runfile(".bazelrc").read_text(encoding="utf-8")
    attempts = re.findall(
        r"^test(?::ci)?\s+--flaky_test_attempts=(\d+)\s*$",
        configuration,
        re.MULTILINE,
    )
    assert attempts == ["1"]


def test_vision_kafka_probe_has_a_transport_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents librdkafka cleanup from trapping readiness diagnostics."""
    configuration: dict[str, object] = {}

    class Future:
        def result(self, timeout: float | None = None) -> object:
            assert timeout == 3.0
            return SimpleNamespace(members=(object(),))

    class Admin:
        def __init__(self, settings: dict[str, object]) -> None:
            configuration.update(settings)

        def describe_consumer_groups(
            self, group_ids: list[str], *, request_timeout: float
        ) -> dict[str, Future]:
            assert request_timeout == 3.0
            return {group_id: Future() for group_id in group_ids}

    monkeypatch.setattr(e2e_clients, "AdminClient", Admin)

    assert _vision_consumers_ready() is True
    assert configuration == {
        "bootstrap.servers": "127.0.0.1:19092",
        "request.timeout.ms": 3000,
        "socket.timeout.ms": 3000,
    }


def test_operations_require_a_previous_real_login() -> None:
    """Prevents fixture callers from silently manufacturing an identity."""
    with pytest.raises(RuntimeError, match="has not authenticated"):
        e2e_clients.authenticated_token("never-authenticated")


def test_e2e_uses_hydra_discovery_instead_of_a_fixture_signing_key() -> None:
    """Keeps end-to-end authentication on the production verification path."""
    with tarfile.open(fileobj=io.BytesIO(configuration_archive())) as archive:
        config = archive.extractfile("proxy/envoy.yaml")
        assert config is not None
        content = config.read().decode()
        assert "remote_jwks:" in content
        assert "local_jwks:" not in content
        assert "proxy/jwks.json" not in archive.getnames()


def test_lineage_statuses_are_grouped_without_losing_transitions() -> None:
    """Makes lifecycle failures readable without weakening exact assertions."""
    events = [
        {"step": "infer", "status": "accepted"},
        {"step": "infer", "status": "completed"},
        {"step": "sink", "status": "running"},
    ]

    assert statuses_by_step(events) == {
        "infer": {"accepted", "completed"},
        "sink": {"running"},
    }


def test_lineage_waits_for_all_required_statuses() -> None:
    """Terminal events may arrive before other partitions are consumed."""
    required = {
        "infer": frozenset({"accepted", "running", "completed"}),
        "resolve": frozenset({"running", "completed"}),
        "sink": frozenset({"running", "completed"}),
    }
    observed = {
        "infer": {"completed"},
        "resolve": {"completed"},
        "sink": {"completed"},
    }

    assert not lineage_statuses_complete(observed, required)
    observed["infer"].update({"accepted", "running"})
    observed["resolve"].add("running")
    observed["sink"].add("running")
    assert lineage_statuses_complete(observed, required)


def test_eventually_bounds_one_stuck_observation() -> None:
    """Ensures one blocked probe cannot bypass the polling deadline."""

    async def blocked() -> None:
        await asyncio.Event().wait()

    async def exercise() -> None:
        with pytest.raises(
            AssertionError, match="Timed out waiting for blocked probe"
        ):
            await asyncio.wait_for(
                eventually(
                    blocked,
                    timeout_seconds=0.01,
                    description="blocked probe",
                ),
                timeout=0.1,
            )

    asyncio.run(exercise())


def test_eventually_propagates_terminal_pipeline_failure() -> None:
    """A durable step failure must not consume the entire lifecycle budget."""

    class TerminalPipelineFailure(RuntimeError):
        """Represents work that cannot recover without a new publication."""

    async def failed() -> None:
        raise TerminalPipelineFailure("empirical_resolve: invalid candidate")

    async def exercise() -> None:
        with pytest.raises(TerminalPipelineFailure, match="empirical_resolve"):
            await eventually(
                failed,
                timeout_seconds=1.0,
                description="empirical pipeline",
                abort_on=(TerminalPipelineFailure,),
            )

    asyncio.run(exercise())


def test_pipeline_failure_preserves_step_and_error() -> None:
    """Makes terminal actor failures actionable instead of timing out opaquely."""
    assert (
        pipeline_execution_failure(
            [("infer", "failed", "model fixture unavailable")]
        )
        == "infer: model fixture unavailable"
    )
    assert pipeline_execution_failure([("infer", "completed", None)]) is None


def test_tempo_service_names_read_otlp_resource_attributes() -> None:
    """Ensures trace assertions use Tempo's OTLP response structure."""
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "galadril-vision"},
                        }
                    ]
                }
            }
        ]
    }

    assert tempo_service_names(trace) == {"galadril-vision"}


def test_tempo_trace_waits_for_every_required_service() -> None:
    """A queryable trace can precede export of its other service spans."""
    partial = {
        "batches": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "galadril-vision"},
                        }
                    ]
                }
            }
        ]
    }
    required = frozenset({"galadril-intake", "galadril-vision"})

    assert complete_tempo_trace(None, required) is None
    assert complete_tempo_trace(partial, required) is None

    complete = {
        "batches": [
            *partial["batches"],
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "galadril-intake"},
                        }
                    ]
                }
            },
        ]
    }
    assert complete_tempo_trace(complete, required) is complete


def test_image_loader_command_does_not_require_executable_runfile(
    tmp_path: Path,
) -> None:
    """Keeps remotely materialized OCI launchers usable without mode bits."""
    loader = tmp_path / "load_gateway.sh"
    loader.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    loader.chmod(0o644)

    assert image_loader_command(loader) == ("/bin/bash", str(loader))


def test_application_image_loads_have_a_dedicated_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps large OCI imports independent of ordinary command deadlines."""
    deadlines: list[float | None] = []

    async def record_run(_command: object, **kwargs: object) -> str:
        deadline = kwargs.get("timeout_seconds")
        deadlines.append(deadline if isinstance(deadline, float) else None)
        return ""

    monkeypatch.setattr(e2e_environment, "_run", record_run)
    monkeypatch.setattr(e2e_environment, "runfile", Path)

    async def exercise() -> None:
        await ComposeEnvironment().load_images()

    asyncio.run(exercise())
    assert deadlines == [600.0, 600.0, 600.0, 1200.0, 600.0, 600.0, 600.0]


def test_compose_startup_includes_cold_image_pull_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allows clean remote executors to pull dependencies before startup."""
    deadlines: list[float | None] = []

    async def record_run(_command: object, **kwargs: object) -> str:
        deadline = kwargs.get("timeout_seconds")
        deadlines.append(deadline if isinstance(deadline, float) else None)
        return ""

    monkeypatch.setattr(e2e_environment, "_run", record_run)

    asyncio.run(ComposeEnvironment().start_core())
    assert deadlines == [1200.0, 1200.0]


@pytest.mark.parametrize("state", ["restarting 1", "exited 0", "running 1"])
def test_vision_startup_rejects_restarts(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    async def record_run(command: tuple[str, ...], **_kwargs: object) -> str:
        return state if command[:2] == ("docker", "inspect") else "vision-id"

    monkeypatch.setattr(e2e_environment, "_run", record_run)
    with pytest.raises(AssertionError, match="Vision restarted or exited"):
        asyncio.run(ComposeEnvironment().assert_vision_did_not_restart())


def test_vision_pending_requires_an_otlp_startup_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = "unrelated log"

    async def record_run(command: tuple[str, ...], **_kwargs: object) -> str:
        if command[:2] == ("docker", "inspect"):
            return "running 0"
        return logs if "logs" in command else "vision-id"

    monkeypatch.setattr(e2e_environment, "_run", record_run)

    async def exercise() -> None:
        nonlocal logs
        environment = ComposeEnvironment()
        assert not await environment.vision_waiting_for_publication()
        logs = "event.name: Str(pipeline.publication.pending)"
        assert await environment.vision_waiting_for_publication()

    asyncio.run(exercise())


def test_compose_diagnostics_and_cleanup_have_recovery_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allows an overloaded daemon to report and remove failed services."""
    deadlines: list[float | None] = []

    async def record_run(_command: object, **kwargs: object) -> str:
        deadline = kwargs.get("timeout_seconds")
        deadlines.append(deadline if isinstance(deadline, float) else None)
        return ""

    monkeypatch.setattr(e2e_environment, "_run", record_run)
    environment = ComposeEnvironment()

    async def exercise() -> None:
        await environment.logs()
        await environment.close()

    asyncio.run(exercise())
    assert deadlines == [90.0] * 9


def test_compose_diagnostics_expose_outbox_worker_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps terminal E2E failures actionable without runner access."""
    commands: list[tuple[str, ...]] = []

    async def record_run(command: object, **_kwargs: object) -> str:
        assert isinstance(command, tuple)
        commands.append(command)
        return ""

    monkeypatch.setattr(e2e_environment, "_run", record_run)

    asyncio.run(ComposeEnvironment().logs())

    rendered = "\n".join(" ".join(command) for command in commands)
    assert "attempts" in rendered
    assert "next_retry_at" in rendered
    assert "rolbypassrls" in rendered
    assert "application_name" in rendered
    assert "find /tmp/ray" not in rendered
    assert "worker-*.err" in rendered
    assert commands[-1][-2:] == ("1000", "otel-collector")


def test_vision_database_probe_sets_transport_and_statement_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents an overloaded Vision stack from trapping readiness polling."""
    connection_options: dict[str, object] = {}
    executed_queries: list[str] = []

    class Cursor:
        async def fetchone(self) -> tuple[str, str, str, bool, bool]:
            return (
                "entity_states",
                "pipeline_executions",
                "authz_outbox",
                True,
                True,
            )

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(
            self,
            _exception_type: object,
            _exception: object,
            _traceback: object,
        ) -> None:
            return None

        async def execute(
            self, query: str, _parameters: tuple[str, ...]
        ) -> Cursor:
            executed_queries.append(query)
            return Cursor()

    async def connect(_dsn: str, **kwargs: object) -> Connection:
        connection_options.update(kwargs)
        return Connection()

    monkeypatch.setattr(e2e_clients.psycopg.AsyncConnection, "connect", connect)

    assert asyncio.run(e2e_clients.vision_database_ready()) is True
    assert connection_options == {
        "connect_timeout": 3,
        "options": "-c statement_timeout=3000",
    }
    query = "\n".join(executed_queries)
    for extension in ("age", "postgis", "timescaledb", "vector"):
        assert extension in query
    assert "ag_catalog.ag_graph" in query
    assert "graphid = namespace::oid" in query


@pytest.mark.parametrize("closed_bucket", [False, True])
def test_causal_cohort_waits_for_the_last_event_bucket_to_close(
    monkeypatch: pytest.MonkeyPatch, closed_bucket: bool
) -> None:
    class Cursor:
        def __init__(self, query: str) -> None:
            self.query = query

        async def fetchall(self) -> list[tuple[str, str, str, int]]:
            return [
                ("alpha", "alpha-entity", "1", 30),
                ("decoy", "decoy-entity", "2", 6),
            ]

        async def fetchone(self) -> tuple[object, ...] | None:
            if "SELECT step, error" in self.query:
                return None
            if "pipeline_executions" in self.query:
                return (36,)
            if "authz_outbox" in self.query:
                return (0,)
            return (30, 0.0, closed_bucket)

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(
            self, query: str, parameters: tuple[str, ...]
        ) -> Cursor:
            if "JOIN entity_embeddings" in query:
                assert "MAX(state.event_time)" in query
                assert "date_trunc('second', clock_timestamp())" in query
            return Cursor(query)

    async def connect(dsn: str) -> Connection:
        return Connection()

    monkeypatch.setattr(e2e_clients.psycopg.AsyncConnection, "connect", connect)
    assert asyncio.run(e2e_clients.read_causal_cohort()) == (
        "alpha-entity" if closed_bucket else None
    )


def test_runfile_resolution_ignores_environment_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prevents caller-controlled environment paths from selecting test input."""
    monkeypatch.setenv("RUNFILES_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_SRCDIR", str(tmp_path))

    assert runfile("tests/e2e/environment/compose.yaml").samefile(E2E_COMPOSE)


def test_environment_uses_pinned_rustfs_with_private_storage() -> None:
    """Keeps the same immutable S3 provider in development and Linux E2E."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert (
        "image: rustfs/rustfs:1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c"
        in compose
    )
    assert "rustfs-data:/data" in compose
    assert 'RUSTFS_NOTIFY_ENABLE: "true"' in compose
    assert "RUSTFS_NOTIFY_WEBHOOK_ENDPOINT_PRIMARY:" in compose
    assert "arn:rustfs:sqs:us-east-1:primary:webhook" in compose
    assert "  s3-notifications:" in compose
    assert "service: minio" not in compose


def test_environment_uses_image_native_postgres_data_directory() -> None:
    """Keeps the non-root database image able to initialize its volume."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "PGDATA: /home/postgres/pgdata/data" in compose
    assert "postgres-data:/home/postgres/pgdata/data" in compose
    database = runfile("infrastructure/docker/database.yaml").read_text()
    assert '"listen_addresses=*"' in database


def test_environment_uses_tempo_three_configuration() -> None:
    """Exercises the deployed configuration instead of a divergent fixture."""
    with tarfile.open(fileobj=io.BytesIO(configuration_archive())) as archive:
        tempo = archive.extractfile("observability/tempo.yaml")
        assert tempo is not None
        assert (
            tempo.read()
            == runfile("infrastructure/observability/tempo.yaml").read_bytes()
        )


def test_vision_has_dedicated_shared_memory_for_embedded_ray() -> None:
    """Prevents Ray workers from exhausting Docker's 64 MiB default shm."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    vision = compose.split("\n  vision:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert 'shm_size: "6gb"' in vision
    assert 'RAY_memory_usage_threshold: "0.99"' in vision


def test_vision_uses_a_stable_grpc_resolver_for_embedded_ray() -> None:
    """Prevents c-ares startup races from killing Ray's first node."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    vision = compose.split("\n  vision:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    assert "GRPC_DNS_RESOLVER: native" in vision
    assert 'RAY_enable_agent_fate_share: "0"' in vision
    assert 'RAY_raylet_start_wait_time_s: "60"' in vision


def test_vision_readiness_requires_every_runtime_consumer_group() -> None:
    """Prevents database migrations from masquerading as broker readiness."""
    assert not _vision_consumer_groups_have_members(
        {
            "galadril-e2e-ingress": 1,
            "galadril-e2e-cpu": 1,
            "galadril-e2e-gpu": 1,
            "galadril-e2e-causal": 0,
        }
    )
    assert _vision_consumer_groups_have_members(
        {
            "galadril-e2e-ingress": 1,
            "galadril-e2e-cpu": 1,
            "galadril-e2e-gpu": 1,
            "galadril-e2e-causal": 1,
        }
    )


def test_environment_preserves_structured_application_log_bodies() -> None:
    """Keeps fatal OTLP diagnostics visible in Bazel failure artifacts."""
    collector = E2E_OTEL_COLLECTOR.read_text(encoding="utf-8")
    assert "verbosity: detailed" in collector
    assert "filter/drop_debug:" in collector
    assert "severity_number < SEVERITY_NUMBER_INFO" in collector


@pytest.mark.parametrize(
    "path",
    (
        E2E_OTEL_COLLECTOR,
        Path("infrastructure/observability/otel-collector.yaml"),
    ),
)
def test_collectors_accept_identity_and_application_traces(path: Path) -> None:
    """Ory exports OTLP over HTTP while the application services use gRPC."""
    collector = path.read_text(encoding="utf-8")
    for protocol, port in (("grpc", 4317), ("http", 4318)):
        assert f"{protocol}:\n        endpoint: 0.0.0.0:{port}" in collector


def test_gateway_signing_keys_are_owned_by_hydra() -> None:
    """Keeps nested JWT fields independent of flat environment mapping."""
    connectors = E2E_CONNECTORS.read_text(encoding="utf-8")
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "jwt:" not in connectors
    assert "key_pem:" not in connectors
    assert "api-proxy:" in compose
    assert "JWT_ISSUER" not in compose
    assert "PUBLIC_KEY_PEM" not in compose


def test_gateway_security_limits_are_explicit_in_bootstrap_fixture() -> None:
    """Keeps denial thresholds deterministic across local and remote runners."""
    connectors = E2E_CONNECTORS.read_text(encoding="utf-8")
    assert "max_body_bytes: 4096" in connectors
    assert "max_graphql_depth: 8" in connectors


def test_environment_mounts_daemon_portable_configuration_volume() -> None:
    """Keeps Docker orchestration independent of daemon host paths."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    assert "${E2E_FIXTURES_DIR}" not in compose
    assert "${E2E_INFRA_DIR}" not in compose
    assert "${E2E_SCHEMAS_DIR}" not in compose
    assert "subpath:" not in compose
    for category in ("application", "observability", "database", "proxy"):
        assert f"source: e2e-{category}" in compose
    assert "external: true" in compose


def test_postgres_healthcheck_waits_for_final_tcp_postmaster() -> None:
    """Prevents dependents from observing the socket-only bootstrap server."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    postgres = compose.split("\n  postgres:\n", 1)[1].split(
        "\n  spicedb-migrate:\n", 1
    )[0]
    assert "pg_isready --quiet --host=127.0.0.1" in postgres
    assert "postmaster.pid" not in postgres


def test_postgres_uses_an_init_process_for_healthcheck_children() -> None:
    """Prevents healthcheck children from being reaped by the postmaster."""
    compose = E2E_COMPOSE.read_text(encoding="utf-8")
    postgres = compose.split("\n  postgres:\n", 1)[1].split(
        "\n  spicedb-migrate:\n", 1
    )[0]
    assert "init: true" in postgres


def test_configuration_archive_contains_required_runfiles() -> None:
    """Ensures the daemon receives every file previously bind-mounted."""
    archive = configuration_archive()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        names = set(bundle.getnames())

    assert {
        "proxy/envoy.yaml",
        "proxy/error-page.lua",
        "application/connectors.yaml",
        "application/s3-notifications.yaml",
        "observability/otel-collector.yaml",
        "observability/tempo.yaml",
        "application/site-packages/e2e_inference_model.py",
        "application/spicedb/schema.zed",
        "database/003-install-extensions.sh",
        "database/004-create-galadril-app.sh",
        "database/010-init-spicedb.sh",
        "database/020-init-galadril-roles.sh",
    } <= names


def test_spicedb_probe_allowed_supports_canonical_resources() -> None:
    """Prevents double-canonicalization when probing pre-escaped SpiceDB object IDs."""
    requests: list[object] = []

    class MockClient:
        async def CheckPermission(self, request: object) -> SimpleNamespace:
            requests.append(request)
            return SimpleNamespace(permissionship=1)

    probe = object.__new__(e2e_clients.SpiceDBProbe)
    probe._client = MockClient()

    async def exercise() -> None:
        await probe.allowed(
            resource_type="raw",
            resource_id="tenant/raw/report.txt",
            permission="view",
            user_id="user_1",
        )
        await probe.allowed(
            resource_type="raw",
            resource_id="tenant/raw/report=2Etxt",
            permission="view",
            user_id="user_1",
            canonical_resource=True,
        )

    asyncio.run(exercise())
    assert len(requests) == 2
    assert requests[0].resource.object_id == "tenant/raw/report=2Etxt"
    assert requests[1].resource.object_id == "tenant/raw/report=2Etxt"


def test_environment_tears_down_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevents leaked containers when Compose fails before yielding."""
    calls: list[str] = []

    async def load_images(_environment: ComposeEnvironment) -> None:
        calls.append("load")

    async def start_core(_environment: ComposeEnvironment) -> None:
        calls.append("start")
        raise RuntimeError("startup failed")

    async def prepare_configuration(_environment: ComposeEnvironment) -> None:
        calls.append("prepare")

    async def close(_environment: ComposeEnvironment) -> None:
        calls.append("close")

    monkeypatch.setattr(ComposeEnvironment, "__init__", lambda _self: None)
    monkeypatch.setattr(ComposeEnvironment, "load_images", load_images)
    monkeypatch.setattr(
        ComposeEnvironment,
        "prepare_configuration",
        prepare_configuration,
    )
    monkeypatch.setattr(ComposeEnvironment, "start_core", start_core)
    monkeypatch.setattr(ComposeEnvironment, "close", close)

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="startup failed"):
            async with pipeline_environment():
                raise AssertionError(
                    "environment yielded after startup failure"
                )

    asyncio.run(exercise())
    assert calls == ["load", "prepare", "start", "close"]


def test_environment_preserves_failure_when_teardown_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keeps the actionable service failure when best-effort cleanup fails."""

    async def succeed(_environment: ComposeEnvironment) -> None:
        return None

    async def start_core(_environment: ComposeEnvironment) -> None:
        raise RuntimeError("service startup failed")

    async def close(_environment: ComposeEnvironment) -> None:
        raise CommandFailure("cleanup failed")

    monkeypatch.setattr(ComposeEnvironment, "__init__", lambda _self: None)
    monkeypatch.setattr(ComposeEnvironment, "load_images", succeed)
    monkeypatch.setattr(ComposeEnvironment, "prepare_configuration", succeed)
    monkeypatch.setattr(ComposeEnvironment, "start_core", start_core)
    monkeypatch.setattr(ComposeEnvironment, "close", close)

    async def exercise() -> RuntimeError:
        try:
            async with pipeline_environment():
                raise AssertionError(
                    "environment yielded after startup failure"
                )
        except RuntimeError as error:
            assert "service startup failed" in str(error)
            return error

    error = asyncio.run(exercise())
    assert len(error.__notes__) == 2
    assert error.__notes__[0].startswith(
        "Unable to collect Docker Compose diagnostics:"
    )
    assert error.__notes__[1] == (
        "Unable to clean up Docker Compose environment: cleanup failed"
    )


def _orchestration_tree_command(directory: Path) -> tuple[str, ...]:
    """Exercises the shell and loader tree without depending on Docker speed."""
    child = (
        "import os, signal, sys, time\n"
        "from pathlib import Path\n"
        "def stop(signum, frame):\n"
        f"    Path({str(directory / 'terminated')!r}).touch()\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(directory / 'ready')!r}).write_text(str(os.getpid()))\n"
        "print('loading fixture layer', flush=True)\n"
        "print('loader diagnostic', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n"
    )
    parent = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(60)\n"
    )
    return (sys.executable, "-c", parent)


def _cleanup_orchestration_child(directory: Path) -> None:
    """Keeps a failing cleanup regression from leaving a sleeping child."""
    ready = directory / "ready"
    if ready.exists() and not (directory / "terminated").exists():
        try:
            os.kill(int(ready.read_text()), signal.SIGKILL)
        except ProcessLookupError:
            # The child may exit between the readiness check and the signal.
            pass


def test_orchestration_timeout_preserves_output_and_terminates_children(
    tmp_path: Path,
) -> None:
    """Keeps loader progress and prevents descendants from retaining pipes."""

    async def exercise() -> None:
        with pytest.raises(
            CommandFailure, match="timed out after 1.00s"
        ) as error:
            await _run(
                _orchestration_tree_command(tmp_path), timeout_seconds=1.0
            )
        assert str(error.value).endswith(
            "loading fixture layer\nloader diagnostic\n"
        )
        assert (tmp_path / "terminated").exists()

    try:
        asyncio.run(exercise())
    finally:
        _cleanup_orchestration_child(tmp_path)


def test_orchestration_cancellation_terminates_children(tmp_path: Path) -> None:
    """Prevents a cancelled E2E owner from orphaning Docker or tar processes."""

    async def ready() -> bool | None:
        return True if (tmp_path / "ready").exists() else None

    async def exercise() -> None:
        task = asyncio.create_task(_run(_orchestration_tree_command(tmp_path)))
        try:
            await eventually(
                ready, timeout_seconds=5.0, description="orchestration child"
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert (tmp_path / "terminated").exists()

    try:
        asyncio.run(exercise())
    finally:
        _cleanup_orchestration_child(tmp_path)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))


def test_archived_storage_endpoint_resolves_a_declared_compose_service() -> (
    None
):
    """Checks the deployed archive rather than only Compose's provider name."""
    with tarfile.open(fileobj=io.BytesIO(configuration_archive())) as archive:
        content = archive.extractfile("application/connectors.yaml")
        assert content is not None
        configuration = content.read().decode()
    match = re.search(
        r'  s3:\n    endpoint: "http://([^:/]+):9000"', configuration
    )
    assert match is not None
    declared = set(
        re.findall(r"^  ([a-z0-9-]+):$", E2E_COMPOSE.read_text(), re.MULTILINE)
    )
    assert match.group(1) in declared, (
        "S3 DNS must resolve the actual storage service"
    )


def test_authentication_readiness_requires_the_public_studio_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def identity_request(
        _self: ComposeEnvironment,
        url: str,
        _body: Mapping[str, object] | None,
        method: str,
    ) -> Mapping[str, object]:
        assert method == "GET"
        calls.append(url)
        if url == "https://127.0.0.1:8080/healthz":
            raise CommandFailure("Public Studio route returned HTTP 503")
        return {"status": "ok"}

    monkeypatch.setattr(
        ComposeEnvironment, "identity_request", identity_request
    )
    with pytest.raises(CommandFailure, match="Public Studio"):
        asyncio.run(ComposeEnvironment().authentication_ready())
    assert calls[-1] == "https://127.0.0.1:8080/healthz"


def test_authentication_readiness_rejects_an_unexpected_public_health_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def identity_request(
        _self: ComposeEnvironment,
        url: str,
        _body: Mapping[str, object] | None,
        method: str,
    ) -> Mapping[str, object]:
        return (
            {"status": "starting"}
            if url.startswith("https:")
            else {"status": "ok"}
        )

    monkeypatch.setattr(
        ComposeEnvironment, "identity_request", identity_request
    )
    assert asyncio.run(ComposeEnvironment().authentication_ready()) is None


def test_authentication_readiness_accepts_the_complete_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def identity_request(
        _self: ComposeEnvironment,
        url: str,
        _body: Mapping[str, object] | None,
        method: str,
    ) -> Mapping[str, object]:
        assert method == "GET" and _body is None
        calls.append(url)
        return {"status": "ok"}

    monkeypatch.setattr(
        ComposeEnvironment, "identity_request", identity_request
    )
    assert asyncio.run(ComposeEnvironment().authentication_ready()) is True
    assert calls == [
        "http://127.0.0.1:8081/healthz",
        "http://127.0.0.1:4434/health/ready",
        "http://127.0.0.1:4445/health/ready",
        "https://127.0.0.1:8080/healthz",
    ]
