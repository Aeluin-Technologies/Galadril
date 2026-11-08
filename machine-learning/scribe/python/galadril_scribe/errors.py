"""Domain failures that tools may expose as bounded availability results."""


class ToolFailure(RuntimeError):
    """A privileged dependency refused or failed an operation."""
