"""Gateway-to-Gateway integration test for the complete data lifecycle."""

from __future__ import annotations

import asyncio
import json
import time
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
    VISION_RUNTIME_READY_TIMEOUT_SECONDS,
    GatewayClient,
    RegistryFixtures,
    RelationshipSpec,
    S3ObjectEvidence,
    SpiceDBProbe,
    canonical_spicedb_object_id,
    consume_lineage,
    mint_token,
    read_pipeline_state,
    read_s3_object,
    read_tempo_trace,
    seed_users,
    statuses_by_step,
    upload_presigned,
    vision_runtime_ready,
)
from environment import PIPELINE_LIFECYCLE_TIMEOUT_SECONDS, pipeline_environment

pytestmark = pytest.mark.anyio

_EXPECTED_STEPS = frozenset({"infer", "resolve", "sink"})
_GATEWAY_TRACE_ID = "87e27b4f8c1245938b79ca2831a4f385"
_QUERY_FIELDS = frozenset(
    {
        "auditEvents",
        "conversation",
        "conversations",
        "entityRelations",
        "globalSearch",
        "ontologies",
        "ontologyBindings",
        "pipelineDefinitions",
        "pipelineExecutions",
        "roleAssignments",
        "roles",
        "searchEmbeddings",
        "searchEntities",
        "searchEvents",
        "structuredSearch",
        "users",
    }
)
_MUTATION_FIELDS = frozenset(
    {
        "assignRoleToUser",
        "completeUpload",
        "createConversation",
        "createMessage",
        "createPipeline",
        "createRole",
        "createUser",
        "deleteConversation",
        "deleteMessage",
        "deletePipeline",
        "deleteRole",
        "deleteUser",
        "publishOntology",
        "publishPipeline",
        "requestStagingUpload",
        "retireOntology",
        "setCedarPolicy",
        "unassignRoleFromUser",
        "updateConversation",
        "updateMessage",
        "updatePipeline",
        "updateUser",
    }
)


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


async def _wait_for_gateway_tenant_admin(
    gateway: GatewayClient, spicedb: SpiceDBProbe, token: str
) -> None:
    """Waits until both SpiceDB and Gateway observe tenant administration."""

    async def tenant_manage_allowed() -> bool | None:
        allowed = await spicedb.allowed(
            resource_type="tenant",
            resource_id=TENANT_ID,
            permission="manage",
            user_id=UPLOADER_ID,
        )
        return True if allowed else None

    await eventually(
        tenant_manage_allowed,
        timeout_seconds=30.0,
        description="tenant administrator relationship replication",
    )

    consecutive_observations = 0

    async def gateway_admin_visible() -> bool | None:
        nonlocal consecutive_observations
        try:
            data = await gateway.execute(
                token, "query TenantAdmin { users { userId } }"
            )
        except AssertionError as error:
            if "Authorization denied" in str(error):
                consecutive_observations = 0
                return None
            raise
        require_sequence(data.get("users"), "users")
        consecutive_observations += 1
        return True if consecutive_observations >= 5 else None

    await eventually(
        gateway_admin_visible,
        timeout_seconds=30.0,
        description="Gateway tenant administrator consistency",
    )


async def _execute_after_authorization_replication(
    gateway: GatewayClient,
    token: str,
    query: str,
    variables: Mapping[str, object] | None,
    *,
    description: str,
) -> dict[str, object]:
    """Retries only authorization denials caused by async tuple replication."""
    deadline = time.monotonic() + 30.0
    last_denial: AssertionError | None = None
    while time.monotonic() < deadline:
        try:
            return await gateway.execute(token, query, variables)
        except AssertionError as error:
            message = str(error)
            if not any(
                denial in message
                for denial in ("Authorization denied", "not a tenant admin")
            ):
                raise
            last_denial = error
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"Timed out waiting for {description}; last denial: {last_denial}"
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


