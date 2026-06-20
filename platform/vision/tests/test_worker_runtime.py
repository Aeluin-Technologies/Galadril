"""Unit tests for the thread-local async worker bridge."""

from __future__ import annotations

from galadril_vision.pipeline.worker_runtime import run_blocking


def test_run_blocking_returns_coroutine_result() -> None:
    """Verify the sync bridge returns the awaited result."""

    async def _value() -> int:
        return 42

    assert run_blocking(_value()) == 42
