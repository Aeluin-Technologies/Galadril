"""Checks Pengolo privacy and detached execution against the deployed services."""

import base64
import hashlib
import json
import uuid
from collections.abc import Mapping

import httpx
import psycopg
from assertions import eventually, require_mapping, require_sequence
from clients import (
    OUTSIDER_ID,
    POSTGRES_DSN,
    TENANT_ID,
    UPLOADER_ID,
    DerivedPipelineState,
    GatewayClient,
    GraphQLOperationError,
    authenticated_token,
    gateway_tls_context,
    read_causal_cohort,
)
from pengolo_browser import exercise_pengolo_browser

_METADATA = "id ownerId projectId expiresAt revision updatedAt"
_CHAT = f"{{ {_METADATA} data {{ title pinned activeGeneration }} }}"
_PROJECT = (
    f"{{ {_METADATA} data {{ name icon color shared roles contextMode }} }}"
)
_FILE = f"{{ {_METADATA} data {{ name mime size status originalName temporarySession }} }}"
_INPUT_TYPES = {
    "sendPengolo": "PengoloSendInput",
    "updatePengoloChat": "PengoloChatInput",
    "deletePengoloChat": "PengoloChatInput",
    "cancelPengolo": "PengoloCancelInput",
    "closePengoloTemporary": "PengoloTemporaryInput",
    "savePengoloProject": "PengoloProjectInput",
    "deletePengoloProject": "PengoloProjectDeletionInput",
    "beginPengoloUpload": "PengoloUploadInput",
    "finishPengoloUpload": "PengoloFileInput",
    "deletePengoloFile": "PengoloFileInput",
    "pengoloDownload": "PengoloFileInput",
}
_SELECTIONS = {
    "sendPengolo": f"{{ chat {_CHAT} generation {{ id data {{ status chatId sequence }} }} userMessage {{ id text fileIds }} }}",
    "updatePengoloChat": _CHAT,
    "savePengoloProject": _PROJECT,
    "beginPengoloUpload": f"{{ url headers {{ name value }} file {_FILE} }}",
    "finishPengoloUpload": _FILE,
    "pengoloDownload": "{ url }",
}


def _mutation_document(field: str) -> str:
    return f"mutation Pengolo($input: {_INPUT_TYPES[field]}!) {{ {field}(input: $input) {_SELECTIONS.get(field, '')} }}"


def _object(value: object) -> dict[str, object]:
    return dict(require_mapping(value, "Pengolo object"))


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    assert isinstance(result, str) and result
    return result


async def _mutation(
    gateway: GatewayClient,
    field: str,
    value: Mapping[str, object],
    *,
    actor: str = UPLOADER_ID,
) -> object:
    result = await gateway.execute(
        authenticated_token(actor),
        _mutation_document(field),
        {"input": value},
    )
    response = result.get(field)
    return response


async def _object_mutation(
    gateway: GatewayClient,
    field: str,
    value: Mapping[str, object],
    *,
    actor: str = UPLOADER_ID,
) -> dict[str, object]:
    return _object(await _mutation(gateway, field, value, actor=actor))


async def _denied(
    gateway: GatewayClient,
    field: str,
    value: Mapping[str, object],
    *,
    actor: str = UPLOADER_ID,
    tenant: str = TENANT_ID,
) -> None:
    result = await gateway.request(
        authenticated_token(actor, tenant_id=tenant),
        _mutation_document(field),
        {"input": value},
    )
    assert result.status_code == 200
    errors = require_sequence(
        require_mapping(result.json(), "GraphQL denial").get("errors"), "errors"
    )
    assert len(errors) == 1
    error = require_mapping(errors[0], "denied field")
    assert error.get("path") == [field]
    assert (
        require_mapping(error.get("extensions"), "error code").get("code")
        == "REQUEST_FAILED"
    )


