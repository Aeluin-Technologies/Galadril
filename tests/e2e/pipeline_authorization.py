"""Gateway GraphQL authentication, inventory and live authorization contracts."""

from __future__ import annotations

import pytest
from assertions import (
    require_mapping,
    require_sequence,
)
from authentication import expired_token
from clients import (
    TENANT_ID,
    UPLOADER_ID,
    GatewayClient,
    GraphQLOperationError,
    SpiceDBProbe,
    authenticated_token,
)
from gateway_scenario import (
    GatewayScenario,
    _field,
    _string,
    revoke_fixture_admin,
)

_QUERY_FIELDS = frozenset(
    {
        "administration",
        "conversation",
        "conversations",
        "entityRelations",
        "globalSearch",
        "globalSearchPage",
        "generationEvents",
        "golOntologies",
        "golOntologyCanManage",
        "golOntologyPackages",
        "golOntologyWorkspace",
        "golOntologySymbols",
        "golOntologySchema",
        "ontologyBindings",
        "pipelineCapabilities",
        "pipelineDefinition",
        "pipelineLinkTargets",
        "pipelineComposition",
        "pipelineDiagnostics",
        "pipelineDefinitions",
        "pipelineExecutions",
        "searchEmbeddings",
        "searchEntities",
        "searchEvents",
        "scribeCapabilities",
        "scribeModels",
        "structuredSearch",
        "structuredSearchPage",
    }
)
_MUTATION_FIELDS = frozenset(
    {
        "administration",
        "completeUpload",
        "createConversation",
        "createMessage",
        "createPipeline",
        "authorPipeline",
        "deleteConversation",
        "deleteMessage",
        "deletePipeline",
        "installGolOntologyPackage",
        "saveGolOntology",
        "validateGolOntology",
        "publishGolOntology",
        "deleteGolOntology",
        "bindGolOntology",
        "publishPipeline",
        "requestStagingUpload",
        "updateConversation",
        "updateMessage",
        "updatePipeline",
    }
)


