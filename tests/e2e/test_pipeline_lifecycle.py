"""Gateway-to-Gateway integration test for the complete data lifecycle."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import pytest
from assertions import (
    eventually,
    require_mapping,
    require_sequence,
    tempo_service_names,
)
from clients import (
    ONTOLOGY_ID,
    OUTSIDER_ID,
    PIPELINE_ID,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    RegistryFixtures,
    RelationshipSpec,
    S3ObjectEvidence,
    SpiceDBProbe,
    consume_lineage,
    mint_token,
    read_pipeline_state,
    read_s3_object,
    read_tempo_trace,
    seed_users,
    statuses_by_step,
    upload_presigned,
)
from environment import pipeline_environment

pytestmark = pytest.mark.anyio

_EXPECTED_STEPS = frozenset({"infer", "resolve", "sink"})
_GATEWAY_TRACE_ID = "87e27b4f8c1245938b79ca2831a4f385"


@pytest.fixture
def anyio_backend() -> str:
    """Runs the lifecycle on the production asyncio backend only."""
    return "asyncio"


def _ontology() -> bytes:
    """Returns the smallest production-valid ontology for every DAG block."""
    return json.dumps(
        {
            "version": "1",
            "resources": [
                {
                    "resource_id": "object.entity",
                    "kind": "object_type",
                    "display_name": "E2E entity",
                    "description": "Entity materialized by the lifecycle test",
                    "references": [],
                    "attributes": {},
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _pipeline() -> dict[str, object]:
    """Returns one deterministic source-to-sink runtime DAG."""
    return {
        "version": 1,
        "name": PIPELINE_ID,
        "sources": [
            {
                "id": "e2e_text",
                "topic": "e2e.raw",
                "match_pattern": (
                    rf"^{TENANT_ID}/raw/default/e2e-record\.txt$"
                ),
                "schema_path": "schemas/avro/text.avsc",
                "parser": "text",
                "source_kind": "e2e_fixture",
            }
        ],
        "pipeline": [
            {
                "step": "infer",
                "type": "inference",
                "input_from": ["e2e_text"],
                "model": "e2e_deterministic",
                "artifact_path": "/tmp/galadril-e2e-model",
                "params": {"action": "embed"},
            },
            {
                "step": "resolve",
                "type": "resolve",
                "input_from": ["infer"],
                "params": {
                    "modality": "e2e",
                    "threshold": 0.85,
                    "entity_type": "E2E_ENTITY",
                },
            },
            {
                "step": "sink",
                "type": "sink",
                "connector": "postgres",
                "input_from": ["resolve"],
                "params": {
                    "entity_type": "E2E_ENTITY",
                    "modality": "e2e",
                    "state_type": "E2E_OBSERVATION",
                },
            },
        ],
    }


def _field(data: Mapping[str, object], name: str) -> dict[str, object]:
    """Returns one required GraphQL object field."""
    return require_mapping(data.get(name), f"GraphQL {name}")


def _string(data: Mapping[str, object], name: str) -> str:
    """Returns one required string field with a precise failure."""
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise AssertionError(f"{name} must be a non-empty string")
    return value


async def _authorize_fixture_principals(spicedb: SpiceDBProbe) -> None:
    """Seeds only the identities and ownership absent from public APIs."""
    await spicedb.touch(
        RelationshipSpec("tenant", TENANT_ID, "member", "user", UPLOADER_ID),
        RelationshipSpec(
            "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
        ),
        RelationshipSpec("tenant", TENANT_ID, "member", "user", OUTSIDER_ID),
        RelationshipSpec(
            "ontology",
            f"{TENANT_ID}/{ONTOLOGY_ID}",
            "parent",
            "tenant",
            TENANT_ID,
        ),
        RelationshipSpec(
            "ontology",
            f"{TENANT_ID}/{ONTOLOGY_ID}",
            "owner",
            "user",
            UPLOADER_ID,
        ),
    )


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


async def test_gateway_upload_reaches_authorized_gateway_access() -> None:
    """Proves data, authorization, lineage, and traces survive the full DAG."""
    uploader_token = mint_token(UPLOADER_ID)
    outsider_token = mint_token(OUTSIDER_ID)
    gateway = GatewayClient()
    registry = RegistryFixtures()
    spicedb = SpiceDBProbe()

    try:
        async with pipeline_environment() as environment:
            await eventually(
                lambda: gateway.ready(uploader_token),
                timeout_seconds=90.0,
                description="authenticated Gateway readiness",
            )
            await seed_users()
            await _authorize_fixture_principals(spicedb)
            with pytest.raises(
                AssertionError, match="GraphQL operation failed"
            ):
                await gateway.execute(
                    outsider_token,
                    """
                    mutation RequestUpload {
                      requestStagingUpload { uploadUrl stagingKey }
                    }
                    """,
                )

            ontology_revision = await registry.put_ontology(_ontology())
            ontology_data = await gateway.execute(
                uploader_token,
                """
                mutation PublishOntology(
                  $ontologyId: String!,
                  $displayName: String!,
                  $revisionId: String!,
                  $metadata: JSON!
                ) {
                  publishOntology(
                    ontologyId: $ontologyId,
                    displayName: $displayName,
                    revisionId: $revisionId,
                    metadata: $metadata
                  ) { ontologyId revisionId lifecycle }
                }
                """,
                {
                    "ontologyId": ONTOLOGY_ID,
                    "displayName": "E2E ontology",
                    "revisionId": ontology_revision,
                    "metadata": {"purpose": "pipeline-e2e"},
                },
            )
            publication = _field(ontology_data, "publishOntology")
            assert publication.get("revisionId") == ontology_revision
            assert publication.get("lifecycle") == "production"

            pipeline_data = await gateway.execute(
                uploader_token,
                """
                mutation CreatePipeline(
                  $pipelineId: String!,
                  $name: String!,
                  $definition: JSON!,
                  $message: String!
                ) {
                  createPipeline(
                    pipelineId: $pipelineId,
                    name: $name,
                    definition: $definition,
                    message: $message
                  ) { pipelineId headRevisionId ownerId }
                }
                """,
                {
                    "pipelineId": PIPELINE_ID,
                    "name": PIPELINE_ID,
                    "definition": _pipeline(),
                    "message": "Create deterministic E2E pipeline",
                },
            )
            created_pipeline = _field(pipeline_data, "createPipeline")
            head_revision = _string(created_pipeline, "headRevisionId")
            assert created_pipeline.get("ownerId") == UPLOADER_ID

            async def pipeline_publish_allowed() -> bool | None:
                allowed = await spicedb.allowed(
                    resource_type="pipeline",
                    resource_id=f"{TENANT_ID}/{PIPELINE_ID}",
                    permission="publish",
                    user_id=UPLOADER_ID,
                )
                return True if allowed else None

            await eventually(
                pipeline_publish_allowed,
                timeout_seconds=30.0,
                description="Gateway pipeline relationship replication",
            )
            with pytest.raises(
                AssertionError, match="GraphQL operation failed"
            ):
                await gateway.execute(
                    outsider_token,
                    """
                    mutation PublishPipeline(
                      $pipelineId: String!, $revisionId: String!
                    ) {
                      publishPipeline(
                        pipelineId: $pipelineId,
                        revisionId: $revisionId
                      ) { pipelineId }
                    }
                    """,
                    {
                        "pipelineId": PIPELINE_ID,
                        "revisionId": head_revision,
                    },
                )
            for block_id in ("infer", "resolve", "sink"):
                head_revision = await registry.put_binding(
                    block_id=block_id,
                    expected_revision_id=head_revision,
                )

            published_data = await gateway.execute(
                uploader_token,
                """
                mutation PublishPipeline(
                  $pipelineId: String!, $revisionId: String!
                ) {
                  publishPipeline(
                    pipelineId: $pipelineId,
                    revisionId: $revisionId
                  ) { pipelineId headRevisionId publishedRevisionId }
                }
                """,
                {"pipelineId": PIPELINE_ID, "revisionId": head_revision},
            )
            published_pipeline = _field(published_data, "publishPipeline")
            assert published_pipeline.get("headRevisionId") == head_revision
            assert (
                published_pipeline.get("publishedRevisionId") == head_revision
            )
            runtime_definition = await registry.get_runtime_pipeline(
                head_revision
            )
            assert json.loads(runtime_definition) == _pipeline()

            await environment.start_vision()
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
                b"Galadril E2E record crossing the complete pipeline.\n",
            )
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
                    "targetName": "e2e-record.txt",
                },
                trace_id=_GATEWAY_TRACE_ID,
            )
            object_key = f"{TENANT_ID}/raw/default/e2e-record.txt"
            assert complete_data.get("completeUpload") == object_key

            async def final_object() -> S3ObjectEvidence | None:
                return await read_s3_object(object_key)

            s3_evidence = await eventually(
                final_object,
                timeout_seconds=30.0,
                description="Gateway-promoted S3 object",
            )
            assert s3_evidence.metadata == {
                "tenant": TENANT_ID,
                "owner": UPLOADER_ID,
                "authz-origin": "gateway",
                "authz-permission": "ingest",
                "authz-resource": f"raw:{object_key}",
                "authz-issuer": "https://e2e.galadril.test",
                "authz-delegation-id": s3_evidence.metadata[
                    "authz-delegation-id"
                ],
            }
            assert len(s3_evidence.metadata["authz-delegation-id"]) == 32
            assert s3_evidence.tags == {
                "owner": UPLOADER_ID,
                "tenant": TENANT_ID,
            }

            state = await eventually(
                read_pipeline_state,
                timeout_seconds=180.0,
                description="terminal Vision DAG and drained authz outbox",
            )
            assert state.completed_steps == _EXPECTED_STEPS
            assert state.state_value.get("label") == "gateway-e2e-record"

            async def source_raw() -> str | None:
                return await spicedb.source_raw_id(state.entity_id)

            source_raw_id = await eventually(
                source_raw,
                timeout_seconds=30.0,
                description="derived entity raw lineage relationship",
            )
            assert source_raw_id.startswith(f"{TENANT_ID}/e2e.raw:")

            administrator = RelationshipSpec(
                "tenant", TENANT_ID, "administrator", "user", UPLOADER_ID
            )
            await spicedb.delete(administrator)
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

            lineage = await consume_lineage(_EXPECTED_STEPS, 60.0)
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

            lineage_trace = await eventually(
                lambda: read_tempo_trace(lineage_trace_id),
                timeout_seconds=60.0,
                description="Intake-to-Vision Tempo trace",
            )
            assert {"galadril-intake", "galadril-vision"}.issubset(
                tempo_service_names(lineage_trace)
            )
            gateway_trace = await eventually(
                lambda: read_tempo_trace(_GATEWAY_TRACE_ID),
                timeout_seconds=60.0,
                description="caller-propagated Gateway Tempo trace",
            )
            assert "galadril-gateway" in tempo_service_names(gateway_trace)
    finally:
        await registry.close()
        await gateway.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