async def _assert_graphql_security_boundaries(
    gateway: GatewayClient, token: str
) -> None:
    """Proves authentication and resource exhaustion controls fail closed."""
    missing = await gateway.request(None, "query { __typename }")
    assert missing.status_code == 401

    malformed = await gateway.request("not-a-jwt", "query { __typename }")
    assert malformed.status_code == 401

    header, claims, signature = token.split(".")
    replacement = "A" if not signature.startswith("A") else "B"
    tampered = f"{header}.{claims}.{replacement}{signature[1:]}"
    invalid_signature = await gateway.request(tampered, "query { __typename }")
    assert invalid_signature.status_code == 401

    expired = await gateway.request(
        mint_token(UPLOADER_ID, expires_at=int(time.time()) - 60),
        "query { __typename }",
    )
    assert expired.status_code == 401

    oversized = await gateway.request(
        token,
        "query { __typename }" + (" " * 5000),
    )
    assert oversized.status_code == 413

    nested = await gateway.request(
        token,
        """
        query ExcessiveDepth {
          ontologies {
            ... on Ontology {
              ... on Ontology {
                ... on Ontology {
                  ... on Ontology { ontologyId }
                }
              }
            }
          }
        }
        """,
    )
    assert nested.status_code == 400
    assert "configured limit of 5" in nested.text


async def _assert_public_api_inventory(
    gateway: GatewayClient, token: str
) -> None:
    """Fails when a public root field lacks an explicit E2E disposition."""
    data = await gateway.execute(
        token,
        """
        query ApiInventory {
          __schema {
            queryType { fields { name } }
            mutationType { fields { name } }
            subscriptionType { fields { name } }
          }
        }
        """,
    )
    schema = _field(data, "__schema")

    def field_names(root_name: str) -> set[str]:
        root = _field(schema, root_name)
        return {
            _string(require_mapping(field, root_name), "name")
            for field in require_sequence(root.get("fields"), root_name)
        }

    assert field_names("queryType") == _QUERY_FIELDS
    assert field_names("mutationType") == _MUTATION_FIELDS
    assert field_names("subscriptionType") == {"ask"}


