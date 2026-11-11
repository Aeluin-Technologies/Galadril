"""Opt-in Documenso V2 transport for a trusted signing coordinator."""

import asyncio
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from galadril_plugins.signatures import (
    MAX_PDF_BYTES,
    EnvelopeDraft,
    ProviderArtifact,
    ProviderError,
    ProviderStatus,
    SignatureKind,
)

_MAX_RESPONSE = 256 * 1024
_ENVELOPE_ID = r"^envelope_[A-Za-z0-9]{1,64}$"
_ITEM_ID = r"^envelope_item_[A-Za-z0-9]{1,64}$"
_TIER = {
    SignatureKind.CRYPTOGRAPHIC: "AES",
    SignatureKind.QUALIFIED_REQUESTED: "QES",
}
_TRACER = trace.get_tracer(__name__)
type JsonObject = dict[str, object]


class _EnvelopeRef(BaseModel):
    id: Annotated[str, Field(pattern=_ENVELOPE_ID)]


class _Meta(BaseModel):
    signingOrder: Literal["SEQUENTIAL"]
    allowDictateNextSigner: Literal[False] = False


class _Item(BaseModel):
    id: Annotated[str, Field(pattern=_ITEM_ID)]


class _Recipient(BaseModel):
    email: str
    role: Literal["SIGNER"]
    signingOrder: int
    signingStatus: str | None = None


class _Snapshot(_EnvelopeRef):
    externalId: str
    status: str
    signatureLevel: Literal["AES", "QES"]
    documentMeta: _Meta
    recipients: tuple[_Recipient, ...] = Field(min_length=1, max_length=16)
    envelopeItems: tuple[_Item, ...] = ()


class _Distributed(_EnvelopeRef):
    success: Literal[True]


class _Cancelled(BaseModel):
    success: Literal[True]


type TerminalEvent = Literal[
    "DOCUMENT_COMPLETED", "DOCUMENT_CANCELLED", "DOCUMENT_REJECTED"
]


class _HintPayload(BaseModel):
    envelopeId: Annotated[str, Field(pattern=_ENVELOPE_ID)]


