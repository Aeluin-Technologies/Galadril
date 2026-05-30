"""AuthZ outbox flusher (Postgres -> SpiceDB) with Kafka DLQ fallback."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
import structlog
from psycopg import AsyncConnection

from galadril_vision.common.config import KafkaConfig, SpiceDBConfig
from galadril_vision.connectors.authz.spicedb import AuthzTuple, SpiceDBWriter
from galadril_vision.connectors.kafka.producer import (
    KafkaJsonProducer,
    resolve_authz_dlq_topic,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    tenant_id: str
    object_id: str
    tuples: list[AuthzTuple]
    attempts: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _compute_backoff(
    *,
    base_ms: int,
    max_ms: int,
    attempt: int,
) -> int:
    """
    Exponential backoff with jitter.
    attempt is >= 1.
    """
    exp = min(attempt, 30)
    delay_ms = min(max_ms, base_ms * (2**exp))
    return random.randint(0, delay_ms)


class AuthzOutboxFlusher:
    """
    Flushes authz_outbox rows to SpiceDB.

    Safety properties:
      - never holds a DB transaction open across network calls to SpiceDB
      - uses SKIP LOCKED to allow horizontal scaling
      - bounded local retries; after threshold sends to Kafka DLQ
    """

    def __init__(
        self,
        *,
        spicedb_cfg: SpiceDBConfig,
        kafka_cfg: KafkaConfig,
        dlq_producer: KafkaJsonProducer,
        writer: SpiceDBWriter | None = None,
    ) -> None:
        self._spicedb_cfg = spicedb_cfg
        self._kafka_cfg = kafka_cfg
        self._dlq_producer = dlq_producer
        self._writer = writer or SpiceDBWriter(spicedb_cfg)
        self._dlq_topic = resolve_authz_dlq_topic(kafka_cfg)

    async def run_forever(
        self,
        *,
        conn: AsyncConnection,
        poll_interval_s: float = 0.5,
        batch_size: int = 50,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """
        Main loop. Use a dedicated connection or a lightweight pool connection.
        """
        while stop_event is None or not stop_event.is_set():
            try:
                rows = await self._claim_due_rows(conn=conn, limit=batch_size)
                if not rows:
                    await asyncio.sleep(poll_interval_s)
                    continue

                for row in rows:
                    await self._flush_one(conn=conn, row=row)

                # Let producer callbacks progress.
                self._dlq_producer.poll(0.0)
            except Exception as exc:
                logger.error("authz_outbox_loop_failed", error=str(exc))
                await asyncio.sleep(1.0)

    async def _claim_due_rows(
        self, *, conn: AsyncConnection, limit: int
    ) -> list[OutboxRow]:
        """
        Claim due rows for processing using SELECT ... FOR UPDATE SKIP LOCKED.

        We keep the transaction short and only use it to lock + fetch.
        """
        now = _utcnow()

        async with conn.transaction():
            res = await conn.execute(
                """
                SELECT id, tenant_id, object_id, tuples_json, attempts
                FROM authz_outbox
                WHERE next_retry_at <= $1
                ORDER BY next_retry_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT $2
                """,
                (now, int(limit)),
            )
            raw_rows = await res.fetchall()

        out: list[OutboxRow] = []
        out_append = out.append

        for r in raw_rows:
            try:
                row_id = int(r[0])
                tenant_id = str(r[1])
                object_id = str(r[2])
                tuples_json = r[3]
                attempts = int(r[4])

                tuples = self._parse_tuples(tuples_json)
                out_append(
                    OutboxRow(
                        id=row_id,
                        tenant_id=tenant_id,
                        object_id=object_id,
                        tuples=tuples,
                        attempts=attempts,
                    )
                )
            except Exception:
                # If parsing fails, DLQ it and delete row to prevent infinite poison-pill loops.
                await self._dlq_poison_pill(
                    conn=conn,
                    row_id=int(r[0]),
                    tenant_id=str(r[1]),
                    object_id=str(r[2]),
                    tuples_json=r[3],
                    reason="tuple_parse_failed",
                )

        return out

    def _parse_tuples(self, tuples_json: Any) -> list[AuthzTuple]:
        """
        Parse tuples_json from DB (JSONB).

        psycopg may return dict/list already; handle both bytes/str/list.
        """
        if tuples_json is None:
            return []

        if isinstance(tuples_json, (bytes, bytearray, memoryview)):
            data = orjson.loads(bytes(tuples_json))
        elif isinstance(tuples_json, str):
            data = orjson.loads(tuples_json)
        else:
            data = tuples_json

        if not isinstance(data, list):
            return []

        out: list[AuthzTuple] = []
        out_append = out.append
        for item in data:
            if not isinstance(item, dict):
                continue
            resource = item.get("resource")
            relation = item.get("relation")
            subject = item.get("subject")
            if (
                isinstance(resource, str)
                and isinstance(relation, str)
                and isinstance(subject, str)
                and resource
                and relation
                and subject
            ):
                out_append(
                    AuthzTuple(
                        resource=resource, relation=relation, subject=subject
                    )
                )
        return out

    async def _flush_one(
        self, *, conn: AsyncConnection, row: OutboxRow
    ) -> None:
        max_local = int(self._spicedb_cfg.max_local_retries)
        attempt_n = row.attempts + 1

        try:
            await self._writer.write_relationships(row.tuples)
        except Exception as exc:
            logger.warning(
                "spicedb_write_failed",
                outbox_id=row.id,
                attempt=attempt_n,
                error=str(exc),
            )

            if attempt_n >= max_local:
                await self._send_to_dlq_and_reschedule(
                    conn=conn,
                    row=row,
                    attempt_n=attempt_n,
                    error=str(exc),
                )
                return

            delay_ms = _compute_backoff(
                base_ms=int(self._spicedb_cfg.base_retry_ms),
                max_ms=int(self._spicedb_cfg.max_retry_ms),
                attempt=attempt_n,
            )
            await self._reschedule(
                conn=conn, row_id=row.id, attempt_n=attempt_n, delay_ms=delay_ms
            )
            return

        async with conn.transaction():
            await conn.execute(
                "DELETE FROM authz_outbox WHERE id = $1",
                (row.id,),
            )

        logger.debug(
            "authz_outbox_flushed", outbox_id=row.id, object_id=row.object_id
        )

    async def _reschedule(
        self,
        *,
        conn: AsyncConnection,
        row_id: int,
        attempt_n: int,
        delay_ms: int,
    ) -> None:
        next_retry = _utcnow() + timedelta(milliseconds=int(delay_ms))
        async with conn.transaction():
            await conn.execute(
                """
                UPDATE authz_outbox
                SET attempts = $2,
                    next_retry_at = $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                (int(row_id), int(attempt_n), next_retry),
            )

    async def _send_to_dlq_and_reschedule(
        self,
        *,
        conn: AsyncConnection,
        row: OutboxRow,
        attempt_n: int,
        error: str,
    ) -> None:
        payload = {
            "kind": "authz_outbox_flush_failed",
            "outbox_id": row.id,
            "tenant_id": row.tenant_id,
            "object_id": row.object_id,
            "attempts": attempt_n,
            "error": error,
            "tuples": [
                {
                    "resource": t.resource,
                    "relation": t.relation,
                    "subject": t.subject,
                }
                for t in row.tuples
            ],
            "ts": _utcnow().isoformat(),
        }
        key = f"{row.tenant_id}:{row.object_id}"
        self._dlq_producer.produce_json(
            topic=self._dlq_topic, key=key, payload=payload
        )

        await self._reschedule(
            conn=conn, row_id=row.id, attempt_n=attempt_n, delay_ms=60_000
        )

        logger.error(
            "authz_sent_to_dlq",
            outbox_id=row.id,
            dlq_topic=self._dlq_topic,
            tenant_id=row.tenant_id,
            object_id=row.object_id,
        )

    async def _dlq_poison_pill(
        self,
        *,
        conn: AsyncConnection,
        row_id: int,
        tenant_id: str,
        object_id: str,
        tuples_json: Any,
        reason: str,
    ) -> None:
        payload = {
            "kind": "authz_outbox_poison_pill",
            "reason": reason,
            "outbox_id": row_id,
            "tenant_id": tenant_id,
            "object_id": object_id,
            "tuples_json": tuples_json,
            "ts": _utcnow().isoformat(),
        }
        key = f"{tenant_id}:{object_id}"
        self._dlq_producer.produce_json(
            topic=self._dlq_topic, key=key, payload=payload
        )

        async with conn.transaction():
            await conn.execute(
                "DELETE FROM authz_outbox WHERE id = $1", (int(row_id),)
            )

        logger.error(
            "authz_outbox_poison_pill_deleted",
            outbox_id=row_id,
            dlq_topic=self._dlq_topic,
        )