async def _exercise_iam_and_conversation_api(
    gateway: GatewayClient, token: str
) -> None:
    """Exercises available GraphQL CRUD fields without invoking chat."""
    managed_user = "e2e_managed_user"
    managed_role = "e2e_managed_role"
    created = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        mutation CreateUser($userId: String!) {
          createUser(userId: $userId, isActive: false)
        }
        """,
        {"userId": managed_user},
        description="tenant administrator mutation consistency",
    )
    assert created.get("createUser") is True
    updated = await gateway.execute(
        token,
        """
        mutation UpdateUser($userId: String!) {
          updateUser(userId: $userId, isActive: true)
        }
        """,
        {"userId": managed_user},
    )
    assert updated.get("updateUser") is True
    role = await gateway.execute(
        token,
        """
        mutation CreateRole($roleName: String!) {
          createRole(roleName: $roleName)
        }
        """,
        {"roleName": managed_role},
    )
    assert role.get("createRole") is True
    assigned = await gateway.execute(
        token,
        """
        mutation Assign($userId: String!, $roleName: String!) {
          assignRoleToUser(userId: $userId, roleName: $roleName)
        }
        """,
        {"userId": managed_user, "roleName": managed_role},
    )
    assert assigned.get("assignRoleToUser") is True

    directory = await gateway.execute(
        token,
        """
        query Directory {
          users { userId isActive }
          roles { roleName }
          roleAssignments { userId roleName }
        }
        """,
    )
    assert any(
        require_mapping(user, "user").get("userId") == managed_user
        for user in require_sequence(directory.get("users"), "users")
    )
    assert any(
        require_mapping(item, "role").get("roleName") == managed_role
        for item in require_sequence(directory.get("roles"), "roles")
    )
    assert any(
        require_mapping(item, "assignment").get("userId") == managed_user
        for item in require_sequence(
            directory.get("roleAssignments"), "roleAssignments"
        )
    )

    policy = await gateway.execute(
        token,
        """
        mutation Policy($policyId: String!, $content: String!) {
          setCedarPolicy(
            policyId: $policyId,
            content: $content,
            isActive: false
          )
        }
        """,
        {
            "policyId": "e2e_inactive_policy",
            "content": "permit(principal, action, resource);",
        },
    )
    assert policy.get("setCedarPolicy") is True

    conversation_data = await gateway.execute(
        token,
        """
        mutation CreateConversation($title: String!) {
          createConversation(title: $title) {
            conversationId revision title
          }
        }
        """,
        {"title": "E2E conversation"},
    )
    conversation = _field(conversation_data, "createConversation")
    conversation_id = _string(conversation, "conversationId")
    conversation_revision = _string(conversation, "revision")

    async def visible_conversation() -> bool | None:
        data = await gateway.execute(
            token,
            """
            query Conversation($conversationId: String!) {
              conversation(conversationId: $conversationId) {
                conversationId revision title
              }
              conversations { conversationId }
            }
            """,
            {"conversationId": conversation_id},
        )
        return True if data.get("conversation") is not None else None

    await eventually(
        visible_conversation,
        timeout_seconds=30.0,
        description="conversation authorization consistency",
    )

    renamed_data = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        mutation UpdateConversation(
          $conversationId: String!,
          $revision: String!,
          $title: String!
        ) {
          updateConversation(
            conversationId: $conversationId,
            expectedRevision: $revision,
            title: $title
          ) { revision title }
        }
        """,
        {
            "conversationId": conversation_id,
            "revision": conversation_revision,
            "title": "Renamed E2E conversation",
        },
        description="conversation edit authorization consistency",
    )
    renamed = _field(renamed_data, "updateConversation")
    conversation_revision = _string(renamed, "revision")

    message_data = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        mutation CreateMessage($conversationId: String!) {
          createMessage(
            conversationId: $conversationId,
            content: "E2E message"
          ) { messageId revision content }
        }
        """,
        {"conversationId": conversation_id},
        description="message creation authorization consistency",
    )
    message = _field(message_data, "createMessage")
    message_id = _string(message, "messageId")
    message_revision = _string(message, "revision")
    changed_message = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        mutation UpdateMessage(
          $conversationId: String!,
          $messageId: String!,
          $revision: String!
        ) {
          updateMessage(
            conversationId: $conversationId,
            messageId: $messageId,
            expectedRevision: $revision,
            content: "Updated E2E message"
          ) { revision content }
        }
        """,
        {
            "conversationId": conversation_id,
            "messageId": message_id,
            "revision": message_revision,
        },
        description="message edit authorization consistency",
    )
    message_revision = _string(
        _field(changed_message, "updateMessage"), "revision"
    )

    deleted_message = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        mutation DeleteMessage(
          $conversationId: String!,
          $messageId: String!,
          $revision: String!
        ) {
          deleteMessage(
            conversationId: $conversationId,
            messageId: $messageId,
            expectedRevision: $revision
          )
        }
        """,
        {
            "conversationId": conversation_id,
            "messageId": message_id,
            "revision": message_revision,
        },
        description="message deletion authorization consistency",
    )
    assert deleted_message.get("deleteMessage") is True
    latest_conversation = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        query ConversationRevision($conversationId: String!) {
          conversation(
            conversationId: $conversationId,
            includeDeletedMessages: true
          ) { revision }
        }
        """,
        {"conversationId": conversation_id},
        description="conversation read authorization consistency",
    )
    conversation_revision = _string(
        _field(latest_conversation, "conversation"), "revision"
    )
    deleted_conversation = await _execute_after_authorization_replication(
        gateway,
        token,
        """
        mutation DeleteConversation(
          $conversationId: String!, $revision: String!
        ) {
          deleteConversation(
            conversationId: $conversationId,
            expectedRevision: $revision
          )
        }
        """,
        {
            "conversationId": conversation_id,
            "revision": conversation_revision,
        },
        description="conversation deletion authorization consistency",
    )
    assert deleted_conversation.get("deleteConversation") is True

    unassigned = await gateway.execute(
        token,
        """
        mutation Unassign($userId: String!, $roleName: String!) {
          unassignRoleFromUser(userId: $userId, roleName: $roleName)
        }
        """,
        {"userId": managed_user, "roleName": managed_role},
    )
    assert unassigned.get("unassignRoleFromUser") is True
    removed_role = await gateway.execute(
        token,
        """
        mutation DeleteRole($roleName: String!) {
          deleteRole(roleName: $roleName)
        }
        """,
        {"roleName": managed_role},
    )
    assert removed_role.get("deleteRole") is True
    removed_user = await gateway.execute(
        token,
        """
        mutation DeleteUser($userId: String!) {
          deleteUser(userId: $userId)
        }
        """,
        {"userId": managed_user},
    )
    assert removed_user.get("deleteUser") is True


