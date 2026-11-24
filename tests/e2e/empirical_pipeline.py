"""Gateway-driven empirical retail lifecycle and least-privilege oracle."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from time import monotonic

import psycopg
import pytest
from assertions import eventually, require_mapping, require_sequence
from authentication import login_user
from clients import (
    ONTOLOGY_ID,
    PIPELINE_ID,
    POSTGRES_DSN,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    GraphQLOperationError,
    RelationshipSpec,
    SpiceDBProbe,
    authenticated_token,
    canonical_spicedb_object_id,
    read_s3_object,
    seed_public_clearances,
    upload_presigned,
)
from empirical_data import load_retail_rows
from environment import EMPIRICAL_STATE_TIMEOUT_SECONDS
from scenario import (
    ADMIN,
    RetailState,
    UploadBatch,
    assert_retail_identities,
    build_access_oracle,
    build_retail_uploads,
    retail_smoke_rows,
)


class EmpiricalPipelineFailure(RuntimeError):
    """Signals a terminal empirical DAG result that polling cannot repair."""


async def _create_principals(
    gateway: GatewayClient, uploads: tuple[UploadBatch, ...]
) -> None:
    """Uses the public administrator mutation for every scenario identity."""
    actors = {upload.actor for upload in uploads}
    actors.update(
        {
            "e2e-sales-reader",
            "e2e-returns-reader",
            "e2e-analyst",
            "e2e-exact-reader",
            "e2e-member",
        }
    )
    for actor in sorted(actors - {UPLOADER_ID}):
        created = await gateway.execute(
            authenticated_token(UPLOADER_ID),
            """
            mutation CreateEmpiricalUser($userId: String!) { administration {
              createUser(userId: $userId, isActive: true)
             }}
            """,
            {"userId": actor},
        )
        assert (
            require_mapping(
                created.get("administration"), "administration"
            ).get("createUser")
            is True
        )
        await login_user(actor)


async def _grant_domains(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    uploads: tuple[UploadBatch, ...],
) -> None:
    """Uses tenant-admin Gateway mutations for scoped ingest and read grants."""
    await spicedb.touch(
        RelationshipSpec("tenant", TENANT_ID, "administrator", "user", ADMIN)
    )

    async def grant(domain: str, user_id: str, permission: str) -> None:
        mutation = await gateway.execute(
            authenticated_token(ADMIN),
            """
            mutation GrantEmpirical($groupId: String!, $userId: String!, $grant: DataDomainGrant!) { administration {
              setDataDomainGrant(groupId: $groupId, userId: $userId, grant: $grant, enabled: true)
             }}
            """,
            {"groupId": domain, "userId": user_id, "grant": permission},
        )
        assert (
            require_mapping(
                mutation.get("administration"), "administration"
            ).get("setDataDomainGrant")
            is True
        )

    for actor in {upload.actor for upload in uploads if upload.actor != ADMIN}:
        domains = {
            upload.data_type for upload in uploads if upload.actor == actor
        }
        assert len(domains) == 1
        await grant(domains.pop(), actor, "INGEST")
    for domain, reader in (
        ("sales", "e2e-sales-reader"),
        ("returns", "e2e-returns-reader"),
    ):
        await grant(domain, reader, "READ")


async def _grant_pipeline_execution(
    spicedb: SpiceDBProbe, actors: set[str]
) -> None:
    """Grants the empirical writers access to the exact pipeline and its GOL contracts."""
    schema_ids = tuple(
        "-".join(
            part.encode("utf-8").hex()
            for part in (TENANT_ID, ONTOLOGY_ID, symbol, "__schema")
        )
        for symbol in (
            "E2EEntity",
            "E2EObservation",
            "IdentityPrototype",
            "ObservationEdge",
            "ObservationEmbedding",
            "ObservationState",
        )
    )
    await spicedb.touch(
        *(
            RelationshipSpec(
                resource_type,
                f"{TENANT_ID}/{resource}",
                relation,
                "user",
                actor,
            )
            for actor in sorted(actors - {ADMIN})
            for resource_type, resource, relation in (
                ("pipeline", PIPELINE_ID, "executor"),
                ("ontology", ONTOLOGY_ID, "editor"),
            )
        ),
        *(
            RelationshipSpec(
                "gol_resource", resource_id, "metadata_readers", "user", actor
            )
            for actor in sorted(actors)
            for resource_id in schema_ids
        ),
    )


async def _upload_rows(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    uploads: tuple[UploadBatch, ...],
) -> dict[str, str]:
    """Promotes independently owned Gateway uploads without timing sleeps."""
    first, *_ = uploads
    actors = {upload.actor for upload in uploads}
    await _grant_pipeline_execution(spicedb, actors)
    await seed_public_clearances(*sorted(actors - {first.actor}))
    promote = """
        mutation PromoteEmpirical($key: String!, $name: String!, $groupId: String!) {
          completeUpload(stagingKey: $key, targetName: $name, groupId: $groupId)
        }
        """
    keys: dict[str, str] = {}
    for upload in uploads:
        token = authenticated_token(upload.actor)
        staged = require_mapping(
            (
                await gateway.execute(
                    token,
                    """
                    mutation StageEmpirical($groupId: String!) {
                      requestStagingUpload(groupId: $groupId) {
                        uploadUrl stagingKey
                      }
                    }
                    """,
                    {"groupId": upload.data_type},
                )
            ).get("requestStagingUpload"),
            "empirical staging",
        )
        upload_url = staged.get("uploadUrl")
        staging_key = staged.get("stagingKey")
        assert isinstance(upload_url, str) and isinstance(staging_key, str)
        await upload_presigned(upload_url, upload.payload)
        variables = {
            "key": staging_key,
            "name": upload.name,
            "groupId": upload.data_type,
        }
        nonce = staging_key.rsplit("/", 1)[-1]
        key = f"{TENANT_ID}/raw/{upload.data_type}/{nonce}/{upload.name}"
        if upload is first:
            with pytest.raises(
                GraphQLOperationError, match="Upload admission unavailable"
            ):
                await gateway.execute(token, promote, variables)
            assert await read_s3_object(key) is None
            await seed_public_clearances(upload.actor)
        completed = await gateway.execute(token, promote, variables)
        assert completed.get("completeUpload") == key
        evidence = await read_s3_object(key)
        assert evidence is not None
        assert evidence.tags == {"owner": upload.actor, "tenant": TENANT_ID}
        assert await spicedb.allowed(
            resource_type="raw",
            resource_id=key,
            permission="view",
            user_id=upload.actor,
        )
        keys[upload.name] = key
    return keys


async def _grant_readers(
    spicedb: SpiceDBProbe, keys: Mapping[str, str]
) -> None:
    """Adds exact-object exceptions and a genuinely intersecting analyst view."""
    grants = [
        RelationshipSpec(
            "raw", keys["sales-00.csv"], "reader", "user", "e2e-exact-reader"
        )
    ]
    for index, name in enumerate(sorted(keys)):
        if index % 2 == 0:
            grants.append(
                RelationshipSpec(
                    "raw", keys[name], "reader", "user", "e2e-analyst"
                )
            )
    await spicedb.touch(*grants)


async def _retail_states(expected: int) -> tuple[RetailState, ...] | None:
    """Reports empirical DAG progress and fails on terminal worker errors."""
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT entity_id, event_id, state_value->>'source_field',
                   state_value->'scalar_evidence'->>'source_row'
            FROM entity_states
            WHERE tenant_id = %s AND state_value->>'label' = 'retail-customer'
            """,
            (TENANT_ID,),
        )
        rows = await cursor.fetchall()
        outbox = await connection.execute(
            "SELECT COUNT(*) FROM authz_outbox WHERE tenant_id = %s",
            (TENANT_ID,),
        )
        outbox_row = await outbox.fetchone()
        progress = await connection.execute(
            """
            SELECT step, status, COUNT(*), MAX(error)
            FROM pipeline_executions
            WHERE tenant_id = %s AND step IN (
                'empirical_infer', 'empirical_resolve', 'empirical_sink'
            )
            GROUP BY step, status
            ORDER BY step, status
            """,
            (TENANT_ID,),
        )
        executions = await progress.fetchall()
    summary = ", ".join(
        f"{step}/{status}={count}" for step, status, count, _ in executions
    )
    for step, status, _, error in executions:
        if status == "failed":
            raise EmpiricalPipelineFailure(
                f"Vision {step} failed: {str(error)[-2000:]}"
            )
    if len(rows) > expected:
        raise EmpiricalPipelineFailure(
            f"Intake duplicated empirical observations: {len(rows)} states"
        )
    outbox_count = int(outbox_row[0]) if outbox_row is not None else -1
    if len(rows) < expected or outbox_count != 0:
        raise RuntimeError(
            f"Empirical progress: states={len(rows)}/{expected}, "
            f"authz_outbox={outbox_count}, executions=[{summary}]"
        )
    return tuple(
        RetailState(str(entity), str(event), int(customer), int(source_row))
        for entity, event, customer, source_row in rows
    )


