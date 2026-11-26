"""Real Gateway upload, derived access and distributed trace assertions."""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from assertions import (
    complete_tempo_trace,
    eventually,
    require_mapping,
    require_sequence,
    require_tempo_ray_spans,
    tempo_service_names,
)
from clients import (
    OUTSIDER_ID,
    TENANT_ID,
    UPLOADER_ID,
    DerivedPipelineState,
    GatewayClient,
    PipelineExecutionError,
    authenticated_token,
    canonical_spicedb_object_id,
    consume_dead_letter,
    consume_lineage,
    read_pipeline_state,
    read_s3_object,
    read_tempo_trace,
    statuses_by_step,
    upload_presigned,
)
from galadril_pipeline.telemetry_contract import (
    EVENT_VISION_STEP_FAILED,
    ErrorCode,
)
from gateway_scenario import GatewayScenario, _field, _string

_EXPECTED_STEPS = frozenset({"infer", "resolve", "sink"})
_EXPECTED_LINEAGE_STATUSES = {
    "infer": frozenset({"accepted", "running", "completed"}),
    "resolve": frozenset({"running", "completed"}),
    "sink": frozenset({"running", "completed"}),
}
_GATEWAY_TRACE_ID = "87e27b4f8c1245938b79ca2831a4f385"


async def _gateway_search(
    gateway: GatewayClient, token: str, entity_id: str
) -> Sequence[object]:
    """Searches one derived entity through the public access API."""
    data = await gateway.execute(
        token,
        """
        query Search($input: StructuredSearchInput!) {
          structuredSearch(input: $input) { kind entityId payload }
        }
        """,
        {"input": {"entityId": entity_id, "limit": 10}},
    )
    return require_sequence(data.get("structuredSearch"), "structuredSearch")


async def _tempo_trace_with_services(
    trace_id: str,
    required_services: frozenset[str],
    required_ray_commands: frozenset[str] = frozenset(),
) -> dict[str, object] | None:
    """Waits until required service spans and Ray relationships are queryable."""
    trace = await read_tempo_trace(trace_id)
    if trace is not None and (
        missing := required_services - tempo_service_names(trace)
    ):
        raise AssertionError(f"Tempo trace {trace_id} lacks {sorted(missing)}")
    complete = complete_tempo_trace(trace, required_services)
    if complete is not None and required_ray_commands:
        require_tempo_ray_spans(complete, required_ray_commands)
    return complete


async def _assert_upload_security_boundaries(
    gateway: GatewayClient,
    staging_key: str,
) -> None:
    """Rejects confused-deputy and path attacks before S3 promotion."""
    mutation = """
        mutation CompleteUpload($stagingKey: String!, $targetName: String!) {
          completeUpload(stagingKey: $stagingKey, targetName: $targetName)
        }
    """
    for token, key, name in (
        (
            authenticated_token(UPLOADER_ID, tenant_id="isolated_e2e_tenant"),
            staging_key,
            "stolen.txt",
        ),
        (
            authenticated_token(UPLOADER_ID),
            f"other_tenant/{UPLOADER_ID}/stolen",
            "stolen.txt",
        ),
        (authenticated_token(UPLOADER_ID), staging_key, "../escaped.txt"),
        (authenticated_token(UPLOADER_ID), staging_key, "nested/escaped.txt"),
    ):
        with pytest.raises(AssertionError, match="GraphQL operation failed"):
            await gateway.execute(
                token,
                mutation,
                {"stagingKey": key, "targetName": name},
            )


