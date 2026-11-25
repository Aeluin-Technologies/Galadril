"""Domain failures shared by execution policy and its adapters."""

from enum import StrEnum


class CommandFailureReason(StrEnum):
    """Public diagnostic categories carried across the worker boundary."""

    RUNNING_LINEAGE = "running_lineage"
    GOL_RUNTIME_OPEN = "gol_runtime_open"
    GOL_HANDLER = "gol_handler"
    GOL_RUNTIME_UNAVAILABLE = "gol_runtime_unavailable"
    GOL_HANDLER_UNAVAILABLE = "gol_handler_unavailable"
    WORKER_EXECUTION = "worker_execution"
    WORKER_RESULT = "worker_result"
    LEDGER_COMPLETION = "ledger_completion"

    def public_message(self) -> str:
        return f"Pipeline execution unavailable ({self.value})"


class CommandProcessingError(RuntimeError):
    """Raised for non-retryable command or pipeline configuration errors."""

    def __init__(
        self, message: str, *, reason: CommandFailureReason | None = None
    ) -> None:
        self.reason = reason
        super().__init__(message)
