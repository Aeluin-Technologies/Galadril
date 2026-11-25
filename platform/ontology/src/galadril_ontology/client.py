"""Gateway validates every opaque admission and effect against live policies."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from os import PathLike
from typing import Literal
from uuid import UUID

import httpx
import orjson
from opentelemetry import propagate
from pydantic import ConfigDict, JsonValue, TypeAdapter, ValidationError

from .contracts import GolEffect, GolLease, GolRequest
from .errors import GolDenied, GolUnavailable

_LOGGER = logging.getLogger(__name__)
type _RpcPhase = Literal["open", "resume", "begin", "finish"]

_EFFECT = TypeAdapter(dict[str, JsonValue])
_PROPOSAL = TypeAdapter(
    dict[str, JsonValue], config=ConfigDict(allow_inf_nan=False)
)


def _log_rpc_failure(phase: _RpcPhase, reason: str) -> None:
    """Reports the failure without attaching the request, response or exception."""
    _LOGGER.warning(
        "GOL worker RPC failed",
        extra={
            "event.name": "pipeline.gol.rpc.failed",
            "phase": phase,
            "reason": reason,
        },
    )


class GatewayGolClient:
    """Live transports are created only inside their owning Ray actor."""

    __slots__ = ("_endpoint", "_client", "_worker_key_path")

    def __init__(
        self,
        endpoint: str,
        *,
        worker_key_path: str | PathLike[str] | None = None,
    ) -> None:
        url = httpx.URL(endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Invalid GOL Gateway endpoint")
        self._endpoint = str(url).rstrip("/")
        self._worker_key_path = (
            os.fspath(worker_key_path) if worker_key_path is not None else None
        )
        self._client: httpx.AsyncClient | None = None

    def _worker_key(self) -> str:
        """Bounds credential I/O and rejects nonregular files without blocking on a FIFO."""
        if self._worker_key_path is None:
            raise GolDenied("GOL workload authentication unavailable")
        try:
            descriptor = os.open(
                self._worker_key_path, os.O_RDONLY | os.O_NONBLOCK
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise GolDenied("GOL workload authentication unavailable")
                key = os.read(descriptor, 33)
            finally:
                os.close(descriptor)
        except OSError:
            raise GolDenied("GOL workload authentication unavailable") from None
        if len(key) != 32:
            raise GolDenied("GOL workload authentication unavailable")
        return key.hex()

    async def _post(
        self, path: str, body: dict[str, object], *, phase: _RpcPhase
    ) -> bytes:
        credential = await asyncio.to_thread(self._worker_key)
        if self._client is None:
            # Let the private Gateway proxy report its 30-second deadline.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, read=35.0),
                follow_redirects=False,
                trust_env=False,
            )
        headers = {
            "content-type": "application/json",
            "x-galadril-gol-worker-key": credential,
        }
        propagate.inject(headers)
        try:
            async with self._client.stream(
                "POST",
                self._endpoint + path,
                content=orjson.dumps(body),
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    _log_rpc_failure(phase, f"http_{response.status_code}")
                if response.status_code >= 500 or response.status_code == 429:
                    raise GolUnavailable("GOL runtime unavailable")
                if response.status_code != 200:
                    raise GolDenied("GOL operation unavailable")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > 2 * 1024 * 1024:
                        raise GolUnavailable("GOL runtime unavailable")
                    content.extend(chunk)
                return bytes(content)
        except httpx.HTTPError as error:
            _log_rpc_failure(phase, type(error).__name__)
            raise GolUnavailable("GOL runtime unavailable") from None

    async def open(self, request: GolRequest, admission: str) -> GolLease:
        """Leaves calculation reuse and source provenance under Gateway control."""
        scope: dict[str, object] = {
            **request.model_dump(by_alias=True),
            "admission": admission,
        }
        if request.metadata_target is not None:
            scope["metadataTarget"] = request.metadata_target
        try:
            async with asyncio.timeout(35.0):
                try:
                    response = await self._post(
                        "/internal/gol/runtime", scope, phase="open"
                    )
                except GolUnavailable:
                    # The exact admission is rechecked before a worker can run any effect.
                    response = await self._post(
                        "/internal/gol/runtime", scope, phase="open"
                    )
        except TimeoutError:
            _log_rpc_failure("open", "overall_deadline")
            raise GolUnavailable("GOL runtime unavailable") from None
        try:
            return GolLease.model_validate_json(response)
        except ValidationError:
            raise GolUnavailable("GOL runtime unavailable") from None

    async def resume(
        self, request: GolRequest, lineage: str, command_id: UUID
    ) -> str:
        """Exchanges signed ancestry for an admission owned by Gateway."""
        response = await self._post(
            "/internal/gol/runtime",
            {
                **request.model_dump(by_alias=True),
                "lineage": lineage,
                "commandId": str(command_id),
            },
            phase="resume",
        )
        try:
            admission = _EFFECT.validate_json(response).get("admission")
        except ValidationError:
            raise GolUnavailable("GOL admission unavailable") from None
        if not isinstance(admission, str) or len(admission) != 32:
            raise GolUnavailable("GOL admission unavailable")
        return admission

    @asynccontextmanager
    async def protected(
        self,
        request: GolRequest,
        selected: GolLease,
        admission: str,
        *,
        effects: Sequence[GolEffect | Mapping[str, object]],
    ) -> AsyncIterator[tuple[str | None, ...]]:
        """Retains the host's exclusion boundary until real I/O has completed."""
        async with self._protected_batch(
            request, selected, admission, effects=effects, select_readable=False
        ) as (receipts, _):
            yield receipts

    @asynccontextmanager
    async def authorized_reads(
        self,
        request: GolRequest,
        selected: GolLease,
        admission: str,
        *,
        effects: Sequence[GolEffect],
    ) -> AsyncIterator[tuple[bool, ...]]:
        """Returns a positional mask only while the live read scope is held."""
        async with self._protected_batch(
            request, selected, admission, effects=effects, select_readable=True
        ) as (_, allowed):
            if allowed is None:
                raise GolDenied("GOL read selection unavailable")
            yield allowed

    @asynccontextmanager
    async def _protected_batch(
        self,
        request: GolRequest,
        selected: GolLease,
        admission: str,
        *,
        effects: Sequence[GolEffect | Mapping[str, object]],
        select_readable: bool,
    ) -> AsyncIterator[tuple[tuple[str | None, ...], tuple[bool, ...] | None]]:

        if not effects or len(effects) > 256:
            raise GolDenied("GOL effect batch unavailable")
        try:
            checked = tuple(
                effect
                if isinstance(effect, GolEffect)
                else GolEffect.model_validate(effect)
                for effect in effects
            )
            for effect in checked:
                if effect.operation in {"write", "execute"} and (
                    effect.proposal is None or not effect.instance_id
                ):
                    raise GolDenied("GOL business proposal unavailable")
                if effect.proposal is not None:
                    proposal = _PROPOSAL.validate_python(
                        effect.proposal, strict=True
                    )
                    if len(orjson.dumps(proposal)) > 128 * 1024:
                        raise GolDenied("GOL business proposal exceeds limit")
            scope: dict[str, object] = {
                **request.model_dump(by_alias=True),
                "admission": admission,
                "contextId": selected.context_id,
                "generationId": selected.generation_id,
            }
            body = {
                **scope,
                "effects": [
                    effect.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )
                    for effect in checked
                ],
            }
            if select_readable:
                body["selectReadable"] = True
            if len(orjson.dumps(body)) > 160 * 1024:
                raise GolDenied("GOL effect batch exceeds limit")
        except (ValidationError, TypeError, ValueError):
            raise GolDenied("GOL effect batch unavailable") from None
        response = await self._post("/internal/gol/effect", body, phase="begin")
        try:
            result = _EFFECT.validate_json(response)
        except ValueError:
            raise GolDenied("GOL effect permit unavailable") from None
        permit = result.get("permit")
        receipts = result.get("receipts")
        if (
            result.get("allowed") is not True
            or not isinstance(permit, str)
            or not permit
            or len(permit) > 4096
            or not isinstance(receipts, list)
            or len(receipts) != len(checked)
            or any(
                receipt is not None
                and (
                    not isinstance(receipt, str)
                    or not receipt
                    or len(receipt) > 65536
                )
                for receipt in receipts
            )
            or any(
                effect.operation in {"write", "execute"} and not receipt
                for effect, receipt in zip(checked, receipts, strict=True)
            )
        ):
            raise GolDenied("GOL effect permit unavailable")
        succeeded = False
        try:
            allowed_reads = result.get("allowedReads")
            mask = None
            if select_readable:
                if (
                    not isinstance(allowed_reads, list)
                    or len(allowed_reads) != len(checked)
                    or any(
                        type(allowed) is not bool for allowed in allowed_reads
                    )
                ):
                    raise GolDenied("GOL read selection unavailable")
                mask = tuple(allowed is True for allowed in allowed_reads)
            yield (
                tuple(
                    receipt if isinstance(receipt, str) else None
                    for receipt in receipts
                ),
                mask,
            )
            succeeded = True
        finally:
            await self._finish(
                {**scope, "completion": permit, "succeeded": succeeded}
            )

    async def _finish(self, body: dict[str, object]) -> None:
        """Cancellation cannot silently release a calculation as successful."""
        task = asyncio.create_task(
            self._post("/internal/gol/effect", body, phase="finish")
        )
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        try:
            result = _EFFECT.validate_json(task.result())
        except ValueError:
            raise GolUnavailable("GOL effect completion unavailable") from None
        if result.get("allowed") is not True:
            raise GolDenied("GOL effect completion unavailable")
        if cancelled:
            raise asyncio.CancelledError

    async def close(self) -> None:
        """Releases the actor-owned transport without altering server provenance."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