async def _exercise_query_api(
    gateway: GatewayClient, token: str, entity_id: str
) -> None:
    """Executes every available GraphQL Query field against live services."""
    data = await gateway.execute(
        token,
        """
        query PublicQueries(
          $entityId: String!, $pipelineId: String!
        ) {
          searchEntities(query: "gateway", limit: 10) {
            entityId metadata
          }
          globalSearch(query: "gateway", limit: 10) {
            kind entityId eventId
          }
          structuredSearch(
            input: {entityId: $entityId, limit: 10}
          ) { kind entityId }
          searchEvents(text: "gateway", limit: 10)
          searchEmbeddings(queryText: "gateway", k: 10)
          entityRelations(entityId: $entityId, depth: 2, limit: 10) {
            nodes { id label }
            edges { fromId toId label }
          }
          users(limit: 10) { userId isActive }
          roles(limit: 10) { roleName }
          roleAssignments(limit: 10) { userId roleName }
          auditEvents(limit: 100) {
            action outcome requestId traceId revisionId publicationId
          }
          ontologies(limit: 10) {
            ontologyId productionPublication { publicationId revisionId }
          }
          ontologyBindings(pipelineId: $pipelineId, limit: 10) {
            pipelineId blockId ontologyId
          }
          pipelineExecutions(pipelineId: $pipelineId, limit: 100) {
            pipelineId step status correlationId
          }
          conversations(limit: 10) { conversationId }
          pipelineDefinitions(limit: 10) {
            pipelineId headRevisionId publishedRevisionId
          }
        }
        """,
        {"entityId": entity_id, "pipelineId": PIPELINE_ID},
    )
    sequence_fields = (
        "searchEntities",
        "globalSearch",
        "structuredSearch",
        "searchEvents",
        "searchEmbeddings",
        "users",
        "roles",
        "roleAssignments",
        "auditEvents",
        "ontologies",
        "ontologyBindings",
        "pipelineExecutions",
        "conversations",
        "pipelineDefinitions",
    )
    for field in sequence_fields:
        require_sequence(data.get(field), field)
    _field(data, "entityRelations")
    assert any(
        require_mapping(item, "ontology").get("ontologyId") == ONTOLOGY_ID
        for item in require_sequence(data.get("ontologies"), "ontologies")
    )
    assert any(
        require_mapping(item, "pipeline").get("pipelineId") == PIPELINE_ID
        for item in require_sequence(
            data.get("pipelineDefinitions"), "pipelineDefinitions"
        )
    )