async def _upload_text(
    scenario: GatewayScenario,
    content: bytes,
    target_name: str,
    *,
    check_upload_boundaries: bool = False,
) -> str:
    """Promotes a test input through the real Gateway and object store."""
    gateway, uploader_token = scenario.gateway, scenario.uploader_token
    print("E2E stage: uploading through Gateway", flush=True)
    upload_data = await gateway.execute(
        uploader_token,
        """
        mutation RequestUpload {
          requestStagingUpload { uploadUrl stagingKey }
        }
        """,
        trace_id=_GATEWAY_TRACE_ID,
    )
    staging = _field(upload_data, "requestStagingUpload")
    staging_url = _string(staging, "uploadUrl")
    staging_key = _string(staging, "stagingKey")
    await upload_presigned(
        staging_url,
        content,
    )
    if check_upload_boundaries:
        await _assert_upload_security_boundaries(gateway, staging_key)
    complete_data = await gateway.execute(
        uploader_token,
        """
        mutation CompleteUpload(
          $stagingKey: String!, $targetName: String!
        ) {
          completeUpload(
            stagingKey: $stagingKey,
            targetName: $targetName
          )
        }
        """,
        {
            "stagingKey": staging_key,
            "targetName": target_name,
        },
        trace_id=_GATEWAY_TRACE_ID,
    )
    nonce = staging_key.rsplit("/", 1)[-1]
    object_key = f"{TENANT_ID}/raw/default/{nonce}/{target_name}"
    assert complete_data.get("completeUpload") == object_key

    return object_key


async def ingest_record(
    scenario: GatewayScenario, *, check_upload_boundaries: bool = False
) -> tuple[DerivedPipelineState, str, str]:
    """Waits for the committed state, draining the authorization outbox."""
    spicedb = scenario.spicedb
    object_key = await _upload_text(
        scenario,
        b"Galadril E2E record crossing the complete pipeline.\n",
        "e2e-record.txt",
        check_upload_boundaries=check_upload_boundaries,
    )

    s3_evidence = await read_s3_object(object_key)
    assert s3_evidence is not None
    assert bool(s3_evidence.metadata.get("authz-gol-admission"))
    assert {
        key: value
        for key, value in s3_evidence.metadata.items()
        if key != "authz-gol-admission"
    } == {
        "tenant": TENANT_ID,
        "owner": UPLOADER_ID,
        "authz-origin": "gateway",
        "authz-permission": "ingest",
        "authz-resource": f"raw:{object_key}",
        "authz-issuer": "https://aeluin.gravitalia.com",
        "authz-delegation-id": s3_evidence.metadata["authz-delegation-id"],
    }
    assert len(s3_evidence.metadata["authz-delegation-id"]) == 32
    assert s3_evidence.tags == {
        "owner": UPLOADER_ID,
        "tenant": TENANT_ID,
    }

    print("E2E stage: awaiting terminal pipeline state", flush=True)
    state = await eventually(
        read_pipeline_state,
        timeout_seconds=180.0,
        description="terminal Vision DAG and drained authz outbox",
        abort_on=(PipelineExecutionError,),
    )
    assert state.completed_steps == _EXPECTED_STEPS
    assert state.state_value.get("label") == "gateway-e2e-record"

    source_raw_id = await spicedb.source_raw_id(state.entity_id)
    assert source_raw_id == canonical_spicedb_object_id(object_key)

    assert source_raw_id is not None
    return state, object_key, source_raw_id


async def assert_ingestion_access(
    scenario: GatewayScenario,
    state: DerivedPipelineState,
    object_key: str,
    source_raw_id: str,
) -> None:
    """Checks the source and derived permission projection through public search."""
    gateway = scenario.gateway
    spicedb = scenario.spicedb
    uploader_token = authenticated_token(UPLOADER_ID)
    outsider_token = authenticated_token(OUTSIDER_ID)
    assert await spicedb.allowed(
        resource_type="raw",
        resource_id=object_key,
        permission="view",
        user_id=UPLOADER_ID,
    )
    assert await spicedb.allowed(
        resource_type="raw",
        resource_id=source_raw_id,
        permission="view",
        user_id=UPLOADER_ID,
        canonical_resource=True,
    )
    assert await spicedb.allowed(
        resource_type="entity_state",
        resource_id=f"{TENANT_ID}/{state.entity_id}",
        permission="view",
        user_id=UPLOADER_ID,
    )
    assert not await spicedb.allowed(
        resource_type="entity_state",
        resource_id=f"{TENANT_ID}/{state.entity_id}",
        permission="view",
        user_id=OUTSIDER_ID,
    )

    uploader_results = await _gateway_search(
        gateway, uploader_token, state.entity_id
    )
    outsider_results = await _gateway_search(
        gateway, outsider_token, state.entity_id
    )
    assert len(uploader_results) == 1
    uploader_hit = require_mapping(
        uploader_results[0], "uploader structured search hit"
    )
    assert uploader_hit.get("kind") == "entity_state"
    assert uploader_hit.get("entityId") == state.entity_id
    assert outsider_results == []
    isolated_results = await _gateway_search(
        gateway,
        authenticated_token(UPLOADER_ID, tenant_id="isolated_e2e_tenant"),
        state.entity_id,
    )
    assert isolated_results == []
    isolated_search = await gateway.execute(
        authenticated_token(UPLOADER_ID, tenant_id="isolated_e2e_tenant"),
        'query { searchEntities(query: "E2E") { entityId } }',
    )
    assert isolated_search.get("searchEntities") == []
    injection_probe = await gateway.execute(
        outsider_token,
        """
        query SearchInjection($query: String!) {
          searchEntities(query: $query, limit: 10) { entityId }
        }
        """,
        {"query": "' OR 1=1 --"},
    )
    assert injection_probe.get("searchEntities") == []


