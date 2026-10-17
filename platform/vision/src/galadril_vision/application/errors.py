"""Domain failures shared by execution policy and its adapters."""


class CommandProcessingError(RuntimeError):
    """Raised for non-retryable command or pipeline configuration errors."""