async def _run_gateway_upload_lifecycle() -> None:
    """Runs the bounded Gateway-to-Gateway lifecycle scenario."""
    uploader_token = mint_token(UPLOADER_ID)
    outsider_token = mint_token(OUTSIDER_ID)
    gateway = GatewayClient()
    registry = RegistryFixtures()
    spicedb = SpiceDBProbe()

    try:
        async with pipeline_environment() as environment:
            print("E2E stage: awaiting Gateway readiness", flush=True)
            await eventually(
                lambda: gateway.ready(uploader_token),
                timeout_seconds=90.0,
                description="authenticated Gateway readiness",
            )
            print("E2E stage: validating GraphQL security", flush=True)
            await _assert_graphql_security_boundaries(gateway, uploader_token)
            await _assert_public_api_inventory(gateway, uploader_token)
            await seed_users()
            await _authorize_fixture_principals(spicedb)
            await _wait_for_gateway_tenant_admin(
                gateway, spicedb, uploader_token
            )
            print("E2E stage: exercising IAM and conversations", flush=True)
            await _exercise_iam_and_conversation_api(gateway, uploader_token)
            with pytest.raises(
                AssertionError, match="GraphQL operation failed"
            ):
                await gateway.execute(
                    mint_token(UPLOADER_ID, tenant_id="isolated_e2e_tenant"),
                    'query { searchEntities(query: "E2E") { entityId } }',
                )
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

            print("E2E stage: publishing ontology", flush=True)
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
                  ) { publicationId revisionId lifecycle }
                }
                """,
                {
                    "ontologyId": ONTOLOGY_ID,
                    "displayName": "E2E ontology",
                    "revisionId": ontology_revision,
                    "metadata": json.dumps(
                        {"purpose": "pipeline-e2e"},
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                },
            )
            publication = _field(ontology_data, "publishOntology")
            assert publication.get("revisionId") == ontology_revision
            assert publication.get("lifecycle") == "production"

            print("E2E stage: publishing pipeline", flush=True)
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
                    "definition": json.dumps(
                        _pipeline(), separators=(",", ":"), sort_keys=True
                    ),
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

            async def gateway_pipeline_visible() -> bool | None:
                data = await gateway.execute(
                    uploader_token,
                    """
                    query Pipelines {
                      pipelineDefinitions { pipelineId headRevisionId }
                    }
                    """,
                )
                pipelines = require_sequence(
                    data.get("pipelineDefinitions"), "pipelineDefinitions"
                )
                return (
                    True
                    if any(
                        require_mapping(item, "pipeline definition").get(
                            "pipelineId"
                        )
                        == PIPELINE_ID
                        for item in pipelines
                    )
                    else None
                )

            await eventually(
                gateway_pipeline_visible,
                timeout_seconds=30.0,
                description="Gateway authorization consistency",
            )
            revised_data = await _execute_after_authorization_replication(
                gateway,
                uploader_token,
                """
                mutation UpdatePipeline(
                  $pipelineId: String!,
                  $revisionId: String!,
                  $definition: JSON!
                ) {
                  updatePipeline(
                    pipelineId: $pipelineId,
                    expectedHeadRevisionId: $revisionId,
                    name: "e2e_pipeline",
                    definition: $definition,
                    message: "Revise deterministic E2E pipeline"
                  ) { headRevisionId }
                }
                """,
                {
                    "pipelineId": PIPELINE_ID,
                    "revisionId": head_revision,
                    "definition": json.dumps(
                        _pipeline(), separators=(",", ":"), sort_keys=True
                    ),
                },
                description="pipeline edit authorization consistency",
            )
            head_revision = _string(
                _field(revised_data, "updatePipeline"), "headRevisionId"
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

            published_data = await _execute_after_authorization_replication(
                gateway,
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
                description="pipeline publish authorization consistency",
            )
            published_pipeline = _field(published_data, "publishPipeline")
            published_revision = head_revision
            assert (
                published_pipeline.get("publishedRevisionId")
                == published_revision
            )
            head_revision = _string(published_pipeline, "headRevisionId")
            assert head_revision != published_revision
            runtime_definition = await registry.get_runtime_pipeline(
                published_revision
            )
            assert json.loads(runtime_definition) == _pipeline()

            await environment.start_vision()
            print("E2E stage: awaiting Vision runtime", flush=True)
            await eventually(
                vision_runtime_ready,
                timeout_seconds=VISION_RUNTIME_READY_TIMEOUT_SECONDS,
                description="Vision database and Kafka consumers",
            )
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

            print("E2E stage: awaiting terminal pipeline state", flush=True)
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
            assert source_raw_id == canonical_spicedb_object_id(object_key)

            print("E2E stage: exercising GraphQL query API", flush=True)
            await _exercise_query_api(gateway, uploader_token, state.entity_id)

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

            print("E2E stage: verifying lineage events", flush=True)
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

            print("E2E stage: verifying distributed traces", flush=True)
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

            print("E2E stage: retiring Registry resources", flush=True)
            deleted_pipeline = await gateway.execute(
                uploader_token,
                """
                mutation DeletePipeline(
                  $pipelineId: String!, $revisionId: String!
                ) {
                  deletePipeline(
                    pipelineId: $pipelineId,
                    expectedHeadRevisionId: $revisionId
                  )
                }
                """,
                {
                    "pipelineId": PIPELINE_ID,
                    "revisionId": head_revision,
                },
            )
            assert deleted_pipeline.get("deletePipeline") is True
            retired_ontology = await gateway.execute(
                uploader_token,
                """
                mutation RetireOntology(
                  $ontologyId: String!,
                  $publicationId: String!,
                  $revisionId: String!
                ) {
                  retireOntology(
                    ontologyId: $ontologyId,
                    publicationId: $publicationId,
                    revisionId: $revisionId
                  )
                }
                """,
                {
                    "ontologyId": ONTOLOGY_ID,
                    "publicationId": _string(publication, "publicationId"),
                    "revisionId": ontology_revision,
                },
            )
            assert retired_ontology.get("retireOntology") is True
    finally:
        await registry.close()
        await gateway.close()


async def test_gateway_upload_reaches_authorized_gateway_access() -> None:
    """Proves data, authorization, lineage, and traces survive the full DAG."""
    try:
        await asyncio.wait_for(
            _run_gateway_upload_lifecycle(),
            timeout=PIPELINE_LIFECYCLE_TIMEOUT_SECONDS,
        )
    except TimeoutError as error:
        raise AssertionError(
            "The Gateway lifecycle exceeded its 1800-second internal deadline"
        ) from error


if __name__ == "__main__":
    raise SystemExit(
        pytest.main([__file__, "--import-mode=importlib", "--capture=no"])
    )