async def _assert_gateway_projection(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    states: tuple[RetailState, ...],
    keys: Mapping[str, str],
    uploads: tuple[UploadBatch, ...],
) -> None:
    """Matches Gateway output against each caller's independent object oracle."""
    entity_events = assert_retail_identities(states, uploads)
    access = build_access_oracle(uploads)
    source_by_event = await spicedb.event_sources()
    source_by_row = {
        row.source_row: upload.name for upload in uploads for row in upload.rows
    }
    source_ids = {
        name: canonical_spicedb_object_id(key) for name, key in keys.items()
    }
    expected_events: dict[str, set[str]] = defaultdict(set)
    for state in states:
        name = source_by_row[state.source_row]
        assert (
            source_by_event.get(
                canonical_spicedb_object_id(f"{TENANT_ID}/{state.event_id}")
            )
            == source_ids[name]
        )
        expected_events[name].add(state.event_id)

    def source_event(hit: object) -> str:
        """Decodes the public JSON scalar without trusting its wire representation."""
        payload = require_mapping(hit, "state hit").get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        event = require_mapping(payload, "state payload").get("event_id")
        assert isinstance(event, str)
        return event

    for actor, names in access.items():
        token = await login_user(actor)
        visible = (
            set().union(*(expected_events[name] for name in names))
            if names
            else set()
        )
        for entity_id, events in entity_events.items():
            hits = await gateway.execute(
                token,
                """
                query EmpiricalProjection($entityId: String!) {
                  structuredSearch(input: {entityId: $entityId, limit: 50}) {
                    kind payload
                  }
                }
                """,
                {"entityId": entity_id},
            )
            returned = {
                source_event(hit)
                for hit in require_sequence(
                    hits.get("structuredSearch"), "empirical search"
                )
            }
            assert returned == events & visible, (actor, entity_id)

        selected = min(
            (
                entity
                for entity, events in entity_events.items()
                if events & visible
            ),
            default=min(entity_events),
        )
        graph_data = await gateway.execute(
            token,
            """
            query EmpiricalGraph($entityId: String!) {
              entityRelations(entityId: $entityId, depth: 1, limit: 50) {
                nodes { id kind properties }
                edges { fromId toId properties }
              }
            }
            """,
            {"entityId": selected},
        )
        graph = require_mapping(
            graph_data.get("entityRelations"), "empirical graph"
        )
        nodes = [
            require_mapping(node, "graph node")
            for node in require_sequence(graph.get("nodes"), "graph nodes")
        ]
        event_nodes = {
            str(node["id"]) for node in nodes if node["kind"] == "Event"
        }
        assert event_nodes == entity_events[selected] & visible, actor
        if actor == ADMIN:
            assert event_nodes
            assert any(node["properties"] not in ({}, "{}") for node in nodes)
        else:
            assert all(node["properties"] in ({}, "{}") for node in nodes)
            assert all(
                require_mapping(edge, "graph edge")["properties"] in ({}, "{}")
                for edge in require_sequence(graph.get("edges"), "graph edges")
            )


