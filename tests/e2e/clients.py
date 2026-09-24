"""Typed clients and probes used by the pipeline lifecycle E2E test."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import boto3
import grpc
import httpx
import psycopg
from assertions import require_mapping
from authzed.api.v1 import (
    AsyncClient,
    CheckPermissionRequest,
    CheckPermissionResponse,
    Consistency,
    ObjectReference,
    ReadRelationshipsRequest,
    Relationship,
    RelationshipFilter,
    RelationshipUpdate,
    SubjectReference,
    WriteRelationshipsRequest,
)
from botocore.exceptions import ClientError
from confluent_kafka import Consumer, KafkaException
from confluent_kafka.admin import AdminClient
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from galadril_registry_api import registry_pb2
from google.protobuf.message import Message
from grpcutil import insecure_bearer_token_credentials

TENANT_ID = "debug_tenant"
UPLOADER_ID = "e2e_uploader"
OUTSIDER_ID = "e2e_outsider"
PIPELINE_ID = "e2e_pipeline"
ONTOLOGY_ID = "e2e_ontology"
GATEWAY_URL = "http://127.0.0.1:18080/graphql"
REGISTRY_TARGET = "127.0.0.1:15052"
POSTGRES_DSN = "postgresql://postgres:postgres@127.0.0.1:15432/galadril_dev"
SPICEDB_TARGET = "127.0.0.1:15051"
SPICEDB_TOKEN = "secret_key"

_VISION_CONSUMER_GROUPS = (
    "galadril-e2e-ingress",
    "galadril-e2e-cpu",
    "galadril-e2e-gpu",
    "galadril-e2e-causal",
)

_ISSUER = "https://e2e.galadril.test"
_AUDIENCE = "galadril-e2e"
_PRIVATE_KEY = b"""-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgevZzL1gdAFr88hb2
OF/2NxApJCzGCEDdfSp6VQO30hyhRANCAAQRWz+jn65BtOMvdyHKcvjBeBSDZH2r
1RTwjmYSi9R/zpBnuQ4EiMnCqfMPWiZqB4QdbAd0E7oH50VpuZ1P087G
-----END PRIVATE KEY-----
"""


class _Revision(Protocol):
    revision_id: str


class _Binding(Protocol):
    head_revision_id: str


class _RuntimePipeline(Protocol):
    pipeline_id: str
    head_revision_id: str
    definition_json: bytes


class _ConsumerGroupDescription(Protocol):
    members: Sequence[object]


class _ConsumerGroupFuture(Protocol):
    def result(self, timeout: float | None = None) -> object:
        """Returns one broker description or raises its Kafka error."""


@dataclass(frozen=True, slots=True)
class DerivedPipelineState:
    """Durable state proving every Vision step and outbox write completed."""

    entity_id: str
    state_value: Mapping[str, object]
    correlation_id: str
    completed_steps: frozenset[str]


@dataclass(frozen=True, slots=True)
class S3ObjectEvidence:
    """Trusted metadata and tags persisted by Gateway on the final object."""

    metadata: Mapping[str, str]
    tags: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class RelationshipSpec:
    """One exact relationship written to or removed from SpiceDB."""

    resource_type: str
    resource_id: str
    relation: str
    subject_type: str
    subject_id: str


def _base64url(value: bytes) -> str:
    """Encodes one JWT segment without RFC 4648 padding."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def mint_token(
    user_id: str,
    *,
    tenant_id: str = TENANT_ID,
    expires_at: int | None = None,
) -> str:
    """Mints a short-lived ES256 token accepted by the isolated Gateway."""
    now = int(time.time())
    header = _base64url(b'{"alg":"ES256","typ":"JWT"}')
    claims = _base64url(
        json.dumps(
            {
                "aud": _AUDIENCE,
                "exp": expires_at if expires_at is not None else now + 1800,
                "iat": now,
                "iss": _ISSUER,
                "sub": user_id,
                "tenant_id": tenant_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    signing_input = f"{header}.{claims}".encode("ascii")
    private_key = serialization.load_pem_private_key(
        _PRIVATE_KEY, password=None
    )
    if not isinstance(private_key, ec.EllipticCurvePrivateKey):
        raise TypeError("E2E signing key is not an elliptic-curve key")
    der_signature = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r_value, s_value = decode_dss_signature(der_signature)
    signature = r_value.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    return f"{header}.{claims}.{_base64url(signature)}"


class GatewayClient:
    """Small GraphQL client that fails on both transport and field errors."""

    __slots__ = ("_client",)

    def __init__(self) -> None:
        # Authentication and body-limit rejections may intentionally stop
        # reading request bodies, so boundary tests must not reuse the socket.
        self._client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def close(self) -> None:
        """Releases the shared HTTP connection pool."""
        await self._client.aclose()

    async def execute(
        self,
        token: str,
        query: str,
        variables: Mapping[str, object] | None = None,
        *,
        trace_id: str | None = None,
    ) -> dict[str, object]:
        """Executes one authenticated GraphQL operation."""
        response = await self.request(
            token,
            query,
            variables,
            trace_id=trace_id,
        )
        try:
            decoded = response.json()
        except ValueError:
            response.raise_for_status()
            raise AssertionError("GraphQL response is not JSON") from None
        body = require_mapping(decoded, "GraphQL response")
        errors = body.get("errors")
        if errors:
            raise AssertionError(f"GraphQL operation failed: {errors}")
        response.raise_for_status()
        return require_mapping(body.get("data"), "GraphQL response data")

    async def request(
        self,
        token: str | None,
        query: str,
        variables: Mapping[str, object] | None = None,
        *,
        trace_id: str | None = None,
    ) -> httpx.Response:
        """Returns the raw authenticated response for boundary assertions."""
        headers: dict[str, str] = {}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        if trace_id is not None:
            headers["traceparent"] = f"00-{trace_id}-0123456789abcdef-01"
        return await self._client.post(
            GATEWAY_URL,
            headers=headers,
            json={"query": query, "variables": variables or {}},
        )

    async def ready(self, token: str) -> bool | None:
        """Returns readiness only after authenticated GraphQL is usable."""
        await self.execute(token, "query { __typename }")
        return True


def _serialize(message: object) -> bytes:
    """Serializes generated protobufs behind a stable grpcio callback."""
    return cast(bytes, cast(Message, message).SerializeToString())


def _ontology_revision(data: bytes) -> object:
    """Deserializes one Registry ontology revision."""
    return registry_pb2.OntologyRevision.FromString(data)


def _binding(data: bytes) -> object:
    """Deserializes one Registry binding response."""
    return registry_pb2.OntologyBinding.FromString(data)


def _pipeline(data: bytes) -> object:
    """Deserializes one Registry runtime pipeline."""
    return registry_pb2.Pipeline.FromString(data)


class RegistryFixtures:
    """Authors prerequisites absent from Gateway's current public schema."""

    __slots__ = ("_channel",)

    def __init__(self) -> None:
        self._channel = grpc.aio.insecure_channel(REGISTRY_TARGET)

    async def close(self) -> None:
        """Closes the Registry HTTP/2 channel."""
        await self._channel.close()

    async def put_ontology(self, ontology_json: bytes) -> str:
        """Creates the ontology revision required by pipeline bindings."""
        response = cast(
            _Revision,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/PutOntology",
                request_serializer=_serialize,
                response_deserializer=_ontology_revision,
            )(
                registry_pb2.PutOntologyRequest(
                    tenant_id=TENANT_ID,
                    ontology_id=ONTOLOGY_ID,
                    display_name="E2E ontology",
                    ontology_json=ontology_json,
                    author=UPLOADER_ID,
                    message="Seed E2E ontology",
                    create_only=True,
                )
            ),
        )
        return response.revision_id

    async def put_binding(
        self, *, block_id: str, expected_revision_id: str
    ) -> str:
        """Binds one pipeline block and returns the resulting immutable head."""
        response = cast(
            _Binding,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/PutBinding",
                request_serializer=_serialize,
                response_deserializer=_binding,
            )(
                registry_pb2.PutBindingRequest(
                    tenant_id=TENANT_ID,
                    expected_revision_id=expected_revision_id,
                    binding=registry_pb2.OntologyBinding(
                        pipeline_id=PIPELINE_ID,
                        block_id=block_id,
                        ontology_id=ONTOLOGY_ID,
                        resource_ids=["object.entity"],
                        include_dependencies=True,
                        metadata_json=b"{}",
                    ),
                )
            ),
        )
        return response.head_revision_id

    async def get_runtime_pipeline(self, revision_id: str) -> bytes:
        """Verifies the exact revision Vision will resolve at startup."""
        response = cast(
            _RuntimePipeline,
            await self._channel.unary_unary(
                "/galadril.registry.v1.Registry/GetRuntimePipeline",
                request_serializer=_serialize,
                response_deserializer=_pipeline,
            )(
                registry_pb2.GetRuntimePipelineRequest(
                    tenant_id=TENANT_ID,
                    pipeline_id=PIPELINE_ID,
                    revision_id=revision_id,
                )
            ),
        )
        if response.pipeline_id != PIPELINE_ID:
            raise AssertionError("Registry returned the wrong runtime pipeline")
        if response.head_revision_id != revision_id:
            raise AssertionError("Registry returned a mutable runtime revision")
        return response.definition_json


def canonical_spicedb_object_id(value: str) -> str:
    """Matches the production injective encoding for SpiceDB object IDs."""
    encoded = bytearray()
    for byte in value.encode("utf-8"):
        if (
            48 <= byte <= 57
            or 65 <= byte <= 90
            or 97 <= byte <= 122
            or byte in b"/_|-+"
        ):
            encoded.append(byte)
        else:
            encoded.extend(f"={byte:02X}".encode("ascii"))
    return encoded.decode("ascii")


def _relationship(spec: RelationshipSpec) -> Relationship:
    """Builds one generated SpiceDB relationship from a typed fixture."""
    return Relationship(
        resource=ObjectReference(
            object_type=spec.resource_type,
            object_id=canonical_spicedb_object_id(spec.resource_id),
        ),
        relation=spec.relation,
        subject=SubjectReference(
            object=ObjectReference(
                object_type=spec.subject_type,
                object_id=canonical_spicedb_object_id(spec.subject_id),
            )
        ),
    )


class SpiceDBProbe:
    """Writes test principals and verifies the resulting authorization graph."""

    __slots__ = ("_client",)

    def __init__(self) -> None:
        credentials = insecure_bearer_token_credentials(SPICEDB_TOKEN)
        self._client = AsyncClient(SPICEDB_TARGET, credentials)

    async def touch(self, *specs: RelationshipSpec) -> None:
        """Creates exact fixture relationships idempotently."""
        await self._client.WriteRelationships(
            WriteRelationshipsRequest(
                updates=[
                    RelationshipUpdate(
                        operation=(
                            RelationshipUpdate.Operation.OPERATION_TOUCH
                        ),
                        relationship=_relationship(spec),
                    )
                    for spec in specs
                ]
            )
        )

    async def delete(self, spec: RelationshipSpec) -> None:
        """Removes one fixture grant before least-privilege assertions."""
        await self._client.WriteRelationships(
            WriteRelationshipsRequest(
                updates=[
                    RelationshipUpdate(
                        operation=(
                            RelationshipUpdate.Operation.OPERATION_DELETE
                        ),
                        relationship=_relationship(spec),
                    )
                ]
            )
        )

    async def allowed(
        self,
        *,
        resource_type: str,
        resource_id: str,
        permission: str,
        user_id: str,
    ) -> bool:
        """Checks one permission against fully consistent graph state."""
        response = await self._client.CheckPermission(
            CheckPermissionRequest(
                consistency=Consistency(fully_consistent=True),
                resource=ObjectReference(
                    object_type=resource_type,
                    object_id=canonical_spicedb_object_id(resource_id),
                ),
                permission=permission,
                subject=SubjectReference(
                    object=ObjectReference(
                        object_type="user",
                        object_id=canonical_spicedb_object_id(user_id),
                    )
                ),
            )
        )
        return int(response.permissionship) == int(
            CheckPermissionResponse.PERMISSIONSHIP_HAS_PERMISSION
        )

    async def source_raw_id(self, entity_id: str) -> str | None:
        """Reads the derived entity's exact raw lineage edge."""
        stream = self._client.ReadRelationships(
            ReadRelationshipsRequest(
                consistency=Consistency(fully_consistent=True),
                relationship_filter=RelationshipFilter(
                    resource_type="entity_state",
                    optional_resource_id=canonical_spicedb_object_id(
                        f"{TENANT_ID}/{entity_id}"
                    ),
                    optional_relation="source",
                ),
            )
        )
        async for response in stream:
            relationship = response.relationship
            if relationship.subject.object.object_type == "raw":
                return relationship.subject.object.object_id
        return None


async def seed_users() -> None:
    """Creates both test identities in Gateway's authoritative directory."""
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        await connection.execute(
            """
            INSERT INTO iam_users (tenant_id, user_id)
            VALUES (%s, %s), (%s, %s)
            ON CONFLICT (tenant_id, user_id) DO UPDATE
            SET is_active = TRUE, deleted_at = NULL, updated_at = NOW()
            """,
            (TENANT_ID, UPLOADER_ID, TENANT_ID, OUTSIDER_ID),
        )


def pipeline_execution_failure(
    rows: Sequence[tuple[object, object, object]],
) -> str | None:
    """Returns the first durable step failure with its persisted reason."""
    for step, status, error in rows:
        if str(status) == "failed":
            reason = str(error) if error is not None else "unspecified error"
            return f"{step}: {reason}"
    return None


async def read_pipeline_state() -> DerivedPipelineState | None:
    """Returns terminal DB state only when all steps and authz outbox completed."""
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT entity_id, state_value
            FROM entity_states
            WHERE tenant_id = %s
            ORDER BY event_time DESC
            LIMIT 1
            """,
            (TENANT_ID,),
        )
        entity_row = await cursor.fetchone()
        executions_cursor = await connection.execute(
            """
            SELECT step, status, correlation_id, error
            FROM pipeline_executions
            WHERE tenant_id = %s AND pipeline LIKE %s
            """,
            (TENANT_ID, f"%/{PIPELINE_ID}/%"),
        )
        execution_rows = await executions_cursor.fetchall()
        outbox_cursor = await connection.execute(
            "SELECT COUNT(*) FROM authz_outbox WHERE tenant_id = %s",
            (TENANT_ID,),
        )
        outbox_row = await outbox_cursor.fetchone()
    failure = pipeline_execution_failure(
        [(row[0], row[1], row[3]) for row in execution_rows]
    )
    if failure is not None:
        raise AssertionError(f"Vision pipeline execution failed: {failure}")
    if entity_row is None or outbox_row is None or int(outbox_row[0]) != 0:
        return None
    completed_steps = frozenset(
        str(row[0]) for row in execution_rows if str(row[1]) == "completed"
    )
    if completed_steps != frozenset({"infer", "resolve", "sink"}):
        return None
    correlation_ids = {str(row[2]) for row in execution_rows}
    if len(correlation_ids) != 1:
        return None
    entity_id = str(entity_row[0])
    state_value = require_mapping(entity_row[1], "entity state")
    correlation_id = next(iter(correlation_ids))
    return DerivedPipelineState(
        entity_id=entity_id,
        state_value=state_value,
        correlation_id=correlation_id,
        completed_steps=completed_steps,
    )


async def vision_database_ready() -> bool | None:
    """Confirms Vision completed its operational schema transaction."""
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN,
        connect_timeout=3,
        options="-c statement_timeout=3000",
    ) as connection:
        cursor = await connection.execute(
            """
            SELECT to_regclass('public.entity_states'),
                   to_regclass('public.pipeline_executions'),
                   to_regclass('public.authz_outbox')
            """
        )
        row = await cursor.fetchone()
    if row is None or any(value is None for value in row):
        return None
    return True


def _vision_consumer_groups_have_members(
    member_counts: Mapping[str, int],
) -> bool:
    """Requires every ingress and command group to have a live member."""
    return all(
        member_counts.get(group_id, 0) > 0
        for group_id in _VISION_CONSUMER_GROUPS
    )


def _vision_consumers_ready() -> bool | None:
    """Queries Redpanda for the live Vision consumer group membership."""
    admin = AdminClient({"bootstrap.servers": "127.0.0.1:19092"})
    futures = cast(
        Mapping[str, _ConsumerGroupFuture],
        admin.describe_consumer_groups(
            list(_VISION_CONSUMER_GROUPS), request_timeout=3.0
        ),
    )
    member_counts: dict[str, int] = {}
    try:
        for group_id, future in futures.items():
            description = cast(
                _ConsumerGroupDescription, future.result(timeout=3.0)
            )
            member_counts[group_id] = len(description.members)
    except KafkaException:
        return None
    return True if _vision_consumer_groups_have_members(member_counts) else None


async def vision_runtime_ready() -> bool | None:
    """Confirms both Vision schema initialization and Kafka membership."""
    if await vision_database_ready() is None:
        return None
    return await asyncio.to_thread(_vision_consumers_ready)


async def upload_presigned(
    url: str, content: bytes, *, content_type: str = "text/plain"
) -> None:
    """Uploads through the exact URL returned by Gateway against local MinIO."""
    parsed = urlsplit(url)
    local_url = urlunsplit(
        (parsed.scheme, "127.0.0.1:19000", parsed.path, parsed.query, "")
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.put(
            local_url,
            content=content,
            headers={"content-type": content_type, "host": parsed.netloc},
        )
        response.raise_for_status()


def _read_s3_object(key: str) -> S3ObjectEvidence | None:
    """Reads object metadata synchronously for an asyncio executor."""
    client = boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:19000",
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )
    try:
        head = client.head_object(Bucket="lake", Key=key)
        tagging = client.get_object_tagging(Bucket="lake", Key=key)
    except ClientError as error:
        status = error.response.get("ResponseMetadata", {}).get(
            "HTTPStatusCode"
        )
        if status == 404:
            return None
        raise
    metadata_value = head.get("Metadata")
    metadata = (
        {str(key): str(value) for key, value in metadata_value.items()}
        if isinstance(metadata_value, dict)
        else {}
    )
    tag_set = tagging.get("TagSet")
    tags: dict[str, str] = {}
    if isinstance(tag_set, list):
        for item in tag_set:
            if not isinstance(item, dict):
                continue
            key_value = item.get("Key")
            tag_value = item.get("Value")
            if isinstance(key_value, str) and isinstance(tag_value, str):
                tags[key_value] = tag_value
    return S3ObjectEvidence(metadata=metadata, tags=tags)


async def read_s3_object(key: str) -> S3ObjectEvidence | None:
    """Reads final S3 evidence without blocking the test event loop."""
    return await asyncio.to_thread(_read_s3_object, key)


def _consume_lineage(
    expected_steps: frozenset[str], timeout_seconds: float
) -> list[dict[str, object]]:
    """Consumes one correlated lineage chain from its production Kafka topic."""
    consumer = Consumer(
        {
            "bootstrap.servers": "127.0.0.1:19092",
            "group.id": f"galadril-e2e-assert-{uuid4()}",
            "auto.offset.reset": "earliest",
            "enable.auto.commit": False,
        }
    )
    events: list[dict[str, object]] = []
    seen_terminal: set[str] = set()
    deadline = time.monotonic() + timeout_seconds
    try:
        consumer.subscribe(["pipeline.lineage.v1"])
        while time.monotonic() < deadline and seen_terminal != expected_steps:
            message = consumer.poll(0.5)
            if message is None or message.error() is not None:
                continue
            value = message.value()
            if not isinstance(value, bytes):
                continue
            decoded = json.loads(value)
            if not isinstance(decoded, dict):
                continue
            if decoded.get("tenant_id") != TENANT_ID:
                continue
            pipeline = decoded.get("pipeline")
            if (
                not isinstance(pipeline, str)
                or f"/{PIPELINE_ID}/" not in pipeline
            ):
                continue
            event = require_mapping(decoded, "lineage event")
            events.append(event)
            if event.get("status") == "completed":
                step = event.get("step")
                if isinstance(step, str):
                    seen_terminal.add(step)
    finally:
        consumer.close()
    if seen_terminal != expected_steps:
        raise AssertionError(
            f"Missing completed lineage steps: {expected_steps - seen_terminal}"
        )
    return events


async def consume_lineage(
    expected_steps: frozenset[str], timeout_seconds: float
) -> list[dict[str, object]]:
    """Consumes lineage without blocking Gateway or database polling."""
    return await asyncio.to_thread(
        _consume_lineage, expected_steps, timeout_seconds
    )


async def read_tempo_trace(trace_id: str) -> dict[str, object] | None:
    """Returns one Tempo trace after it becomes queryable."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"http://127.0.0.1:13200/api/traces/{trace_id}"
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return require_mapping(response.json(), "Tempo trace")


def statuses_by_step(
    events: Sequence[Mapping[str, object]],
) -> dict[str, set[str]]:
    """Groups lineage statuses by pipeline step for compact assertions."""
    statuses: dict[str, set[str]] = {}
    for event in events:
        step = event.get("step")
        status = event.get("status")
        if isinstance(step, str) and isinstance(status, str):
            statuses.setdefault(step, set()).add(status)
    return statuses