async def _invalid_input(
    gateway: GatewayClient,
    field: str,
    value: Mapping[str, object],
    unknown_field: str,
) -> None:
    """Requires schema rejection rather than accepting an unrelated denial."""
    response = await gateway.request(
        authenticated_token(UPLOADER_ID),
        _mutation_document(field),
        {"input": value},
    )
    assert response.status_code == 400
    payload = require_mapping(response.json(), "GraphQL validation response")
    assert payload.get("data") is None
    errors = require_sequence(payload.get("errors"), "validation errors")
    assert len(errors) == 1
    message = require_mapping(errors[0], "validation error").get("message")
    assert isinstance(message, str)
    assert f'In field "{unknown_field}": Unknown field' in message


async def _events(
    gateway: GatewayClient, identifier: str, *, temporary: bool = False
) -> dict[str, object]:
    result = await gateway.execute(
        authenticated_token(UPLOADER_ID),
        "query Events($id:String!, $temporary:Boolean!) { pengoloEvents(generationId:$id, after:0, temporary:$temporary) { generationId status events { sequence kind content } message { id role text fileIds status } chatRevision chatUpdatedAt } }",
        {"id": identifier, "temporary": temporary},
    )
    return _object(result.get("pengoloEvents"))


class GenerationFailure(RuntimeError):
    """A terminal generation cannot recover through further polling."""


async def _completed(
    gateway: GatewayClient, identifier: str, *, temporary: bool = False
) -> dict[str, object] | None:
    page = await _events(gateway, identifier, temporary=temporary)
    if page.get("status") in {"failed", "cancelled"}:
        raise GenerationFailure(f"Pengolo generation is terminal: {page}")
    return page if page.get("status") == "completed" else None


def _graph_unavailable(page: Mapping[str, object]) -> bool | None:
    """Requires graceful recovery without forwarding denied graph evidence."""
    assert page.get("status") not in {"failed", "cancelled"}, (
        "An unavailable graph must not abort the conversation"
    )
    if page.get("status") != "completed":
        return None
    events = [
        require_mapping(event, "event")
        for event in require_sequence(page.get("events"), "events")
    ]
    assert any(
        event.get("kind") == "activity"
        and json.loads(_text(event, "content"))
        == {"type": "warning", "source": "graph"}
        for event in events
    ), "An unavailable private graph must produce an explicit warning"
    answer = require_mapping(
        json.loads(
            "".join(
                _text(event, "content")
                for event in events
                if event.get("kind") == "content"
            )
        ),
        "fixture answer",
    )
    fixture = require_mapping(answer.get("_fixture"), "model evidence receipt")
    assert fixture.get("graph_context_received") is False, (
        "Project membership must not grant private graph access"
    )
    return True


async def _upload(
    gateway: GatewayClient,
    *,
    project: str | None = None,
    temporary: str | None = None,
    content: bytes = b"Confidential operational evidence for the authorized library.",
    name: str = "evidence.txt",
) -> dict[str, object]:
    result = await _object_mutation(
        gateway,
        "beginPengoloUpload",
        {
            "name": name,
            "mime": "text/plain",
            "size": len(content),
            "checksum": base64.b64encode(
                hashlib.sha256(content).digest()
            ).decode(),
            "projectId": project,
            "temporarySession": temporary,
        },
    )
    file = _object(result.get("file"))
    url = _text(result, "url")
    assert (
        "/staging/" in url
        and "key" not in _object(file.get("data"))
        and "stagingKey" not in _object(file.get("data"))
    )
    headers = {
        _text(require_mapping(header, "upload header"), "name"): _text(
            require_mapping(header, "upload header"), "value"
        )
        for header in require_sequence(result.get("headers"), "upload headers")
    }
    async with httpx.AsyncClient(
        verify=gateway_tls_context(), timeout=30
    ) as client:
        for header, value in (
            ("x-amz-meta-tenant", "another-tenant"),
            ("content-type", "image/png"),
            ("content-disposition", "attachment; filename=changed.txt"),
        ):
            altered = dict(headers)
            altered[header] = value
            refused = await client.put(url, content=content, headers=altered)
            assert refused.status_code in {400, 403}, (
                header,
                refused.status_code,
            )
        corrupt = await client.put(
            url, content=b"x" * len(content), headers=headers
        )
        assert corrupt.status_code in {400, 403}, (
            "Checksum must bind the actual bytes"
        )
        uploaded = await client.put(url, content=content, headers=headers)
        uploaded.raise_for_status()
        duplicate = await client.put(url, content=content, headers=headers)
        assert duplicate.status_code == 412, "Uploads must be immutable"
    ready = await _object_mutation(
        gateway,
        "finishPengoloUpload",
        {"id": _text(file, "id"), "temporarySession": temporary},
    )
    assert _object(ready.get("data")).get("status") == "ready"
    return ready


