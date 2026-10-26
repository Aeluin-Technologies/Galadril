"""Derived HippoRAG2 memory is anchored to ESKG rather than creating new authority."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Annotated, cast

import httpx
import orjson
import structlog
from galadril_ontology import active_ontology_slice
from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    JsonValue,
    TypeAdapter,
)

from galadril_vision.application.errors import CommandProcessingError
from galadril_vision.common.config import VisionConfig
from galadril_vision.compute.helpers import (
    _extract_text_payload,
    source_metadata,
)
from galadril_vision.compute.tasks import PostgresRuntimeState, get_pg_stores

_OBJECT = TypeAdapter(dict[str, JsonValue])
_ID = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, hide_input_in_errors=True
    )


class Fact(Contract):
    subject: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=256)
    object: str = Field(min_length=1, max_length=256)


class PassageFacts(Contract):
    id: _ID
    facts: tuple[Fact, ...] = Field(default=(), max_length=8)


class Extracted(Contract):
    passages: tuple[PassageFacts, ...] = Field(max_length=8)
    warnings: tuple[str, ...] = Field(default=(), max_length=8)


class Embedded(Contract):
    profile_id: _ID
    vectors: tuple[
        Annotated[
            tuple[FiniteFloat, ...], Field(min_length=1, max_length=4096)
        ],
        ...,
    ] = Field(min_length=1, max_length=32)


def evidence_units(
    tenant: str, record: str, raw: dict[str, JsonValue]
) -> list[dict[str, str]]:
    """Stable IDs include tenant and source, so retries cannot duplicate or merge evidence."""
    content = _extract_text_payload(raw)
    if not content or len(content) > 262144 or len(content.encode()) > 1048576:
        raise ValueError("Evidence text is unavailable or exceeds limit")
    metadata = source_metadata(raw)
    headers = "\n".join(
        f"{key}: {metadata[key]}"
        for key in ("subject", "sender", "recipients", "date")
        if key in metadata
    )
    text = (headers + "\n\n" + content) if headers else content
    if len(text) > 262144:
        raise ValueError("Evidence text exceeds limit")
    units = []
    for start in range(0, len(text), 2048):
        passage = text[start : start + 2048]
        identifier = hashlib.sha256(
            f"{tenant}\0{record}\0{start}\0{passage}".encode()
        ).hexdigest()
        units.append({"id": identifier, "text": passage})
    return units


class MemorySink:
    __slots__ = ("_config", "_http", "_state")

    def __init__(
        self, config: VisionConfig, *, http: httpx.AsyncClient | None = None
    ) -> None:
        self._config = config
        self._http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(120), follow_redirects=False
        )
        self._state = PostgresRuntimeState()

    async def _operation(
        self, operation: str, payload: dict[str, JsonValue]
    ) -> bytes:
        endpoint = self._config.connectors.scribe_endpoint.rstrip("/")
        async with self._http.stream(
            "POST", f"{endpoint}/documents/{operation}", json=payload
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 2097152:
                    raise ValueError(
                        "Evidence inference response exceeds limit"
                    )
                body.extend(chunk)
            return bytes(body)

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Commit only complete bound extraction; the ESKG branch owns visibility grants."""
        with trace.get_tracer(__name__).start_as_current_span(
            "pipeline.memory.index",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return await self._index(command, step)
            except Exception as error:
                span.set_status(
                    Status(StatusCode.ERROR, "evidence_indexing_failed")
                )
                structlog.get_logger(__name__).warning(
                    "pipeline.memory.unavailable",
                    error_type=type(error).__name__,
                )
                raise CommandProcessingError(
                    "Evidence indexing unavailable"
                ) from None

    async def _index(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        """Keep extraction complete before committing any feature or pruning old units."""
        record = _OBJECT.validate_python(command.payload.get("record"))
        raw = _OBJECT.validate_python(record.get("raw_payload"))
        record_id = record.get("record_id")
        if (
            not isinstance(record_id, str)
            or not record_id
            or len(record_id) > 252
        ):
            raise ValueError("Invalid evidence record identity")
        timestamp = record.get("timestamp")
        if not isinstance(timestamp, str):
            raise ValueError("Missing evidence timestamp")
        event_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if event_time.tzinfo is None:
            raise ValueError("Evidence timestamp must include its timezone")
        units = evidence_units(command.tenant_id, record_id, raw)
        metadata = source_metadata(raw)
        checksum = hashlib.sha256(orjson.dumps(raw)).hexdigest()
        features: list[
            tuple[str, tuple[Fact, ...], str, tuple[float, ...]]
        ] = []
        ontology = active_ontology_slice()
        if (
            ontology is not None
            and ontology.metadata.tenant_id != command.tenant_id
        ):
            raise ValueError("Evidence ontology tenant mismatch")
        for start in range(0, len(units), 8):
            batch = units[start : start + 8]
            extracted = Extracted.model_validate_json(
                await self._operation(
                    "extract",
                    {
                        "passages": cast(list[JsonValue], batch),
                        "ontology": ontology.model_dump(mode="json")
                        if ontology is not None
                        else None,
                    },
                )
            )
            embedded = Embedded.model_validate_json(
                await self._operation(
                    "embed", {"texts": [unit["text"] for unit in batch]}
                )
            )
            facts = {
                passage.id: passage.facts for passage in extracted.passages
            }
            if (
                len(facts) != len(batch)
                or set(facts) != {unit["id"] for unit in batch}
                or len(embedded.vectors) != len(batch)
            ):
                raise ValueError("Evidence inference source mismatch")
            features.extend(
                (unit["id"], facts[unit["id"]], embedded.profile_id, vector)
                for unit, vector in zip(batch, embedded.vectors, strict=True)
            )
        client, _, _ = await get_pg_stores(self._config.postgres, self._state)
        pipeline = (
            step.origin.pipeline_id
            if step.origin
            else self._config.ontology_pipeline_id
        )
        revision = (
            step.origin.revision_id
            if step.origin
            else self._config.runtime_revision_id
        )
        if not pipeline or not revision:
            raise ValueError("Evidence requires a published pipeline revision")
        rows = [
            (
                command.tenant_id,
                record_id,
                unit["id"],
                f"evt_{record_id}",
                event_time,
                checksum,
                unit["text"],
                orjson.dumps(metadata).decode(),
                orjson.dumps([fact.model_dump() for fact in facts]).decode(),
                profile,
                list(vector),
                pipeline,
                revision,
            )
            for unit, (_, facts, profile, vector) in zip(
                units, features, strict=True
            )
        ]
        async with client.tenant_connection(command.tenant_id) as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM pipeline_evidence WHERE tenant_id=%s AND source_id=%s AND NOT(unit_id=ANY(%s))",
                    (
                        command.tenant_id,
                        record_id,
                        [unit["id"] for unit in units],
                    ),
                )
                await cursor.executemany(
                    """
                    INSERT INTO pipeline_evidence(tenant_id,source_id,unit_id,event_id,event_time,checksum,text,metadata,facts,profile_id,embedding,pipeline_id,revision_id)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s)
                    ON CONFLICT(tenant_id,source_id,unit_id) DO UPDATE SET
                      checksum=EXCLUDED.checksum,text=EXCLUDED.text,metadata=EXCLUDED.metadata,
                      facts=EXCLUDED.facts,profile_id=EXCLUDED.profile_id,embedding=EXCLUDED.embedding,
                      pipeline_id=EXCLUDED.pipeline_id,revision_id=EXCLUDED.revision_id,updated_at=now()
                """,
                    rows,
                )
        structlog.get_logger(__name__).info(
            "pipeline.memory.indexed", units=len(rows)
        )
        return {**command.payload, "indexed_units": len(rows)}
