"""Unit tests for idempotent pipeline execution claims."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from galadril_pipeline.config import StepType
from galadril_pipeline.events import (
    EventStatus,
    PipelineCommand,
    ResourceClass,
    StepResult,
)
from galadril_vision.pipeline.ledger import (
    ClaimState,
    MemoryExecutionLedger,
    PostgresExecutionLedger,
)


def _command() -> PipelineCommand:
    """Builds a stable command fixture used across duplicate claims."""
    return PipelineCommand(
        correlation_id=uuid4(),
        pipeline="vision",
        step="infer",
        step_type=StepType.INFERENCE,
        resource_class=ResourceClass.GPU,
    )


def _result(command: PipelineCommand) -> StepResult:
    """Builds the durable result stored for a completed command."""
    return StepResult(
        correlation_id=command.correlation_id,
        causation_id=command.event_id,
        pipeline=command.pipeline,
        command_id=command.event_id,
        step=command.step,
        step_type=command.step_type,
        resource_class=command.resource_class,
        status=EventStatus.COMPLETED,
        duration_seconds=0.1,
    )


def _postgres_mocks() -> tuple[MagicMock, MagicMock]:
    """Creates a client/connection pair with psycopg-shaped async methods."""
    client = MagicMock()
    client.connection = MagicMock()
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.transaction = MagicMock()
    client.connection.return_value.__aenter__.return_value = connection
    return client, connection


@pytest.mark.asyncio
async def test_completed_claim_replays_durable_result() -> None:
    """Ensures redelivery resumes publication without rerunning the actor."""
    ledger = MemoryExecutionLedger()
    command = _command()
    claim = await ledger.claim(command)
    assert claim.state is ClaimState.ACQUIRED

    result = _result(command)
    await ledger.complete(command, result)

    duplicate = await ledger.claim(command)
    assert duplicate.state is ClaimState.COMPLETED
    assert duplicate.result == result


@pytest.mark.asyncio
async def test_live_duplicate_is_not_acquired() -> None:
    """Prevents concurrent delivery from dispatching the same logical task twice."""
    ledger = MemoryExecutionLedger()
    command = _command()

    assert (await ledger.claim(command)).state is ClaimState.ACQUIRED
    assert (await ledger.claim(command)).state is ClaimState.IN_PROGRESS


@pytest.mark.asyncio
async def test_postgres_claim_acquires_new_command_atomically() -> None:
    """Ensures an inserted idempotency row grants the execution lease."""
    client, connection = _postgres_mocks()
    inserted = MagicMock()
    inserted.fetchone = AsyncMock(return_value=("key",))
    connection.execute.return_value = inserted

    claim = await PostgresExecutionLedger(client).claim(_command())

    assert claim.state is ClaimState.ACQUIRED
    assert connection.execute.await_count == 1


@pytest.mark.asyncio
async def test_postgres_claim_replays_completed_result() -> None:
    """Ensures duplicate delivery reads the durable result without actor work."""
    client, connection = _postgres_mocks()
    command = _command()
    expected = _result(command)
    inserted = MagicMock()
    inserted.fetchone = AsyncMock(return_value=None)
    acquired = MagicMock()
    acquired.fetchone = AsyncMock(return_value=None)
    existing = MagicMock()
    existing.fetchone = AsyncMock(
        return_value=("completed", expected.model_dump(mode="json"))
    )
    connection.execute.side_effect = (inserted, acquired, existing)

    claim = await PostgresExecutionLedger(client).claim(command)

    assert claim.state is ClaimState.COMPLETED
    assert claim.result == expected


@pytest.mark.asyncio
async def test_postgres_complete_detects_lost_lease() -> None:
    """Prevents a stale worker from overwriting a lease owned elsewhere."""
    client, connection = _postgres_mocks()
    command = _command()
    updated = MagicMock(rowcount=0)
    connection.execute.return_value = updated

    with pytest.raises(RuntimeError, match="Execution lease was lost"):
        await PostgresExecutionLedger(client).complete(
            command, _result(command)
        )
