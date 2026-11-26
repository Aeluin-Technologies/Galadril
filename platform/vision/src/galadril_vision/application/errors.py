"""Structured outcomes shared by execution and its transport adapters."""

from galadril_pipeline.events import Failure
from galadril_pipeline.telemetry_contract import ErrorCode, VisionPhase


class CommandProcessingError(RuntimeError):
    """Carries the original failure independently of the local exception text."""

    __slots__ = ("failure",)

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INVALID_INPUT,
        phase: VisionPhase = VisionPhase.EXECUTE,
        failure: Failure | None = None,
    ) -> None:
        super().__init__(message)
        self.failure = failure or Failure(code=code, phase=phase)


def execution_failure(error: Exception, phase: VisionPhase) -> Failure:
    """Assigns an occurrence only when no upstream structured cause exists."""
    if isinstance(error, CommandProcessingError):
        return error.failure
    if isinstance(error, TimeoutError):
        code = ErrorCode.DEADLINE_EXCEEDED
    elif phase is VisionPhase.RESULT:
        code = ErrorCode.INVALID_RESULT
    else:
        code = ErrorCode.INTERNAL
    return Failure(code=code, phase=phase)