class _Notification(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    event: TerminalEvent
    payload: _HintPayload


@dataclass(frozen=True, slots=True)
class WebhookHint:
    """The same terminal notification may be delivered multiple times."""

    envelope_id: str
    event: TerminalEvent


def _parse[Model: BaseModel](
    model: type[Model], body: bytes | bytearray, *, mutation: bool = False
) -> Model:
    try:
        return model.model_validate_json(body)
    except ValidationError:
        raise ProviderError(
            "Unexpected Documenso response", reconcile_before_retry=mutation
        ) from None


class Documenso:
    """A client never follows redirects or retries ambiguous provider mutations."""

    __slots__ = ("_client", "_base", "_token", "_webhook_secret")

    def __init__(
        self,
        client: httpx.AsyncClient,
        api_base: str,
        token: str,
        webhook_secret: str,
    ) -> None:
        base = httpx.URL(api_base)
        if (
            base.scheme != "https"
            or not base.host
            or base.userinfo
            or base.query
            or base.fragment
            or base.path.rstrip("/") != "/api/v2"
            or not re.fullmatch(r"api_[A-Za-z0-9_-]+", token)
            or not 32 <= len(webhook_secret.encode()) <= 256
        ):
            raise ValueError("Invalid Documenso provider configuration")
        self._client = client
        self._base = str(base).rstrip("/")
        self._token = token
        self._webhook_secret = webhook_secret.encode()

    async def _request(
        self,
        method: Literal["GET", "POST"],
        endpoint: str,
        *,
        operation: str,
        limit: int = _MAX_RESPONSE,
        payload: JsonObject | None = None,
        original: bytes | None = None,
    ) -> bytearray:
        mutation = method == "POST"
        files = (
            {"files": ("document.pdf", original, "application/pdf")}
            if original is not None
            else None
        )
        with _TRACER.start_as_current_span(
            "signature_provider.documenso",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("signature.operation", operation)
            try:
                async with (
                    asyncio.timeout(30),
                    self._client.stream(
                        method,
                        self._base + endpoint,
                        headers={
                            "Authorization": self._token,
                            "Accept-Encoding": "identity",
                        },
                        json=payload if files is None else None,
                        data={"payload": json.dumps(payload)}
                        if files
                        else None,
                        files=files,
                        follow_redirects=False,
                        timeout=10,
                    ) as response,
                ):
                    span.set_attribute(
                        "http.response.status_code", response.status_code
                    )
                    if not response.is_success:
                        raise ProviderError(
                            "Documenso request rejected",
                            reconcile_before_retry=mutation,
                        )
                    if (
                        response.headers.get(
                            "content-encoding", "identity"
                        ).lower()
                        != "identity"
                    ):
                        raise ProviderError(
                            "Unexpected Documenso encoding",
                            reconcile_before_retry=mutation,
                        )
                    body = bytearray()
                    async for chunk in response.aiter_bytes(
                        chunk_size=64 * 1024
                    ):
                        if len(body) + len(chunk) > limit:
                            raise ProviderError(
                                "Documenso response exceeds limit",
                                reconcile_before_retry=mutation,
                            )
                        body.extend(chunk)
                    return body
            except (httpx.HTTPError, TimeoutError):
                raise ProviderError(
                    "Documenso unavailable", reconcile_before_retry=mutation
                ) from None

    async def create_draft(self, draft: EnvelopeDraft, original: bytes) -> str:
        if (
            draft.kind not in _TIER
            or len(original) > MAX_PDF_BYTES
            or not original.startswith(b"%PDF-")
            or hashlib.sha256(original).hexdigest() != draft.original_sha256
        ):
            raise ProviderError("Invalid signing source")
        payload: JsonObject = {
            "type": "DOCUMENT",
            "title": draft.title,
            "externalId": str(draft.request_id),
            "visibility": "ADMIN",
            "meta": {
                "signingOrder": "SEQUENTIAL",
                "allowDictateNextSigner": False,
            },
            "recipients": [
                {
                    "email": signer.email,
                    "name": signer.name,
                    "role": "SIGNER",
                    "signingOrder": position,
                    "fields": [
                        {
                            "type": "SIGNATURE",
                            "identifier": 0,
                            "page": signer.placement.page,
                            "positionX": signer.placement.x,
                            "positionY": signer.placement.y,
                            "width": signer.placement.width,
                            "height": signer.placement.height,
                            "fieldMeta": {
                                "type": "signature",
                                "required": True,
                            },
                        }
                    ],
                }
                for position, signer in enumerate(draft.signers, 1)
            ],
        }
        result = await self._request(
            "POST",
            "/envelope/create",
            operation="create_draft",
            payload=payload,
            original=original,
        )
        return _parse(_EnvelopeRef, result, mutation=True).id

    async def _snapshot(self, ref: str, draft: EnvelopeDraft) -> _Snapshot:
        if not re.fullmatch(_ENVELOPE_ID, ref):
            raise ProviderError("Invalid Documenso envelope reference")
        result = await self._request(
            "GET", f"/envelope/{ref}", operation="read_envelope"
        )
        snapshot = _parse(_Snapshot, result)
        if (
            snapshot.id != ref
            or snapshot.externalId != str(draft.request_id)
            or snapshot.signatureLevel != _TIER.get(draft.kind)
        ):
            raise ProviderError(
                "Documenso envelope does not match the signing request"
            )
        actual = sorted(
            (recipient.signingOrder, recipient.email.casefold())
            for recipient in snapshot.recipients
        )
        expected = [
            (order, signer.email.casefold())
            for order, signer in enumerate(draft.signers, 1)
        ]
        if actual != expected:
            raise ProviderError(
                "Documenso signer roster does not match the request"
            )
        return snapshot

    async def status(self, ref: str, draft: EnvelopeDraft) -> ProviderStatus:
        snapshot = await self._snapshot(ref, draft)
        try:
            return ProviderStatus(snapshot.status.lower())
        except ValueError:
            raise ProviderError(
                "Unrecognized Documenso envelope status"
            ) from None

    async def distribute(self, ref: str, draft: EnvelopeDraft) -> None:
        snapshot = await self._snapshot(ref, draft)
        if snapshot.status != "DRAFT":
            raise ProviderError("Documenso envelope is not a draft")
        result = await self._request(
            "POST",
            "/envelope/distribute",
            operation="distribute",
            payload={"envelopeId": ref},
        )
        if _parse(_Distributed, result, mutation=True).id != ref:
            raise ProviderError(
                "Unexpected Documenso envelope", reconcile_before_retry=True
            )

    async def cancel(self, ref: str, draft: EnvelopeDraft) -> None:
        snapshot = await self._snapshot(ref, draft)
        if snapshot.status == "CANCELLED":
            return
        if snapshot.status != "PENDING":
            raise ProviderError("Documenso envelope cannot be cancelled")
        result = await self._request(
            "POST",
            "/envelope/cancel",
            operation="cancel",
            payload={"envelopeId": ref},
        )
        _parse(_Cancelled, result, mutation=True)

    async def download_completed(
        self, ref: str, draft: EnvelopeDraft
    ) -> ProviderArtifact:
        snapshot = await self._snapshot(ref, draft)
        if (
            snapshot.status != "COMPLETED"
            or len(snapshot.envelopeItems) != 1
            or any(r.signingStatus != "SIGNED" for r in snapshot.recipients)
        ):
            raise ProviderError("Documenso artifact is unavailable")
        (item,) = snapshot.envelopeItems
        result = await self._request(
            "GET",
            f"/envelope/item/{item.id}/download?version=signed",
            operation="download_completed",
            limit=MAX_PDF_BYTES,
        )
        if not result.startswith(b"%PDF-"):
            raise ProviderError("Invalid Documenso PDF")
        return ProviderArtifact(
            bytes(result), hashlib.sha256(result).hexdigest()
        )

    def webhook_hint(
        self, supplied_secret: bytes | None, body: bytes
    ) -> WebhookHint:
        # Documenso sends a bearer secret, not a MAC over the body. Only a
        # subsequent authenticated read can drive a durable state transition.
        if supplied_secret is None or not hmac.compare_digest(
            supplied_secret, self._webhook_secret
        ):
            raise ProviderError("Documenso webhook authentication failed")
        if len(body) > _MAX_RESPONSE:
            raise ProviderError("Documenso webhook exceeds limit")
        event = _parse(_Notification, body)
        return WebhookHint(event.payload.envelopeId, event.event)
