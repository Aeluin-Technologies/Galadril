"""Gateway-driven empirical retail lifecycle and least-privilege oracle."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

import psycopg
import pytest
from assertions import eventually, require_mapping, require_sequence
from clients import (
    POSTGRES_DSN,
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    RelationshipSpec,
    SpiceDBProbe,
    canonical_spicedb_object_id,
    mint_token,
    read_s3_object,
    upload_presigned,
)
from empirical_data import load_retail_rows
from scenario import ADMIN, build_access_oracle, build_retail_uploads


@dataclass(frozen=True, slots=True)
class RetailState:
    """One persisted row whose source identity is independently checked."""

    entity_id: str
    event_id: str
    customer_id: int
    source_row: int


class EmpiricalPipelineFailure(RuntimeError):
    """Signals a terminal empirical DAG result that polling cannot repair."""


async def _create_principals(gateway: GatewayClient) -> None:
    """Uses the public administrator mutation for every scenario identity."""
    actors = {
        upload.actor for upload in build_retail_uploads(load_retail_rows())
    }
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
            mint_token(UPLOADER_ID),
            """
            mutation CreateEmpiricalUser($userId: String!) {
              createUser(userId: $userId, isActive: true)
            }
            """,
            {"userId": actor},
        )
        assert created.get("createUser") is True


async def _grant_domains(gateway: GatewayClient, spicedb: SpiceDBProbe) -> None:
    """Uses tenant-admin Gateway mutations for scoped ingest and read grants."""
    uploads = build_retail_uploads(load_retail_rows())
    await spicedb.touch(
        RelationshipSpec("tenant", TENANT_ID, "administrator", "user", ADMIN)
    )

    async def grant(domain: str, user_id: str, permission: str) -> None:
        mutation = await gateway.execute(
            mint_token(ADMIN),
            """
            mutation GrantEmpirical($groupId: String!, $userId: String!, $grant: DataDomainGrant!) {
              setDataDomainGrant(groupId: $groupId, userId: $userId, grant: $grant, enabled: true)
            }
            """,
            {"groupId": domain, "userId": user_id, "grant": permission},
        )
        assert mutation.get("setDataDomainGrant") is True

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


async def _upload_rows(
    gateway: GatewayClient, spicedb: SpiceDBProbe
) -> dict[str, str]:
    """Promotes 36 independently owned Gateway uploads without timing sleeps."""
    uploads = build_retail_uploads(load_retail_rows())
    keys: dict[str, str] = {}
    for upload in uploads:
        token = mint_token(upload.actor)
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
        completed = await gateway.execute(
            token,
            """
            mutation PromoteEmpirical($key: String!, $name: String!, $groupId: String!) {
              completeUpload(stagingKey: $key, targetName: $name, groupId: $groupId)
            }
            """,
            {
                "key": staging_key,
                "name": upload.name,
                "groupId": upload.data_type,
            },
        )
        key = f"{TENANT_ID}/raw/{upload.data_type}/{upload.name}"
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


async def _retail_states() -> tuple[RetailState, ...] | None:
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
                f"Vision {step} failed: {str(error)[:1000]}"
            )
    if len(rows) > 240:
        raise EmpiricalPipelineFailure(
            f"Intake duplicated empirical observations: {len(rows)} states"
        )
    outbox_count = int(outbox_row[0]) if outbox_row is not None else -1
    if len(rows) < 240 or outbox_count != 0:
        raise RuntimeError(
            f"Empirical progress: states={len(rows)}/240, "
            f"authz_outbox={outbox_count}, executions=[{summary}]"
        )
    return tuple(
        RetailState(str(entity), str(event), int(customer), int(source_row))
        for entity, event, customer, source_row in rows
    )


async def _assert_identity_oracle(
    states: tuple[RetailState, ...],
) -> dict[int, str]:
    """Compares the entire empirical source-customer to LI-ESKG identity map."""
    expected = Counter(row.customer_id for row in load_retail_rows())
    actual = Counter(state.customer_id for state in states)
    assert actual == expected
    expected_rows = {
        row.source_row: row.customer_id for row in load_retail_rows()
    }
    assert {
        state.source_row: state.customer_id for state in states
    } == expected_rows
    assert len({state.event_id for state in states}) == 240
    entities: dict[int, set[str]] = defaultdict(set)
    for state in states:
        entities[state.customer_id].add(state.entity_id)
    assert len(entities) == 24
    assert all(len(ids) == 1 for ids in entities.values())
    distinct = {next(iter(ids)) for ids in entities.values()}
    assert len(distinct) == 24
    return {customer: next(iter(ids)) for customer, ids in entities.items()}


async def _assert_gateway_projection(
    gateway: GatewayClient,
    spicedb: SpiceDBProbe,
    states: tuple[RetailState, ...],
    keys: Mapping[str, str],
) -> None:
    """Matches Gateway output against each caller's independent object oracle."""
    uploads = build_retail_uploads(load_retail_rows())
    access = build_access_oracle(uploads)
    source_by_event = await spicedb.event_sources()
    source_by_row = {
        row.source_row: canonical_spicedb_object_id(keys[upload.name])
        for upload in uploads
        for row in upload.rows
    }
    assert all(
        source_by_event.get(
            canonical_spicedb_object_id(f"{TENANT_ID}/{state.event_id}")
        )
        == source_by_row[state.source_row]
        for state in states
    )
    expected_events: dict[str, set[str]] = defaultdict(set)
    for upload in uploads:
        key = canonical_spicedb_object_id(keys[upload.name])
        for state in states:
            if (
                source_by_event.get(
                    canonical_spicedb_object_id(f"{TENANT_ID}/{state.event_id}")
                )
                == key
            ):
                expected_events[upload.name].add(state.event_id)
        assert len(expected_events[upload.name]) == len(upload.rows)
    identity = await _assert_identity_oracle(states)

    def source_event(hit: object) -> str:
        """Decodes the public JSON scalar without trusting its wire representation."""
        payload = require_mapping(hit, "state hit").get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        event = require_mapping(payload, "state payload").get("event_id")
        assert isinstance(event, str)
        return event

    for actor, names in access.items():
        visible = (
            set().union(*(expected_events[name] for name in names))
            if names
            else set()
        )
        for entity_id in identity.values():
            hits = await gateway.execute(
                mint_token(actor),
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
            expected_for_entity = {
                state.event_id
                for state in states
                if state.entity_id == entity_id
            } & visible
            assert returned == expected_for_entity, (actor, entity_id)

        selected = next(iter(identity.values()))
        graph_data = await gateway.execute(
            mint_token(actor),
            """
            query EmpiricalGraph($entityId: String!) {
              entityRelations(entityId: $entityId, depth: 1, limit: 50) {
                nodes { id label properties }
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
            str(node["id"])
            for node in nodes
            if str(node["id"]).startswith("evt_")
        }
        assert event_nodes <= visible, actor
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
    gateway: GatewayClient, spicedb: SpiceDBProbe
) -> None:
    """Runs real multi-principal retail ingestion and permission projection."""
    print("E2E stage: preparing empirical principals", flush=True)
    await _create_principals(gateway)
    await _grant_domains(gateway, spicedb)
    with pytest.raises(AssertionError, match="Authorization denied"):
        await gateway.execute(
            mint_token("e2e-sales-reader"),
            'mutation { requestStagingUpload(groupId: "sales") { stagingKey } }',
        )
    with pytest.raises(AssertionError, match="not a tenant admin"):
        await gateway.execute(
            mint_token("e2e-sales-reader"),
            'mutation { createRole(roleName: "e2e_reader_escalation") }',
        )
    with pytest.raises(AssertionError, match="not a tenant admin"):
        await gateway.execute(
            mint_token("e2e-sales-a"),
            'mutation { createUser(userId: "e2e_escalated", isActive: true) }',
        )
    with pytest.raises(AssertionError, match="not a tenant admin"):
        await gateway.execute(
            mint_token("e2e-sales-a"),
            'mutation { createRole(roleName: "e2e_escalated_role") }',
        )
    with pytest.raises(AssertionError, match="not a tenant admin"):
        await gateway.execute(
            mint_token("e2e-sales-a"),
            'mutation { setDataDomainGrant(groupId: "returns", userId: "e2e-sales-a", grant: INGEST, enabled: true) }',
        )
    with pytest.raises(AssertionError, match="Authorization denied"):
        await gateway.execute(
            mint_token("e2e-sales-a"),
            'mutation { requestStagingUpload(groupId: "returns") { stagingKey } }',
        )
    directory = await gateway.execute(
        mint_token(ADMIN),
        "query EmpiricalDirectory { users { userId } roles { roleName } }",
    )
    assert all(
        require_mapping(user, "directory user").get("userId") != "e2e_escalated"
        for user in require_sequence(directory.get("users"), "directory users")
    )
    assert all(
        require_mapping(role, "directory role").get("roleName")
        not in {"e2e_escalated_role", "e2e_reader_escalation"}
        for role in require_sequence(directory.get("roles"), "directory roles")
    )
    assert not await spicedb.allowed(
        resource_type="group",
        resource_id=f"{TENANT_ID}/returns",
        permission="ingest",
        user_id="e2e-sales-a",
    )
    print("E2E stage: uploading empirical retail observations", flush=True)
    keys = await _upload_rows(gateway, spicedb)
    await _grant_readers(spicedb, keys)
    states = await eventually(
        _retail_states,
        timeout_seconds=1200.0,
        description="240 retail row observations and drained SpiceDB outbox",
        abort_on=(EmpiricalPipelineFailure,),
    )
    await _assert_gateway_projection(gateway, spicedb, states, keys)
