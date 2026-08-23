"""Durable idempotency claims for at-least-once Kafka command delivery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from galadril_pipeline.events import PipelineCommand, StepResult

from galadril_vision.connectors.postgres.client import PostgresClient


class ClaimState(StrEnum):
    """Possible outcomes when claiming a logical pipeline execution."""

    ACQUIRED = "acquired"
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"


@dataclass(frozen=True, slots=True)
class ExecutionClaim:
    """Claim result with a cached terminal value when already completed."""

    state: ClaimState
    result: StepResult | None = None


class PostgresExecutionLedger:
    """Coordinates command leases and results using atomic Postgres updates."""

    __slots__ = ("_client", "_lease_seconds")

    def __init__(
        self, client: PostgresClient, lease_seconds: int = 900
    ) -> None:
        """Configures a lease long enough for normal actor execution."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._client = client
        self._lease_seconds = lease_seconds

    async def claim(self, command: PipelineCommand) -> ExecutionClaim:
        """Acquires new, failed, or expired work without stealing live tasks."""
        lease_expires_at = datetime.now(UTC) + timedelta(
            seconds=self._lease_seconds
        )
        async with self._client.tenant_connection(
            command.tenant_id
        ) as connection:
            async with connection.transaction():
                inserted = await connection.execute(
                    """
                    INSERT INTO pipeline_executions (
                        idempotency_key, command_id, correlation_id, tenant_id,
                        pipeline, step, status, attempt, lease_expires_at,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, 'running', %s, %s, NOW(), NOW())
                    ON CONFLICT (idempotency_key) DO NOTHING
                    RETURNING idempotency_key
                    """,
                    (
                        command.idempotency_key,
                        str(command.event_id),
                        str(command.correlation_id),
                        command.tenant_id,
                        command.pipeline,
                        command.step,
                        command.attempt,
                        lease_expires_at,
                    ),
                )
                if await inserted.fetchone() is not None:
                    return ExecutionClaim(ClaimState.ACQUIRED)

                acquired = await connection.execute(
                    """
                    UPDATE pipeline_executions
                    SET status = 'running', attempt = %s, error = NULL,
                        lease_expires_at = %s, updated_at = NOW()
                    WHERE idempotency_key = %s
                      AND (status = 'failed' OR lease_expires_at < NOW())
                    RETURNING idempotency_key
                    """,
                    (
                        command.attempt,
                        lease_expires_at,
                        command.idempotency_key,
                    ),
                )
                if await acquired.fetchone() is not None:
                    return ExecutionClaim(ClaimState.ACQUIRED)

                existing = await connection.execute(
                    """
                    SELECT status, result
                    FROM pipeline_executions
                    WHERE idempotency_key = %s
                    """,
                    (command.idempotency_key,),
                )
                row = await existing.fetchone()
                if row is None:
                    raise RuntimeError(
                        "Execution claim disappeared during transaction"
                    )
                if row[0] == "completed" and row[1] is not None:
                    return ExecutionClaim(
                        ClaimState.COMPLETED,
                        StepResult.model_validate(row[1]),
                    )
                return ExecutionClaim(ClaimState.IN_PROGRESS)

    async def complete(
        self, command: PipelineCommand, result: StepResult
    ) -> None:
        """Persists a successful result before downstream publication."""
        async with self._client.tenant_connection(
            command.tenant_id
        ) as connection:
            updated = await connection.execute(
                """
                UPDATE pipeline_executions
                SET status = 'completed', result = %s::jsonb, error = NULL,
                    updated_at = NOW()
                WHERE idempotency_key = %s AND status = 'running'
                """,
                (result.model_dump_json(), command.idempotency_key),
            )
            if updated.rowcount != 1:
                raise RuntimeError(
                    f"Execution lease was lost before completion: {command.idempotency_key}"
                )

    async def fail(self, command: PipelineCommand, error: Exception) -> None:
        """Releases a failed claim for a bounded retry command."""
        async with self._client.tenant_connection(
            command.tenant_id
        ) as connection:
            await connection.execute(
                """
                UPDATE pipeline_executions
                SET status = 'failed', error = %s, updated_at = NOW()
                WHERE idempotency_key = %s AND status = 'running'
                """,
                (str(error)[:4096], command.idempotency_key),
            )


class MemoryExecutionLedger:
    """Concurrency-safe ledger used by unit tests and local broker simulations."""

    __slots__ = ("_claims", "_lock")

    def __init__(self) -> None:
        self._claims: dict[str, StepResult | None] = {}
        self._lock = asyncio.Lock()

    async def claim(self, command: PipelineCommand) -> ExecutionClaim:
        """Claims a command once within the current process."""
        async with self._lock:
            key = command.idempotency_key
            if key not in self._claims:
                self._claims[key] = None
                return ExecutionClaim(ClaimState.ACQUIRED)
            result = self._claims[key]
            if result is None:
                return ExecutionClaim(ClaimState.IN_PROGRESS)
            return ExecutionClaim(ClaimState.COMPLETED, result)

    async def complete(
        self, command: PipelineCommand, result: StepResult
    ) -> None:
        """Stores the terminal result for duplicate replay tests."""
        async with self._lock:
            if command.idempotency_key not in self._claims:
                raise RuntimeError("Cannot complete an unclaimed command")
            self._claims[command.idempotency_key] = result

    async def fail(self, command: PipelineCommand, error: Exception) -> None:
        """Removes a failed claim so a bounded retry can acquire it."""
        del error
        async with self._lock:
            self._claims.pop(command.idempotency_key, None)