async def assert_ingestion_lineage(state: DerivedPipelineState) -> None:
    """Correlates Kafka lineage with real Intake, Vision and Gateway OTLP traces."""
    print("E2E stage: verifying lineage events", flush=True)
    lineage = await consume_lineage(
        _EXPECTED_LINEAGE_STATUSES, state.correlation_id, 60.0
    )
    statuses = statuses_by_step(lineage)
    assert statuses.get("infer") == {
        "accepted",
        "running",
        "completed",
    }
    assert statuses.get("resolve") == {"running", "completed"}
    assert statuses.get("sink") == {"running", "completed"}
    assert {event.get("correlation_id") for event in lineage} == {
        state.correlation_id
    }
    lineage_trace_ids = {
        trace_id
        for event in lineage
        if isinstance((trace_id := event.get("trace_id")), str)
    }
    assert len(lineage_trace_ids) == 1
    lineage_trace_id = next(iter(lineage_trace_ids))
    completed_commands = frozenset(
        _string(event, "command_id")
        for event in lineage
        if event.get("status") == "completed"
    )
    assert len(completed_commands) == len(_EXPECTED_STEPS)

    print("E2E stage: verifying distributed traces", flush=True)
    await eventually(
        lambda: _tempo_trace_with_services(
            lineage_trace_id,
            frozenset(
                {"galadril-gateway", "galadril-intake", "galadril-vision"}
            ),
            completed_commands,
        ),
        timeout_seconds=60.0,
        description="Intake-to-Vision Tempo trace",
    )
    gateway_trace = await eventually(
        lambda: _tempo_trace_with_services(
            _GATEWAY_TRACE_ID, frozenset({"galadril-gateway"})
        ),
        timeout_seconds=60.0,
        description="caller-propagated Gateway Tempo trace",
    )
    assert "galadril-gateway" in tempo_service_names(gateway_trace)


async def assert_failed_ingestion(scenario: GatewayScenario) -> None:
    """Joins the real Ray error event to the terminal message in Redpanda."""
    await _upload_text(
        scenario,
        b'{"unexpected":"private diagnostic fixture input"}',
        "e2e-failing-record.txt",
    )
    result = await consume_dead_letter()
    assert result.step == "infer"
    failure = result.failure
    assert failure is not None
    assert failure.code is ErrorCode.INTERNAL
    assert result.admission is None
    assert not result.output

    async def has_correlated_failures() -> bool:
        records = await scenario.environment.pipeline_failure_events()
        prefix = (
            f"{EVENT_VISION_STEP_FAILED} code={failure.code} "
            f"occurrence_id={failure.occurrence_id}"
        )
        expected = (
            f"{prefix} phase=execute attempt=0",
            f"{prefix} phase=dispatch attempt=0",
            f"{prefix} phase=result attempt=0 retry_scheduled=false",
        )
        return all(event in records.splitlines() for event in expected)

    await eventually(
        has_correlated_failures,
        timeout_seconds=45.0,
        description="corresponding Ray and Vision OTLP failure events",
    )
