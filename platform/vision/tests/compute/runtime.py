"""Unit tests for driving asynchronous tasks within synchronous execution slots."""

from unittest.mock import patch

from galadril_vision.compute.runtime import run_blocking


class TestRuntimeModule:
    """Evaluates task execution bridges driving core background loops."""

    def test_run_blocking_execution_loop(self) -> None:
        """Ensures target coroutines route accurately through top-level async execution blocks."""

        async def dummy_coro() -> str:
            return "coro_complete"

        with patch("galadril_vision.compute.runtime.asyncio.run") as mock_run:
            mock_run.return_value = "coro_complete"
            res = run_blocking(dummy_coro())
            assert res == "coro_complete"
            mock_run.assert_called_once()
