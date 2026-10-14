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
    authenticated_token,
    gateway_tls_context,
    read_causal_cohort,
)
from pengolo_browser import exercise_pengolo_browser


def _object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
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
) -> dict[str, object]:
    result = await gateway.execute(
        authenticated_token(actor),
        f"mutation Pengolo($input: JSON!) {{ {field}(input: $input) }}",
        {"input": json.dumps(value)},
    )
    return _object(result.get(field))


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
        f"mutation Pengolo($input: JSON!) {{ {field}(input: $input) }}",
        {"input": json.dumps(value)},
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


async def _events(
    gateway: GatewayClient, identifier: str, *, temporary: bool = False
) -> dict[str, object]:
    result = await gateway.execute(
        authenticated_token(UPLOADER_ID),
        "query Events($id:String!, $temporary:Boolean!) { pengoloEvents(generationId:$id, after:0, temporary:$temporary) }",
        {"id": identifier, "temporary": temporary},
    )
    return _object(result.get("pengoloEvents"))


async def _completed(
    gateway: GatewayClient, identifier: str, *, temporary: bool = False
) -> dict[str, object] | None:
    page = await _events(gateway, identifier, temporary=temporary)
    assert page.get("status") not in {"failed", "cancelled"}, page
    return page if page.get("status") == "completed" else None


async def _upload(
    gateway: GatewayClient,
    *,
    project: str | None = None,
    temporary: str | None = None,
) -> dict[str, object]:
    content = b"Confidential operational evidence for the authorized library."
    result = await _mutation(
        gateway,
        "beginPengoloUpload",
        {
            "name": "evidence.txt",
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
        str(key): str(value)
        for key, value in require_mapping(
            result.get("headers"), "upload headers"
        ).items()
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
    ready = await _mutation(
        gateway,
        "finishPengoloUpload",
        {"id": _text(file, "id"), "temporarySession": temporary},
    )
    assert _object(ready.get("data")).get("status") == "ready"
    return ready


async def exercise_pengolo(
    gateway: GatewayClient, state: DerivedPipelineState
) -> None:
    """Uses real auth, roles, S3, PostgreSQL and Scribe without provider credentials."""
    token = authenticated_token(UPLOADER_ID)
    result = await gateway.execute(
        token, "query { pengoloModels { models { alias reasoningEfforts } } }"
    )
    models = require_sequence(
        require_mapping(result.get("pengoloModels"), "catalogue").get("models"),
        "models",
    )
    assert models == [{"alias": "fixture", "reasoningEfforts": ["medium"]}]
    project = await _mutation(
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
        await _mutation(
            gateway, "finishPengoloUpload", {"id": _text(duplicate, "id")}
        )
        == duplicate
    )
    project_file = await _upload(gateway, project=project_id)
    for operation in (
        'mutation { administration { createRole(roleName:"pengolo_reader") } }',
    ):
        await gateway.execute(token, operation)
    shared = await _mutation(
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
        authenticated_token(OUTSIDER_ID), "query { pengoloProjects }"
    )
    assert _text(shared, "id") not in str(before.get("pengoloProjects"))
    await gateway.execute(
        token,
        f'mutation {{ administration {{ assignRoleToUser(userId:"{OUTSIDER_ID}",roleName:"pengolo_reader") }} }}',
    )
    after = await gateway.execute(
        authenticated_token(OUTSIDER_ID), "query { pengoloProjects }"
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
    accessible = await _mutation(
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
    )
    restricted = await _mutation(
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
            "query Events($id:String!) { pengoloEvents(generationId:$id,after:0) }",
            {"id": _text(_object(restricted.get("generation")), "id")},
        )
        page = _object(result.get("pengoloEvents"))
        assert page.get("status") != "completed", (
            "Project membership must not grant private graph access"
        )
        assert not any(
            require_mapping(event, "event").get("kind") == "content"
            for event in require_sequence(page.get("events"), "events")
        )
        return True if page.get("status") == "failed" else None

    await eventually(
        graph_denied,
        timeout_seconds=30,
        description="independent private graph authorization",
    )
    download = await _mutation(
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
            token,
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
        {"name": "../private.pdf", "mime": "application/pdf", "size": 100},
    )
    await _denied(
        gateway,
        "beginPengoloUpload",
        {
            "name": "private.txt",
            "mime": "text/plain",
            "size": 100,
            "key": "other/private.txt",
        },
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
    await _denied(gateway, "sendPengolo", {**base, "reasoningEffort": "high"})
    await _denied(
        gateway,
        "sendPengolo",
        {**base, "projectId": project_id, "fileIds": [_text(personal, "id")]},
    )
    first = await _mutation(gateway, "sendPengolo", base)
    retry = await _mutation(gateway, "sendPengolo", base)
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
                "query Chat($id:String!) { pengoloChat(id:$id) }",
                {"id": chat_id},
            )
        ).get("pengoloChat")
    )
    await _mutation(
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
                "query Chat($id:String!) { pengoloChat(id:$id) }",
                {"id": chat_id},
            )
        ).get("pengoloChat")
    )
    deleted = await gateway.execute(
        token,
        "mutation Delete($input:JSON!) { deletePengoloChat(input:$input) }",
        {
            "input": json.dumps(
                {"id": chat_id, "revision": chat.get("revision")}
            )
        },
    )
    assert deleted.get("deletePengoloChat") in {True, "true"}
    assert _text(
        await _mutation(
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
    with_library = await _mutation(
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
    )
    transient = await _mutation(
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
    await _mutation(
        gateway, "deletePengoloFile", {"id": _text(project_file, "id")}
    )
    await _denied(gateway, "pengoloDownload", {"id": _text(project_file, "id")})
    slow = await _mutation(
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
    await gateway.execute(
        token,
        "mutation Stop($input:JSON!) { cancelPengolo(input:$input) }",
        {"input": json.dumps({"id": slow_id})},
    )
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
    )
    print(
        "Pengolo: detached replay, medium reasoning, graph preflight, immutable uploads, library isolation, expiration, temporary storage and cancellation passed.",
        flush=True,
    )