async def _exercise_project_permissions(
    gateway: GatewayClient, administrator_token: str
) -> None:
    """Checks typed project edits against inherited access and stale revisions."""
    staff, lead = "pengolo_staff", "pengolo_lead"
    await gateway.execute(
        administrator_token,
        'mutation { administration { staff:createRole(roleName:"pengolo_staff") lead:createRole(roleName:"pengolo_lead") } }',
    )
    await gateway.execute(
        administrator_token,
        'mutation { administration { setRoleParent(roleName:"pengolo_staff",parentRoleName:"pengolo_lead",enabled:true) } }',
    )
    for user, role in ((UPLOADER_ID, staff), (OUTSIDER_ID, lead)):
        await gateway.execute(
            administrator_token,
            "mutation Assign($user:String!,$role:String!) { administration { assignRoleToUser(userId:$user,roleName:$role) } }",
            {"user": user, "role": role},
        )
    settings: dict[str, object] = {
        "name": "Hierarchy protected project",
        "icon": "📁",
        "color": "#d97706",
        "shared": True,
        "roles": [staff, "pengolo_reader"],
        "contextMode": "project",
    }
    identity_before = await gateway.execute(
        authenticated_token(UPLOADER_ID),
        "query { pengoloIdentity { version } }",
    )
    project = await _object_mutation(gateway, "savePengoloProject", settings)
    settings.update(id=_text(project, "id"), revision=project.get("revision"))
    identity_after = await gateway.execute(
        authenticated_token(UPLOADER_ID),
        "query { pengoloIdentity { version } }",
    )
    assert identity_after == identity_before, (
        "Owned project creation must preserve drafts"
    )
    await _denied(
        gateway,
        "savePengoloProject",
        {**settings, "roles": ["pengolo_reader"]},
    )
    await _denied(gateway, "savePengoloProject", settings, actor=OUTSIDER_ID)
    renamed = await _object_mutation(
        gateway,
        "savePengoloProject",
        {**settings, "name": "Renamed investigation"},
    )
    await _denied(gateway, "savePengoloProject", settings)
    promoted = await _object_mutation(
        gateway,
        "savePengoloProject",
        {**settings, "revision": renamed.get("revision"), "roles": [lead]},
    )
    assert _object(promoted.get("data")).get("roles") == [lead]
    visible = await gateway.execute(
        authenticated_token(OUTSIDER_ID), "query { pengoloProjects { id } }"
    )
    assert any(
        require_mapping(item, "project").get("id") == project.get("id")
        for item in require_sequence(visible.get("pengoloProjects"), "projects")
    )

    deletion = {
        "id": _text(promoted, "id"),
        "revision": promoted.get("revision"),
    }
    await _denied(gateway, "deletePengoloProject", deletion, actor=OUTSIDER_ID)
    await _denied(
        gateway, "deletePengoloProject", deletion, tenant="other_tenant"
    )
    await _denied(
        gateway,
        "deletePengoloProject",
        {**deletion, "revision": project.get("revision")},
    )
    assert await _mutation(gateway, "deletePengoloProject", deletion) is True
    for actor in (UPLOADER_ID, OUTSIDER_ID):
        remaining = await gateway.execute(
            authenticated_token(actor), "query { pengoloProjects { id } }"
        )
        assert all(
            require_mapping(item, "project").get("id") != deletion["id"]
            for item in require_sequence(
                remaining.get("pengoloProjects"), "projects"
            )
        )
    await _denied(gateway, "deletePengoloProject", deletion)
    await _denied(
        gateway,
        "savePengoloProject",
        {**settings, "revision": promoted.get("revision"), "roles": [lead]},
    )