async def exercise_empirical_pipeline(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    *,
    full: bool,
    required_sources: set[tuple[int, int]],
) -> None:
    """Runs real multi-principal retail ingestion and permission projection."""
    rows = load_retail_rows()
    if not full:
        rows = retail_smoke_rows(rows, required_sources)
    uploads = build_retail_uploads(rows, full=full)
    print("E2E stage: preparing empirical principals", flush=True)
    await _create_principals(gateway, uploads)
    await _grant_domains(gateway, spicedb, uploads)
    with pytest.raises(AssertionError, match="Authorization denied"):
        await gateway.execute(
            authenticated_token("e2e-sales-reader"),
            'mutation { requestStagingUpload(groupId: "sales") { stagingKey } }',
        )
    with pytest.raises(GraphQLOperationError) as denied:
        await gateway.execute(
            authenticated_token("e2e-sales-reader"),
            'mutation { administration { createRole(roleName: "e2e_reader_escalation")  }}',
        )
    denied.value.require_forbidden(("administration", "createRole"))
    with pytest.raises(GraphQLOperationError) as denied:
        await gateway.execute(
            authenticated_token("e2e-sales-a"),
            'mutation { administration { createUser(userId: "e2e_escalated", isActive: true)  }}',
        )
    denied.value.require_forbidden(("administration", "createUser"))
    with pytest.raises(GraphQLOperationError) as denied:
        await gateway.execute(
            authenticated_token("e2e-sales-a"),
            'mutation { administration { createRole(roleName: "e2e_escalated_role")  }}',
        )
    denied.value.require_forbidden(("administration", "createRole"))
    with pytest.raises(GraphQLOperationError) as denied:
        await gateway.execute(
            authenticated_token("e2e-sales-a"),
            'mutation { administration { setDataDomainGrant(groupId: "returns", userId: "e2e-sales-a", grant: INGEST, enabled: true)  }}',
        )
    denied.value.require_forbidden(("administration", "setDataDomainGrant"))
    with pytest.raises(AssertionError, match="Authorization denied"):
        await gateway.execute(
            authenticated_token("e2e-sales-a"),
            'mutation { requestStagingUpload(groupId: "returns") { stagingKey } }',
        )
    directory = await gateway.execute(
        authenticated_token(ADMIN),
        "query EmpiricalDirectory { administration { users(limit: 100) { items { userId } after } roles(limit: 100) { items { roleName } after } }}",
    )
    administration = require_mapping(
        directory.get("administration"), "administration"
    )
    users_page = require_mapping(administration.get("users"), "users page")
    roles_page = require_mapping(administration.get("roles"), "roles page")
    assert users_page.get("after") is None, (
        "Fixture users exceed the directory page"
    )
    assert roles_page.get("after") is None, (
        "Fixture roles exceed the directory page"
    )
    assert all(
        require_mapping(user, "directory user").get("userId") != "e2e_escalated"
        for user in require_sequence(users_page.get("items"), "directory users")
    )
    assert all(
        require_mapping(role, "directory role").get("roleName")
        not in {"e2e_escalated_role", "e2e_reader_escalation"}
        for role in require_sequence(roles_page.get("items"), "directory roles")
    )
    assert not await spicedb.allowed(
        resource_type="group",
        resource_id=f"{TENANT_ID}/returns",
        permission="ingest",
        user_id="e2e-sales-a",
    )
    print("E2E stage: uploading empirical retail observations", flush=True)
    started = monotonic()
    keys = await _upload_rows(gateway, spicedb, uploads)
    await _grant_readers(spicedb, keys)
    states = await eventually(
        lambda: _retail_states(len(rows)),
        timeout_seconds=EMPIRICAL_STATE_TIMEOUT_SECONDS,
        description=f"{len(rows)} retail row observations and drained SpiceDB outbox",
        abort_on=(EmpiricalPipelineFailure,),
    )
    print(
        f"E2E retail: {len(states)} observations durable after {monotonic() - started:.1f}s",
        flush=True,
    )
    await _assert_gateway_projection(gateway, spicedb, states, keys, uploads)
    print(
        f"E2E retail: authorization oracle complete after {monotonic() - started:.1f}s",
        flush=True,
    )