async def _assert_gateway_tenant_admin(
    gateway: GatewayClient, spicedb: SpiceDBProbe, token: str
) -> None:
    """Requires fixture writes to be visible at the Gateway boundary."""
    assert await spicedb.allowed(
        resource_type="tenant",
        resource_id=TENANT_ID,
        permission="manage",
        user_id=UPLOADER_ID,
    )
    data = await gateway.execute(
        token,
        "query TenantAdmin { administration { users(limit: 1) { items { userId } } }}",
    )
    require_sequence(
        _field(_field(data, "administration"), "users").get("items"),
        "users items",
    )


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
        await expired_token(UPLOADER_ID),
        "query { __typename }",
    )
    assert expired.status_code == 401

    oversized = await gateway.request(
        token,
        "query { __typename }" + (" " * (64 * 1024 + 1)),
    )
    assert oversized.status_code == 413

    nested = await gateway.request(
        token,
        'query ExcessiveDepth { __type(name:"PengoloChat") { '
        + "ofType { " * 8
        + "kind"
        + " }" * 10,
    )
    assert nested.status_code == 400
    assert "configured limit of 8" in nested.text


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
          administrationQuery: __type(name: "AdministrationQuery") { fields { name } }
          administrationMutation: __type(name: "AdministrationMutation") { fields { name } }
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

    def assert_fields(
        root_name: str, expected: frozenset[str] | set[str]
    ) -> None:
        actual = field_names(root_name)
        assert actual == expected, (
            f"{root_name} inventory differs: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )

    expected_queries = _QUERY_FIELDS | {
        "pengoloIdentity",
        "pengoloModels",
        "pengoloOpenai",
        "pengoloPreferences",
        "pengoloDocumentProfile",
        "pengoloSigningVault",
        "pengoloHandwrittenSignatures",
        "pengoloHandwrittenSignatureTrace",
        "pengoloProjects",
        "pengoloAgents",
        "pengoloChats",
        "pengoloChat",
        "pengoloLibrary",
        "documentSearch",
        "evidencePage",
        "pengoloEvents",
    }
    assert_fields("queryType", expected_queries)
    expected_mutations = _MUTATION_FIELDS | {
        "emailOperation",
        "updatePengoloOpenai",
        "updatePengoloPreferences",
        "savePengoloDocumentProfile",
        "createPengoloSigningVault",
        "createPengoloHandwrittenSignature",
        "deletePengoloHandwrittenSignature",
        "updatePengoloHandwrittenSignatureCaption",
        "answerPengoloQuestion",
        "sendPengolo",
        "cancelPengolo",
        "savePengoloProject",
        "deletePengoloProject",
        "savePengoloAgent",
        "indexDocument",
        "deletePengoloAgent",
        "updatePengoloChat",
        "deletePengoloChat",
        "closePengoloTemporary",
        "beginPengoloUpload",
        "finishPengoloUpload",
        "deletePengoloFile",
        "pengoloDownload",
    }
    assert_fields("mutationType", expected_mutations)
    expected_subscriptions = {
        "ask",
        "pengoloStream",
        "pengoloIdentityChanges",
        "pengoloLibraryChanges",
    }
    assert_fields("subscriptionType", expected_subscriptions)
    expected_admin_queries = {
        "auditEvents",
        "ssoSettings",
        "users",
        "roles",
        "roleAssignments",
        "accessGraph",
        "cedarPolicies",
        "roleSyncConfigurations",
    }
    expected_admin_mutations = {
        "configureRoleSync",
        "detectSsoProvider",
        "requestSsoDomain",
        "configureSsoConnection",
        "disableSsoConnection",
        "linkExternalIdentity",
        "setPermissionImport",
        "updateOrganization",
        "setPasskeyMfa",
        "resetUserPasskeys",
        "importPermissions",
        "createUser",
        "setDataDomainGrant",
        "setDirectoryDisplayName",
        "updateUser",
        "deleteUser",
        "createRole",
        "deleteRole",
        "assignRoleToUser",
        "unassignRoleFromUser",
        "setCedarPolicy",
        "setRoleParent",
        "setResourceAccess",
        "configureGeneralAccess",
    }
    for alias, expected in (
        ("administrationQuery", expected_admin_queries),
        ("administrationMutation", expected_admin_mutations),
    ):
        actual = {
            _string(require_mapping(field, alias), "name")
            for field in require_sequence(
                _field(data, alias).get("fields"), alias
            )
        }
        assert actual == expected, {
            "type": alias,
            "missing": sorted(expected - actual),
            "unexpected": sorted(actual - expected),
        }


async def _exercise_iam_and_conversation_api(
    gateway: GatewayClient, token: str
) -> None:
    """Exercises available GraphQL CRUD fields without invoking chat."""
    managed_user = "e2e_managed_user"
    managed_role = "e2e_managed_role"
    created = await gateway.execute(
        token,
        """
        mutation CreateUser($userId: String!) { administration {
          createUser(userId: $userId, isActive: false)
         }}
        """,
        {"userId": managed_user},
    )
    assert (
        require_mapping(created.get("administration"), "administration").get(
            "createUser"
        )
        is True
    )
    updated = await gateway.execute(
        token,
        """
        mutation UpdateUser($userId: String!) { administration {
          updateUser(userId: $userId, isActive: true)
         }}
        """,
        {"userId": managed_user},
    )
    assert (
        require_mapping(updated.get("administration"), "administration").get(
            "updateUser"
        )
        is True
    )
    role = await gateway.execute(
        token,
        """
        mutation CreateRole($roleName: String!) { administration {
          createRole(roleName: $roleName)
         }}
        """,
        {"roleName": managed_role},
    )
    assert (
        require_mapping(role.get("administration"), "administration").get(
            "createRole"
        )
        is True
    )
    for subject_type, subject_id, display_name in (
        ("user", managed_user, "Managed person"),
        ("role", managed_role, "Managed role"),
    ):
        labeled = await gateway.execute(
            token,
            """
            mutation Label($kind: String!, $id: String!, $name: String!) {
              administration {
                setDirectoryDisplayName(
                  subjectType: $kind, subjectId: $id, displayName: $name
                )
              }
            }
            """,
            {"kind": subject_type, "id": subject_id, "name": display_name},
        )
        assert (
            _field(labeled, "administration").get("setDirectoryDisplayName")
            is True
        )

    assigned = await gateway.execute(
        token,
        """
        mutation Assign($userId: String!, $roleName: String!) { administration {
          assignRoleToUser(userId: $userId, roleName: $roleName)
         }}
        """,
        {"userId": managed_user, "roleName": managed_role},
    )
    assert (
        require_mapping(assigned.get("administration"), "administration").get(
            "assignRoleToUser"
        )
        is True
    )

    directory = await gateway.execute(
        token,
        """
        query Directory { administration {
          users(limit: 100) { items { userId displayName isActive } after }
          roles(limit: 100) { items { roleName displayName } after }
          roleAssignments { userId roleName }
         }}
        """,
    )
    directory_users = _field(_field(directory, "administration"), "users")
    directory_roles = _field(_field(directory, "administration"), "roles")
    assert directory_users.get("after") is None, (
        "Fixture users exceed the directory page"
    )
    assert directory_roles.get("after") is None, (
        "Fixture roles exceed the directory page"
    )
    assert any(
        require_mapping(user, "user").get("userId") == managed_user
        and require_mapping(user, "user").get("displayName") == "Managed person"
        for user in require_sequence(
            directory_users.get("items"), "users items"
        )
    )
    assert any(
        require_mapping(item, "role").get("roleName") == managed_role
        and require_mapping(item, "role").get("displayName") == "Managed role"
        for item in require_sequence(
            directory_roles.get("items"), "roles items"
        )
    )
    assert any(
        require_mapping(item, "assignment").get("userId") == managed_user
        for item in require_sequence(
            require_mapping(
                directory.get("administration"), "administration"
            ).get("roleAssignments"),
            "roleAssignments",
        )
    )

    policy = await gateway.execute(
        token,
        """
        mutation Policy($policyId: String!, $content: String!) { administration {
          setCedarPolicy(
            policyId: $policyId,
            content: $content,
            isActive: false
          )
         }}
        """,
        {
            "policyId": "e2e_inactive_policy",
            "content": "permit(principal, action, resource);",
        },
    )
    assert (
        require_mapping(policy.get("administration"), "administration").get(
            "setCedarPolicy"
        )
        is True
    )

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

    visible = await gateway.execute(
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
    assert (
        _field(visible, "conversation").get("conversationId") == conversation_id
    )

    renamed_data = await gateway.execute(
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
    )
    renamed = _field(renamed_data, "updateConversation")
    assert _string(renamed, "revision") != conversation_revision

    message_data = await gateway.execute(
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
    )
    message = _field(message_data, "createMessage")
    message_id = _string(message, "messageId")
    message_revision = _string(message, "revision")
    changed_message = await gateway.execute(
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
    )
    message_revision = _string(
        _field(changed_message, "updateMessage"), "revision"
    )

    deleted_message = await gateway.execute(
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
    )
    assert deleted_message.get("deleteMessage") is True
    latest_conversation = await gateway.execute(
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
    )
    conversation_revision = _string(
        _field(latest_conversation, "conversation"), "revision"
    )
    deleted_conversation = await gateway.execute(
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
    )
    assert deleted_conversation.get("deleteConversation") is True

    unassigned = await gateway.execute(
        token,
        """
        mutation Unassign($userId: String!, $roleName: String!) { administration {
          unassignRoleFromUser(userId: $userId, roleName: $roleName)
         }}
        """,
        {"userId": managed_user, "roleName": managed_role},
    )
    assert (
        require_mapping(unassigned.get("administration"), "administration").get(
            "unassignRoleFromUser"
        )
        is True
    )
    removed_role = await gateway.execute(
        token,
        """
        mutation DeleteRole($roleName: String!) { administration {
          deleteRole(roleName: $roleName)
         }}
        """,
        {"roleName": managed_role},
    )
    assert (
        require_mapping(
            removed_role.get("administration"), "administration"
        ).get("deleteRole")
        is True
    )
    removed_user = await gateway.execute(
        token,
        """
        mutation DeleteUser($userId: String!) { administration {
          deleteUser(userId: $userId)
         }}
        """,
        {"userId": managed_user},
    )
    assert (
        require_mapping(
            removed_user.get("administration"), "administration"
        ).get("deleteUser")
        is True
    )


async def exercise_gateway_authorization(scenario: GatewayScenario) -> None:
    """Runs the public GraphQL authentication, inventory and administration contracts."""
    gateway = scenario.gateway
    uploader_token = scenario.uploader_token
    await _assert_graphql_security_boundaries(gateway, uploader_token)
    await _assert_public_api_inventory(gateway, uploader_token)
    await _assert_gateway_tenant_admin(
        gateway, scenario.spicedb, uploader_token
    )
    await _exercise_iam_and_conversation_api(gateway, uploader_token)
    with pytest.raises(AssertionError, match="GraphQL operation failed"):
        await gateway.execute(
            scenario.outsider_token,
            """
            mutation RequestUpload {
              requestStagingUpload { uploadUrl stagingKey }
            }
            """,
        )


async def exercise_revoked_administrator(
    scenario: GatewayScenario, administrator: str
) -> None:
    """Checks denial and the remaining member directory after live role revocation."""
    gateway = scenario.gateway
    uploader_token = authenticated_token(UPLOADER_ID)
    await revoke_fixture_admin(scenario)
    with pytest.raises(GraphQLOperationError) as denied:
        await gateway.execute(
            uploader_token,
            "query RevokedAdmin { administration { cedarPolicies { id } }}",
        )
    denied.value.require_forbidden(("administration", "cedarPolicies"))
    directory = await gateway.execute(
        uploader_token,
        "query MemberDirectory { administration { users(limit: 100) { items { userId } after } }}",
    )
    member_page = _field(_field(directory, "administration"), "users")
    assert member_page.get("after") is None, (
        "Fixture members exceed the directory page"
    )
    members = require_sequence(member_page.get("items"), "users items")
    assert any(
        require_mapping(member, "directory member").get("userId") == UPLOADER_ID
        for member in members
    )
    assert all(
        require_mapping(member, "directory member").get("userId")
        != administrator
        for member in members
    )
