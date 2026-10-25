"""Explicitly deployed mail service; model calls never acquire secret authority."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from galadril_plugins.ingestion import IngestionClient, IngestionSettings
from galadril_plugins.utils.autoconfig import AutoConfiguration
from galadril_plugins.utils.discovery import (
    Discovery,
    DNSResolver,
    normalize_domain,
)
from galadril_plugins.utils.mime import compose_reply
from galadril_plugins.utils.models import (
    DEFAULT_SIGNATURE,
    ConnectionInput,
    Contract,
    Credentials,
    DraftInput,
    MailMessage,
    PasswordReplacement,
    Principal,
    ResponseProfile,
    Right,
    validate_address,
)
from galadril_plugins.utils.oauth import OAuthBroker, OAuthClient
from galadril_plugins.utils.store import MailError, MailStore
from galadril_plugins.utils.tracking import OpenTracking
from galadril_plugins.utils.transport import MailTransport
from opentelemetry import metrics, trace
from pydantic import (
    Field,
    JsonValue,
    ValidationError,
    field_validator,
    model_validator,
)

logger = structlog.get_logger(__name__)
_OPERATIONS = metrics.get_meter("galadril.plugins.utils").create_counter(
    "utils.email.operations"
)


class ServerSettings(Contract):
    imap_host: str = Field(max_length=253)
    smtp_host: str = Field(max_length=253)
    smtp_port: Literal[465, 587] = 465

    @field_validator("imap_host", "smtp_host")
    @classmethod
    def hostname(cls, value: str) -> str:
        if "@" in value:
            raise ValueError("Mail servers require a hostname")
        return normalize_domain(value)


class Operation(Contract):
    operation: Literal[
        "accounts",
        "create",
        "update",
        "delete",
        "reveal",
        "grant",
        "grants",
        "messages",
        "message",
        "thread",
        "drafts",
        "drafts_page",
        "draft",
        "save_draft",
        "approve",
        "send",
        "generate",
        "profile",
        "save_profile",
        "sync",
        "test",
        "label",
        "audit",
        "oauth_start",
        "oauth_finish",
        "providers",
        "discover",
        "validate_servers",
        "tracking_status",
        "proposal_status",
        "opens",
    ]
    account: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    revision: int | None = Field(default=None, ge=1)
    config: ConnectionInput | None = None
    credentials: Credentials | PasswordReplacement | None = None
    user: str | None = Field(default=None, max_length=256)
    rights: tuple[Right, ...] = ()
    message: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    draft: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    content: DraftInput | None = None
    profile: ResponseProfile | None = None
    label: str = Field(default="", max_length=128)
    query: str = Field(default="", max_length=512)
    before: int = Field(default=0, ge=0)
    provider: Literal["google", "microsoft"] | None = None
    address: str = Field(default="", max_length=320)
    state: str = Field(default="", max_length=128)
    code: str = Field(default="", max_length=8192, repr=False)
    servers: ServerSettings | None = None
    domain: str | None = Field(default=None, max_length=1024)

    @field_validator("domain")
    @classmethod
    def mail_domain(cls, value: str | None) -> str | None:
        return normalize_domain(value) if value is not None else None

    @model_validator(mode="after")
    def exact_fields(self) -> Operation:
        allowed = {
            "accounts": set(),
            "providers": set(),
            "tracking_status": set(),
            "proposal_status": {"draft"},
            "opens": {"account", "draft", "before"},
            "discover": {"domain"},
            "validate_servers": {"servers"},
            "create": {"config", "credentials"},
            "update": {"account", "revision", "config", "credentials"},
            "delete": {"account", "revision"},
            "grant": {"account", "user", "rights"},
            "messages": {"account", "query", "before"},
            "message": {"account", "message"},
            "thread": {"account", "message"},
            "drafts_page": {"account", "before"},
            "draft": {"account", "draft"},
            "save_draft": {"account", "draft", "revision", "content"},
            "approve": {"account", "draft", "revision"},
            "send": {"account", "draft", "revision"},
            "generate": {"account", "message"},
            "save_profile": {"account", "profile"},
            "label": {"account", "message", "label"},
            "oauth_start": {"provider", "address"},
            "oauth_finish": {"state", "code"},
        }.get(self.operation, {"account"})
        if self.model_fields_set - {"operation"} - allowed:
            raise ValueError("Unexpected operation fields")
        if (
            self.operation
            not in {
                "accounts",
                "providers",
                "create",
                "oauth_start",
                "oauth_finish",
                "discover",
                "validate_servers",
                "tracking_status",
                "proposal_status",
            }
            and self.account is None
        ):
            raise ValueError("Connection required")
        if self.operation == "discover" and self.domain is None:
            raise ValueError("Mail domain required")
        if self.operation == "thread" and self.message is None:
            raise ValueError("Message required")
        if self.operation == "opens" and self.draft is None:
            raise ValueError("Draft required")
        if self.operation == "proposal_status" and self.draft is None:
            raise ValueError("Draft required")
        if self.operation == "validate_servers" and self.servers is None:
            raise ValueError("Mail servers required")
        return self


class OpeningSignal(Contract):
    token: str = Field(pattern=r"^[a-f0-9]{64}$")
    ip: str = Field(min_length=1, max_length=45)


class MailService:
    __slots__ = (
        "store",
        "transport",
        "key",
        "scribe",
        "ingestion",
        "oauth",
        "gateway",
        "_locks",
        "_http",
        "_last",
        "_semaphore",
        "discovery",
        "tracking",
        "tracking_key",
    )

    def __init__(
        self,
        store: MailStore,
        transport: MailTransport,
        key: str,
        scribe: str,
        ingestion: Mapping[str, tuple[IngestionSettings, Path]],
        oauth_clients: Mapping[str, OAuthClient] | None = None,
        tracking_origin: str = "",
        tracking_key: str = "",
    ) -> None:
        if len(key) < 32:
            raise MailError("Email service requires a private service key")
        self.store, self.transport, self.key = store, transport, key
        self.scribe, self.ingestion = scribe.rstrip("/"), ingestion
        self.gateway: str | None = None
        self.oauth = OAuthBroker(store, oauth_clients or {})
        if tracking_origin and len(tracking_key) < 32:
            raise MailError("Opening capture requires a separate private key")
        self.tracking_key = tracking_key
        self.tracking = OpenTracking(store, tracking_origin)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._http: httpx.AsyncClient | None = None
        self._last: dict[tuple[str, str], float] = {}
        self._semaphore = asyncio.Semaphore(8)
        self.discovery = Discovery(
            DNSResolver(),
            transport.allowed_networks,
            directory=AutoConfiguration(),
        )

    def lock(self, actor: Principal, identifier: str) -> asyncio.Lock:
        # Keep identity while waiters exist, including the unlocked handoff interval.
        return self._locks.setdefault(
            (actor.tenant, identifier), asyncio.Lock()
        )

    async def authorize(self, actor: Principal) -> None:
        if self.gateway is None:
            raise MailError("Live identity verification is unavailable")
        async with self.http() as http:
            response = await http.post(
                self.gateway.rstrip("/") + "/internal/utils/authorize",
                headers={"x-utils-key": self.key},
                json=actor.model_dump(),
                timeout=15,
            )
        if (
            response.status_code != 200
            or len(response.content) > 1024
            or response.json() != {"active": True}
        ):
            raise MailError("Mail identity is no longer active")

    @asynccontextmanager
    async def http(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(
                timeout=130, trust_env=False, follow_redirects=False
            ) as http:
                yield http

    async def credentials(
        self, actor: Principal, identifier: str, right: Right
    ) -> tuple[ConnectionInput, Credentials]:
        config, old = await asyncio.to_thread(
            self.store.connection, actor, identifier, right
        )
        async with self.http() as http:
            value = await self.oauth.refresh(http, old)
        if value is not old:
            await asyncio.to_thread(self.store.rotate, actor, identifier, value)
        return config, value

    async def generate(
        self,
        actor: Principal,
        identifier: str,
        message_id: str,
        *,
        automatic: bool = False,
    ) -> dict[str, JsonValue]:
        message = await asyncio.to_thread(
            self.store.message, actor, identifier, message_id
        )
        profile = await asyncio.to_thread(self.store.profile, actor, identifier)
        if profile is None:
            raise MailError("Configure a Scribe response profile first")
        context = json.dumps(
            {
                "sender": message.sender,
                "subject": message.subject,
                "date": message.date,
                "body": message.body[:24000],
            },
            ensure_ascii=False,
        )
        request = {
            "model": profile.model,
            "fallbacks": profile.fallbacks,
            "skill_ids": profile.skill_ids,
            "prompt": profile.prompt
            + f"\nLanguage: {profile.language}. Tone: {profile.tone}.",
            "message": context[:32768],
        }
        async with self.http() as http:
            response = await http.post(
                self.scribe + "/utils/draft",
                headers={"x-utils-key": self.key},
                json=request,
            )
        if response.status_code != 200 or len(response.content) > 65536:
            raise MailError("Scribe email generation unavailable")
        result = response.json()
        body = result.get("body")
        if not isinstance(body, str) or not body or len(body) > 16384:
            raise MailError("Invalid Scribe draft")
        draft = DraftInput(
            track_opens=profile.track_opens,
            to=(message.sender,),
            subject=(
                message.subject
                if message.subject.lower().startswith("re:")
                else "Re: " + message.subject
            )[:998]
            or "Re: Message",
            body=body,
            reply_to_message_id=message.message_id,
            references=message.references,
        )
        source_id = (
            hashlib.sha256(message.message_id.encode()).hexdigest()
            if automatic
            else None
        )
        saved = await asyncio.to_thread(
            self.store.save_draft,
            actor,
            identifier,
            None,
            None,
            draft,
            source_id=source_id,
            automatic=automatic,
            source_reference=message.reference,
        )
        category = result.get("category")
        if isinstance(category, str):
            await asyncio.to_thread(
                self.store.label,
                actor,
                identifier,
                message_id,
                category[:128],
                "use",
            )
        saved["analysis"] = {
            "summary": str(result.get("summary", ""))[:4096],
            "model": str(result.get("model", ""))[:128],
        }
        await asyncio.to_thread(
            self.store.record_analysis,
            actor,
            identifier,
            message_id,
            str(result.get("model", "")),
            str(result.get("summary", "")),
        )
        return saved

    async def send(
        self, actor: Principal, identifier: str, draft_id: str, revision: int
    ) -> None:
        await self.authorize(actor)
        config, credentials = await self.credentials(actor, identifier, "send")
        draft = await asyncio.to_thread(
            self.store.draft, actor, identifier, draft_id, "send"
        )
        if draft is None or not isinstance(draft["delivery_id"], str):
            raise MailError("Draft unavailable")
        profile = await asyncio.to_thread(self.store.profile, actor, identifier)
        if (
            DraftInput.model_validate(draft["content"]).track_opens
            and not self.tracking.origin
        ):
            raise MailError("Opening tracking has not been configured")
        value = await asyncio.to_thread(
            self.store.claim_send, actor, identifier, draft_id, revision
        )
        try:
            tracking_url = await asyncio.to_thread(
                self.tracking.prepare, actor, identifier, draft_id
            )
            raw = compose_reply(
                config.address,
                value,
                draft["delivery_id"],
                sender_name=config.sender_name,
                tracking_url=tracking_url,
                automatic=draft["automatic"] is True,
                signature_html=profile.signature
                if profile
                else DEFAULT_SIGNATURE,
            )
            await asyncio.to_thread(
                self.transport.send,
                config,
                credentials,
                (*value.to, *value.cc, *value.bcc),
                raw,
            )
        except BaseException:
            await asyncio.shield(
                asyncio.to_thread(
                    self.store.finish_send, actor, identifier, draft_id, False
                )
            )
            raise MailError(
                "Delivery uncertain; reconcile before sending again"
            ) from None
        await asyncio.to_thread(
            self.store.finish_send, actor, identifier, draft_id, True
        )

    async def sync(self, actor: Principal, identifier: str) -> int:
        config, credentials = await self.credentials(actor, identifier, "use")
        if not config.enabled:
            raise MailError("Connection is disabled")

        def consume(
            folder: str, validity: int, uid: int, message: MailMessage
        ) -> None:
            self.store.record_message(
                actor, identifier, folder, validity, uid, message
            )

        def collect() -> int:
            return self.transport.sync(
                config,
                credentials,
                lambda folder: self.store.cursor(actor, identifier, folder),
                consume,
                lambda folder, validity, uid: self.store.advance(
                    actor, identifier, folder, validity, uid
                ),
            )

        count = await asyncio.to_thread(collect)
        await self.ingest(actor, identifier, config)
        profile = await asyncio.to_thread(self.store.profile, actor, identifier)
        if profile and profile.mode != "draft":
            for message_id, message in await asyncio.to_thread(
                self.store.pending, actor, identifier
            ):
                source_id = hashlib.sha256(
                    message.message_id.encode()
                ).hexdigest()
                if not await asyncio.to_thread(
                    self.store.has_response, actor, identifier, source_id
                ):
                    draft = await self.generate(
                        actor, identifier, message_id, automatic=True
                    )
                    if profile.mode == "automatic" and isinstance(
                        draft["id"], str
                    ):
                        try:
                            await asyncio.to_thread(
                                self.store.approve_automatic,
                                actor,
                                identifier,
                                draft["id"],
                                message,
                            )
                        except MailError:
                            logger.info("utils.email.human_approval_required")
                        else:
                            await self.send(actor, identifier, draft["id"], 1)
                await asyncio.to_thread(
                    self.store.processed, actor, identifier, message_id
                )
        await asyncio.to_thread(self.store.sync_status, actor, identifier, "ok")
        return count

    async def ingest(
        self, actor: Principal, identifier: str, config: ConnectionInput
    ) -> None:
        if not config.ingest:
            return
        settings = self.ingestion.get(actor.tenant)
        if settings is None:
            raise MailError("Tenant ingestion has not been configured")
        deployment, token_file = settings

        async def token() -> str:
            return (await asyncio.to_thread(token_file.read_text)).strip()

        async with self.http() as http:
            client = IngestionClient(deployment, http, token)
            for item in await asyncio.to_thread(
                self.store.messages, actor, identifier, pending=True
            ):
                key = item["id"]
                if not isinstance(key, str):
                    raise MailError("Invalid mail identity")
                message = await asyncio.to_thread(
                    self.store.message, actor, identifier, key, "use"
                )
                # Stable names make re-delivery overwrite the same authorized source.
                envelope = {
                    "content": message.body,
                    "metadata": {
                        "source_kind": "email",
                        "reference": message.thread_reference
                        or message.reference,
                        "message_reference": message.reference,
                        "thread_number": message.thread_number,
                        "account_id": identifier,
                        "email_id": key,
                        "message_id": message.message_id,
                        "thread_id": message.thread_id,
                        "sender": message.sender,
                        "recipients": message.recipients,
                        "subject": message.subject,
                        "date": message.date,
                        "references": message.references,
                        "attachments": [
                            {
                                "name": attachment.name,
                                "content_type": attachment.content_type,
                                "source_name": f"email-{identifier}-{key}-attachment-{index}",
                            }
                            for index, attachment in enumerate(
                                message.attachments
                            )
                        ],
                    },
                }
                for index, attachment in enumerate(message.attachments):
                    await client.upload(
                        base64.b64decode(attachment.content, validate=True),
                        name=f"email-{identifier}-{key}-attachment-{index}",
                        content_type=attachment.content_type,
                    )
                receipt = await client.upload(
                    json.dumps(envelope, ensure_ascii=False).encode(),
                    name=f"email-{identifier}-{key}.json",
                    content_type="application/json",
                )
                await asyncio.to_thread(
                    self.store.mark_ingested,
                    actor,
                    identifier,
                    key,
                    source_key=receipt.key,
                )

    async def operate(
        self, actor: Principal, value: Operation, *, delegated: bool = False
    ) -> JsonValue:
        if delegated and value.operation not in {
            "accounts",
            "messages",
            "message",
            "drafts",
            "save_draft",
            "label",
        }:
            raise MailError("FORBIDDEN")
        if delegated and value.content and value.content.track_opens:
            raise MailError("FORBIDDEN")
        if value.operation == "proposal_status" and value.draft:
            return await asyncio.to_thread(
                self.store.proposal_status, actor, value.draft
            )
        if value.operation == "tracking_status":
            return {
                "available": bool(self.tracking.origin),
                "retention_days": 90,
                "event_limit": 1000,
            }
        if value.operation == "accounts":
            accounts = await asyncio.to_thread(self.store.accounts, actor)
            if delegated:
                return [
                    {"id": item["id"], "rights": item["rights"]}
                    for item in accounts
                ]
            return [item for item in accounts]
        if value.operation == "providers":
            return list(self.oauth.clients)
        if value.operation == "discover" and value.domain:
            async with self._semaphore:
                result = await self.discovery.discover(value.domain)
                return result.model_dump(mode="json")
        if value.operation == "validate_servers" and value.servers:
            async with self._semaphore:
                await asyncio.to_thread(
                    self.transport.validate_servers,
                    value.servers.imap_host,
                    value.servers.smtp_host,
                    value.servers.smtp_port,
                )
            return {"ok": True}
        if value.operation == "oauth_start" and value.provider:
            validate_address(value.address)
            return {
                "url": await asyncio.to_thread(
                    self.oauth.start, actor, value.provider, value.address
                )
            }
        if value.operation == "oauth_finish" and value.state and value.code:
            async with self.http() as http:
                provider, credentials = await self.oauth.finish(
                    http, actor, value.state, value.code
                )
            config = ConnectionInput(
                name=credentials.username,
                address=credentials.username,
                imap_host="imap.gmail.com"
                if provider == "google"
                else "outlook.office365.com",
                smtp_host="smtp.gmail.com"
                if provider == "google"
                else "smtp.office365.com",
                smtp_port=465 if provider == "google" else 587,
            )
            await asyncio.to_thread(self.transport.test, config, credentials)
            return await asyncio.to_thread(
                self.store.create, actor, config, credentials
            )
        if (
            value.operation == "create"
            and value.config
            and isinstance(value.credentials, Credentials)
        ):
            if value.credentials.auth != "password":
                raise MailError("Use provider consent for OAuth credentials")
            return await asyncio.to_thread(
                self.store.create, actor, value.config, value.credentials
            )
        identifier = value.account
        if identifier is None:
            raise MailError("Invalid email operation")
        # Authorize before creating per-account locks; all writes and remote effects serialize.
        required: dict[str, Right] = {
            "delete": "delete",
            "reveal": "reveal",
            "grant": "share",
            "grants": "share",
            "update": "edit",
            "save_profile": "edit",
            "messages": "read",
            "message": "read",
            "thread": "read",
            "drafts_page": "read",
            "drafts": "read",
            "draft": "read",
            "audit": "share",
            "opens": "read",
            "approve": "approve",
            "send": "send",
            "label": "edit",
        }
        await asyncio.to_thread(
            self.store.connection,
            actor,
            identifier,
            required.get(value.operation, "use"),
        )
        async with self._semaphore, self.lock(actor, identifier):
            match value.operation:
                case "opens" if value.draft:
                    return await asyncio.to_thread(
                        self.tracking.history,
                        actor,
                        identifier,
                        value.draft,
                        before=value.before,
                    )
                case "update" if value.config and value.revision:
                    return await asyncio.to_thread(
                        self.store.update,
                        actor,
                        identifier,
                        value.revision,
                        value.config,
                        value.credentials,
                    )
                case "delete" if value.revision:
                    await asyncio.to_thread(
                        self.store.delete, actor, identifier, value.revision
                    )
                    self._last.pop((actor.tenant, identifier), None)
                    self._locks.pop((actor.tenant, identifier), None)
                case "reveal":
                    credentials = await asyncio.to_thread(
                        self.store.reveal, actor, identifier
                    )
                    return {
                        "username": credentials.username,
                        "auth": credentials.auth,
                    }
                case "grant" if value.user:
                    await asyncio.to_thread(
                        self.store.grant,
                        actor,
                        identifier,
                        value.user,
                        value.rights,
                    )
                case "grants":
                    return [
                        item
                        for item in await asyncio.to_thread(
                            self.store.grants, actor, identifier
                        )
                    ]
                case "messages":
                    return await asyncio.to_thread(
                        self.store.message_page,
                        actor,
                        identifier,
                        query=value.query,
                        before=value.before,
                    )
                case "message" if value.message:
                    return (
                        await asyncio.to_thread(
                            self.store.message, actor, identifier, value.message
                        )
                    ).model_dump(mode="json")
                case "drafts":
                    return [
                        item
                        for item in await asyncio.to_thread(
                            self.store.drafts, actor, identifier
                        )
                    ]
                case "thread" if value.message:
                    return await asyncio.to_thread(
                        self.store.thread, actor, identifier, value.message
                    )
                case "drafts_page":
                    return await asyncio.to_thread(
                        self.store.draft_page,
                        actor,
                        identifier,
                        before=value.before,
                    )
                case "draft" if value.draft:
                    return await asyncio.to_thread(
                        self.store.draft, actor, identifier, value.draft
                    )
                case "save_draft" if value.content:
                    return await asyncio.to_thread(
                        self.store.save_draft,
                        actor,
                        identifier,
                        value.draft,
                        value.revision,
                        value.content,
                    )
                case "approve" if value.draft and value.revision:
                    await asyncio.to_thread(
                        self.store.approve,
                        actor,
                        identifier,
                        value.draft,
                        value.revision,
                    )
                case "send" if value.draft and value.revision:
                    await self.send(
                        actor, identifier, value.draft, value.revision
                    )
                case "generate" if value.message:
                    return await self.generate(actor, identifier, value.message)
                case "profile":
                    profile = await asyncio.to_thread(
                        self.store.profile, actor, identifier
                    )
                    return profile.model_dump(mode="json") if profile else None
                case "save_profile" if value.profile:
                    await asyncio.to_thread(
                        self.store.save_profile,
                        actor,
                        identifier,
                        value.profile,
                    )
                case "sync":
                    return {"collected": await self.sync(actor, identifier)}
                case "test":
                    config, credentials = await self.credentials(
                        actor, identifier, "use"
                    )
                    await asyncio.to_thread(
                        self.transport.test, config, credentials
                    )
                case "label" if value.message:
                    await asyncio.to_thread(
                        self.store.label,
                        actor,
                        identifier,
                        value.message,
                        value.label,
                    )
                case "audit":
                    return [
                        item
                        for item in await asyncio.to_thread(
                            self.store.audit, actor, identifier
                        )
                    ]
                case _:
                    raise MailError("Invalid email operation")
        return {"ok": True}

    async def poll(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await asyncio.to_thread(self.tracking.prune)
            for actor, identifier, config in await asyncio.to_thread(
                self.store.workers
            ):
                identity = (actor.tenant, identifier)
                if stop.is_set():
                    break
                if (
                    not config.enabled
                    or time.monotonic() - self._last.get(identity, 0)
                    < config.poll_seconds
                ):
                    continue
                self._last[identity] = time.monotonic()
                try:
                    async with self._semaphore, self.lock(actor, identifier):
                        await self.authorize(actor)
                        await self.sync(actor, identifier)
                except Exception:
                    logger.warning("utils.email.sync_deferred")
                    try:
                        await asyncio.to_thread(
                            self.store.sync_status,
                            actor,
                            identifier,
                            "needs_attention",
                        )
                    except MailError:
                        logger.info("utils.email.connection_removed")
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except TimeoutError:
                continue


def create_app(service: MailService) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/tracking/open")
    async def opened(request: Request) -> JSONResponse:
        supplied = request.headers.getlist("x-email-tracking-key")
        if (
            not service.tracking_key
            or len(supplied) != 1
            or not hmac.compare_digest(service.tracking_key, supplied[0])
        ):
            raise HTTPException(403, "Email service access denied")
        body = bytearray()
        async with asyncio.timeout(3):
            async for chunk in request.stream():
                if len(body) + len(chunk) > 1024:
                    raise HTTPException(413, "Request exceeds limit")
                body.extend(chunk)
        try:
            signal = OpeningSignal.model_validate_json(body)
        except ValidationError:
            raise HTTPException(422, "Invalid opening signal") from None
        await asyncio.to_thread(
            service.tracking.record, signal.token, signal.ip
        )
        _OPERATIONS.add(
            1, {"operation": "opening_signal", "outcome": "received"}
        )
        return JSONResponse({"ok": True}, headers={"cache-control": "no-store"})

    @app.post("/operation")
    async def operation(request: Request) -> JSONResponse:
        supplied = request.headers.getlist("x-utils-key")
        if len(supplied) != 1 or not hmac.compare_digest(
            service.key, supplied[0]
        ):
            raise HTTPException(403, "Email service access denied")
        try:
            if (
                len(request.headers.getlist("x-utils-tenant")) != 1
                or len(request.headers.getlist("x-utils-user")) != 1
            ):
                raise MailError("FORBIDDEN")
            actor = Principal(
                tenant=request.headers.get("x-utils-tenant", ""),
                user=request.headers.get("x-utils-user", ""),
            )
            body = bytearray()
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 16 * 1024 * 1024:
                        raise HTTPException(413, "Request exceeds limit")
                    body.extend(chunk)
            value = Operation.model_validate_json(body)
            with trace.get_tracer(
                "galadril.plugins.utils"
            ).start_as_current_span(
                "utils.email.operation",
                record_exception=False,
                set_status_on_exception=False,
            ):
                result = await service.operate(
                    actor,
                    value,
                    delegated=request.headers.get("x-utils-delegated")
                    == "true",
                )
            _OPERATIONS.add(
                1, {"operation": value.operation, "outcome": "success"}
            )
            return JSONResponse(
                result,
                headers={"cache-control": "no-store", "pragma": "no-cache"},
            )
        except ValidationError:
            raise HTTPException(422, "Invalid email request") from None
        except MailError as error:
            status = (
                403
                if str(error) == "FORBIDDEN"
                else 409
                if str(error) == "CONFLICT"
                else 400
            )
            raise HTTPException(
                status,
                str(error)
                if str(error)
                in {
                    "AUTHENTICATION_FAILED",
                    "TLS_FAILED",
                    "CONNECTION_UNAVAILABLE",
                }
                else "Email permission denied"
                if status == 403
                else "Email operation rejected; review configuration or delivery status",
            ) from None
        except Exception:
            logger.warning("utils.email.operation_failed")
            raise HTTPException(503, "Email operation unavailable") from None

    return app


class Worker:
    __slots__ = ("service", "host", "port", "_lock")

    def __init__(self, environment: Mapping[str, str]) -> None:
        path = Path(environment["UTILS_DATABASE_PATH"])
        keyring = json.loads(
            Path(environment["UTILS_KEYRING_FILE"]).read_text()
        )
        store = MailStore(
            path,
            {
                name: base64.b64decode(value, validate=True)
                for name, value in keyring["keys"].items()
            },
            keyring["active"],
        )
        self._lock = path.with_suffix(".lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock.close()
            raise MailError("Another mail service owns this database") from None
        oauth = (
            {
                name: OAuthClient.model_validate(value)
                for name, value in json.loads(
                    Path(environment["UTILS_OAUTH_CLIENTS_FILE"]).read_text()
                ).items()
            }
            if environment.get("UTILS_OAUTH_CLIENTS_FILE")
            else {}
        )
        ingestion: dict[str, tuple[IngestionSettings, Path]] = {}
        if environment.get("UTILS_INGESTION_FILE"):
            for tenant, value in json.loads(
                Path(environment["UTILS_INGESTION_FILE"]).read_text()
            ).items():
                settings = IngestionSettings.model_validate(value["settings"])
                if settings.tenant_id != tenant:
                    raise MailError("Ingestion tenant mismatch")
                ingestion[tenant] = (settings, Path(value["token_file"]))
        self.service = MailService(
            store,
            MailTransport(
                tuple(
                    filter(
                        None,
                        environment.get("UTILS_ALLOWED_NETWORKS", "").split(
                            ","
                        ),
                    )
                )
            ),
            Path(environment["UTILS_SERVICE_KEY_FILE"]).read_text().strip(),
            environment["UTILS_SCRIBE_URL"],
            ingestion,
            oauth,
            tracking_origin=environment.get("UTILS_TRACKING_PUBLIC_URL", ""),
            tracking_key=(
                Path(environment["UTILS_TRACKING_KEY_FILE"]).read_text().strip()
                if environment.get("UTILS_TRACKING_KEY_FILE")
                else ""
            ),
        )
        self.service.gateway = environment["UTILS_GATEWAY_URL"]
        self.host, self.port = (
            environment.get("UTILS_HOST", "127.0.0.1"),
            int(environment.get("UTILS_PORT", "8095")),
        )

    async def run(self, stop: asyncio.Event) -> None:
        config = uvicorn.Config(
            create_app(self.service),
            host=self.host,
            port=self.port,
            access_log=False,
            log_config=None,
            timeout_graceful_shutdown=15,
        )
        server = uvicorn.Server(config)
        try:
            async with httpx.AsyncClient(
                timeout=130, trust_env=False, follow_redirects=False
            ) as http:
                self.service._http = http
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(server.serve())
                    tasks.create_task(self.service.poll(stop))
                    await stop.wait()
                    server.should_exit = True
        finally:
            self.service._http = None
            self._lock.close()
