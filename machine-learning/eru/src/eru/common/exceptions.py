"""Custom exceptions used by Eru."""


class EruError(Exception):
    """Base exception for all Eru domain failures."""

    pass


class ExtractionError(EruError):
    """Raised when entity extraction cannot be completed."""

    pass


class ReferenceResolutionError(EruError):
    """Raised when coreference resolution cannot be completed."""

    pass


class SemanticNormalizationError(EruError):
    """Raised when semantic normalization cannot be completed."""

    pass


class MergeError(EruError):
    """Raised when entity merging cannot be completed."""

    pass


class ReasoningError(EruError):
    """Raised when LLM reasoning cannot produce a valid graph."""

    pass


class LogicValidationError(EruError):
    """Raised when final graph validation fails."""

    pass


class ModelResolutionError(EruError):
    """Raised when a configured model artifact cannot be located or fetched."""

    pass
