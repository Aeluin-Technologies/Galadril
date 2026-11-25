"""Shared GraphQL and S3 actions for Pengolo Library tests."""

import base64
import hashlib
from collections.abc import Mapping

import httpx
from assertions import require_mapping, require_sequence
from clients import (
    UPLOADER_ID,
    GatewayClient,
    authenticated_token,
    gateway_tls_context,
)

_METADATA = "id ownerId projectId expiresAt revision updatedAt"
_CHAT = f"{{ {_METADATA} data {{ title pinned activeGeneration }} }}"
_PROJECT = f"{{ {_METADATA} data {{ name icon color shared roles contextMode instructions }} }}"
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
    "updatePengoloPreferences": "PengoloPreferencesInput",
    "savePengoloDocumentProfile": "PengoloDocumentProfileInput",
}
_SELECTIONS = {
    "sendPengolo": f"{{ chat {_CHAT} generation {{ id data {{ status chatId sequence }} }} userMessage {{ id text fileIds }} }}",
    "updatePengoloChat": _CHAT,
    "savePengoloProject": _PROJECT,
    "beginPengoloUpload": f"{{ url headers {{ name value }} file {_FILE} }}",
    "finishPengoloUpload": _FILE,
    "pengoloDownload": "{ url }",
    "updatePengoloPreferences": "{ revision instructions }",
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
