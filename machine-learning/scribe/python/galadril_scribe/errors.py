"""Domain failures that tools may expose as bounded availability results."""


class ToolFailure(RuntimeError):
    """A privileged dependency refused or failed an operation."""


class UnavailableToolResult(str):
    """Preserve safe tool output while signaling run-local recovery policy."""

    __slots__ = ()