async def exercise_pengolo(
    gateway: GatewayClient, state: DerivedPipelineState, *, administrator: str
) -> None:
    """Uses real auth, roles, S3, PostgreSQL and Scribe without provider credentials."""
    token = authenticated_token(UPLOADER_ID)
    administrator_token = authenticated_token(administrator)
    result = await gateway.execute(
        token,
        "query { pengoloModels { models { alias reasoningEfforts } } pengoloOpenai { enabled connected accounts { id } } }",
    )
    assert result.get("pengoloOpenai") == {
        "enabled": False,
        "connected": False,
        "accounts": [],
    }
    models = require_sequence(
        require_mapping(result.get("pengoloModels"), "catalogue").get("models"),
        "models",
    )
    assert models == [{"alias": "fixture", "reasoningEfforts": ["medium"]}]
    project = await _object_mutation(
        gateway,
        "savePengoloProject",
        {
            "name": "Operations",
            "icon": "📊",
            "color": "#d97706",
            "shared": False,
            "roles": [],
            "contextMode": "project",
        },
    )
    project_id = _text(project, "id")
    personal = await _upload(gateway)
    await _denied(
        gateway,
        "pengoloDownload",
        {"id": _text(personal, "id")},
        actor=OUTSIDER_ID,
    )
    await _denied(
        gateway,
        "beginPengoloUpload",
        {
            "name": "evidence.txt",
            "mime": "text/plain",
            "size": 10,
            "checksum": base64.b64encode(bytes(32)).decode(),
            "projectId": project_id,
        },
        actor=OUTSIDER_ID,
    )
    await _denied(
        gateway,
        "savePengoloProject",
        {
            "name": "Unsafe",
            "icon": "📊",
            "color": "#d97706",
            "shared": True,
            "roles": ["reader"],
            "contextMode": "personal",
        },
    )
    for tenant in ("isolated_e2e_tenant", "other_tenant"):
        await _denied(
            gateway,
            "pengoloDownload",
            {"id": _text(personal, "id")},
            tenant=tenant,
        )
        await _denied(
            gateway,
            "deletePengoloFile",
            {"id": _text(personal, "id")},
            tenant=tenant,
        )
    duplicate = await _upload(gateway)
    assert _text(duplicate, "id") != _text(personal, "id")
    assert _object(duplicate.get("data")).get("name") == "evidence (1).txt"
    assert (
        await _object_mutation(
            gateway, "finishPengoloUpload", {"id": _text(duplicate, "id")}
        )
        == duplicate
    )
    project_file = await _upload(gateway, project=project_id)
    operation = (
        'mutation { administration { createRole(roleName:"pengolo_reader") } }'
    )
    try:
        await gateway.execute(token, operation)
    except GraphQLOperationError as denied:
        denied.require_forbidden(("administration", "createRole"))
    else:
        raise AssertionError("Ordinary project owners cannot create roles")
    await gateway.execute(administrator_token, operation)
    await _exercise_project_permissions(gateway, administrator_token)
    shared = await _object_mutation(
        gateway,
        "savePengoloProject",
        {
            "name": "Shared operations",
            "icon": "📊",
            "color": "#d97706",
            "shared": True,
            "roles": ["pengolo_reader"],
            "contextMode": "project",
        },
    )
    shared_file = await _upload(gateway, project=_text(shared, "id"))
    await _denied(
        gateway,
        "pengoloDownload",
        {"id": _text(shared_file, "id")},
        actor=OUTSIDER_ID,
    )
    before = await gateway.execute(
        authenticated_token(OUTSIDER_ID), "query { pengoloProjects { id } }"
    )
    assert _text(shared, "id") not in str(before.get("pengoloProjects"))
    await gateway.execute(
        administrator_token,
        f'mutation {{ administration {{ assignRoleToUser(userId:"{OUTSIDER_ID}",roleName:"pengolo_reader") }} }}',
    )
    after = await gateway.execute(
        authenticated_token(OUTSIDER_ID), "query { pengoloProjects { id } }"
    )
    assert _text(shared, "id") in str(after.get("pengoloProjects"))
    causal = await read_causal_cohort()
    assert causal is not None
    shared_prompt = json.dumps(
        {
            "entity_id": state.entity_id,
            "causal_entity_id": causal,
            "question": "Review the private graph context",
            "calculator_only": True,
            "pengolo": True,
        }
    )
    accessible = await _object_mutation(
        gateway,
        "sendPengolo",
        {
            "idempotencyKey": str(uuid.uuid4()),
            "projectId": _text(shared, "id"),
            "prompt": shared_prompt,
            "modelAlias": "fixture",
        },
        actor=OUTSIDER_ID,
    )
    await eventually(
        lambda: _completed(
            gateway, _text(_object(accessible.get("generation")), "id")
        ),
        timeout_seconds=30,
        description="shared project without private graph context",
        abort_on=(GenerationFailure,),
    )
    restricted = await _object_mutation(
        gateway,
        "sendPengolo",
        {
            "idempotencyKey": str(uuid.uuid4()),
            "projectId": _text(shared, "id"),
            "prompt": shared_prompt,
            "modelAlias": "fixture",
            "graphContext": {"kind": "entity", "id": state.entity_id},
        },
        actor=OUTSIDER_ID,
    )

    async def graph_denied() -> bool | None:
        result = await gateway.execute(
            authenticated_token(OUTSIDER_ID),
            "query Events($id:String!) { pengoloEvents(generationId:$id,after:0) { status events { sequence kind content } } }",
            {"id": _text(_object(restricted.get("generation")), "id")},
        )
        page = _object(result.get("pengoloEvents"))
        return _graph_unavailable(page)

    await eventually(
        graph_denied,
        timeout_seconds=30,
        description="independent private graph authorization",
        abort_on=(AssertionError,),
    )
    download = await _object_mutation(
        gateway,
        "pengoloDownload",
        {"id": _text(shared_file, "id")},
        actor=OUTSIDER_ID,
    )
    async with httpx.AsyncClient(
        base_url="https://127.0.0.1:18080",
        verify=gateway_tls_context(),
        timeout=30,
    ) as client:
        downloaded = await client.get(
            _text(download, "url"),
            headers={
                "authorization": "Bearer " + authenticated_token(OUTSIDER_ID)
            },
        )
        assert (
            downloaded.status_code == 200
            and "no-store" in downloaded.headers.get("cache-control", "")
        )
        await gateway.execute(
            administrator_token,
            f'mutation {{ administration {{ unassignRoleFromUser(userId:"{OUTSIDER_ID}",roleName:"pengolo_reader") }} }}',
        )
        revoked = await client.get(
            _text(download, "url"),
            headers={
                "authorization": "Bearer " + authenticated_token(OUTSIDER_ID)
            },
        )
        assert revoked.status_code == 404
    await _denied(
        gateway,
        "pengoloDownload",
        {"id": _text(shared_file, "id")},
        actor=OUTSIDER_ID,
    )
    await _denied(
        gateway, "pengoloDownload", {"id": "../other-tenant/private.pdf"}
    )
    await _denied(
        gateway,
        "beginPengoloUpload",
        {
            "name": "../private.pdf",
            "mime": "application/pdf",
            "size": 100,
            "checksum": base64.b64encode(bytes(32)).decode(),
        },
    )
    await _invalid_input(
        gateway,
        "beginPengoloUpload",
        {
            "name": "private.txt",
            "mime": "text/plain",
            "size": 100,
            "checksum": base64.b64encode(bytes(32)).decode(),
            "key": "other/private.txt",
        },
        "key",
    )
    plan = json.dumps(
        {
            "entity_id": state.entity_id,
            "causal_entity_id": causal,
            "question": "Review the operational evidence",
            "pengolo": True,
        }
    )
    base: dict[str, object] = {
        "idempotencyKey": str(uuid.uuid4()),
        "prompt": plan,
        "modelAlias": "fixture",
        "graphContext": {"kind": "entity", "id": state.entity_id},
    }
    await _invalid_input(
        gateway,
        "sendPengolo",
        {**base, "reasoningEffort": "high"},
        "reasoningEffort",
    )
    await _denied(
        gateway,
        "sendPengolo",
        {**base, "projectId": project_id, "fileIds": [_text(personal, "id")]},
    )
    documents = await _object_mutation(
        gateway,
        "sendPengolo",
        {
            **base,
            "idempotencyKey": str(uuid.uuid4()),
            "fileIds": [_text(personal, "id")],
            "prompt": json.dumps(
                {
                    "entity_id": state.entity_id,
                    "causal_entity_id": causal,
                    "question": "EVDIÉNCE",
                    "library": True,
                }
            ),
        },
    )
    document_page = await eventually(
        lambda: _completed(
            gateway, _text(_object(documents.get("generation")), "id")
        ),
        timeout_seconds=90,
        description="selected library file and direct attachment",
        abort_on=(GenerationFailure,),
    )
    document_answer = _object(
        json.loads(
            "".join(
                str(require_mapping(event, "document event").get("content"))
                for event in require_sequence(
                    document_page.get("events"), "document output"
                )
                if require_mapping(event, "document event").get("kind")
                == "content"
            )
        )
    )
    metadata = require_sequence(
        _object(document_answer.get("library")).get("files"), "library metadata"
    )
    assert metadata and all(
        "attachment" not in require_mapping(file, "library file")
        for file in metadata
    )
    chosen = _text(_object(document_answer.get("read_file")), "id")
    assert chosen == _text(
        require_mapping(metadata[0], "selected library file"), "id"
    )
    assert chosen in {_text(personal, "id"), _text(duplicate, "id")}
    assert (
        json.dumps(document_answer.get("_documents")).count(
            "Confidential operational evidence"
        )
        >= 2
    )
    first = await _object_mutation(gateway, "sendPengolo", base)
    retry = await _object_mutation(gateway, "sendPengolo", base)
    assert _text(_object(first.get("chat")), "id") == _text(
        _object(retry.get("chat")), "id"
    )
    await _denied(gateway, "sendPengolo", {**base, "prompt": "Changed payload"})
    await _denied(gateway, "sendPengolo", base, actor=OUTSIDER_ID)
    generation_id = _text(_object(first.get("generation")), "id")
    # There is deliberately no subscription while the model continues running.
    page = await eventually(
        lambda: _completed(gateway, generation_id),
        timeout_seconds=90,
        description="detached Pengolo generation",
        abort_on=(GenerationFailure,),
    )
    events = require_sequence(page.get("events"), "output")
    assert [
        require_mapping(event, "event").get("sequence") for event in events
    ] == list(range(1, len(events) + 1))
    assert any(
        require_mapping(event, "event").get("kind") == "tool"
        and require_mapping(event, "event").get("content") == "context"
        for event in events
    )
    answer = json.loads(
        "".join(
            str(require_mapping(event, "event").get("content"))
            for event in events
            if require_mapping(event, "event").get("kind") == "content"
        )
    )
    metadata = require_mapping(
        require_mapping(answer, "answer").get("_fixture"), "provider settings"
    )
    assert metadata.get("reasoning_effort") == "medium"
    assert metadata.get("graph_context_received") is True
    chat_id = _text(_object(first.get("chat")), "id")
    chat = _object(
        (
            await gateway.execute(
                token,
                "query Chat($id:String!) { pengoloChat(id:$id) { id revision data { activeGeneration } } }",
                {"id": chat_id},
            )
        ).get("pengoloChat")
    )
    await _object_mutation(
        gateway,
        "updatePengoloChat",
        {
            "id": chat_id,
            "revision": chat.get("revision"),
            "projectId": project_id,
            "moveProject": True,
        },
    )
    assert (await _events(gateway, generation_id)).get("status") == "completed"
    chat = _object(
        (
            await gateway.execute(
                token,
                "query Chat($id:String!) { pengoloChat(id:$id) { id revision data { activeGeneration } } }",
                {"id": chat_id},
            )
        ).get("pengoloChat")
    )
    assert (
        await _mutation(
            gateway,
            "deletePengoloChat",
            {
                "id": chat_id,
                "revision": chat.get("revision"),
            },
        )
        is True
    )
    assert _text(
        await _object_mutation(
            gateway, "pengoloDownload", {"id": _text(personal, "id")}
        ),
        "url",
    )
    await _denied(
        gateway,
        "sendPengolo",
        {
            **base,
            "idempotencyKey": str(uuid.uuid4()),
            "temporary": True,
            "fileIds": [_text(duplicate, "id")],
        },
    )
    with_library = await _object_mutation(
        gateway,
        "sendPengolo",
        {
            **base,
            "idempotencyKey": str(uuid.uuid4()),
            "temporary": True,
            "withLibrary": True,
            "fileIds": [_text(duplicate, "id")],
        },
    )
    await eventually(
        lambda: _completed(
            gateway,
            _text(_object(with_library.get("generation")), "id"),
            temporary=True,
        ),
        timeout_seconds=90,
        description="temporary generation with personal library",
        abort_on=(GenerationFailure,),
    )
    transient = await _object_mutation(
        gateway,
        "sendPengolo",
        {**base, "idempotencyKey": str(uuid.uuid4()), "temporary": True},
    )
    temporary_chat = _text(_object(transient.get("chat")), "id")
    temporary_id = _text(_object(transient.get("generation")), "id")
    await eventually(
        lambda: _completed(gateway, temporary_id, temporary=True),
        timeout_seconds=90,
        description="temporary Pengolo generation",
        abort_on=(GenerationFailure,),
    )
    async with await psycopg.AsyncConnection.connect(
        POSTGRES_DSN
    ) as connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "SELECT count(*) FROM pengolo_records WHERE tenant_id=%s AND id IN (%s,%s)",
                (TENANT_ID, temporary_chat, temporary_id),
            )
            assert await cursor.fetchone() == (0,)
            await cursor.execute(
                "UPDATE pengolo_records SET expires_at=0 WHERE tenant_id=%s AND kind='file' AND id=%s",
                (TENANT_ID, _text(personal, "id")),
            )
    await _denied(gateway, "pengoloDownload", {"id": _text(personal, "id")})
    assert (
        await _mutation(
            gateway, "deletePengoloFile", {"id": _text(project_file, "id")}
        )
        is True
    )
    await _denied(gateway, "pengoloDownload", {"id": _text(project_file, "id")})
    slow = await _object_mutation(
        gateway,
        "sendPengolo",
        {
            **base,
            "idempotencyKey": str(uuid.uuid4()),
            "prompt": json.dumps(
                {
                    "entity_id": state.entity_id,
                    "causal_entity_id": causal,
                    "question": "Slow work",
                    "slow": True,
                }
            ),
        },
    )
    slow_id = _text(_object(slow.get("generation")), "id")
    await _mutation(gateway, "cancelPengolo", {"id": slow_id})
    assert (await _events(gateway, slow_id)).get("status") == "cancelled"
    await exercise_pengolo_browser(
        json.dumps(
            {
                "entity_id": state.entity_id,
                "causal_entity_id": causal,
                "question": "Live browser evidence",
                "pengolo": True,
                "slow": True,
            }
        ),
        str(state.state_value.get("label", state.entity_id)),
        administrator=administrator,
    )
    print(
        "Pengolo: detached replay, medium reasoning, graph preflight, immutable uploads, library isolation, expiration, temporary storage and cancellation passed.",
        flush=True,
    )
