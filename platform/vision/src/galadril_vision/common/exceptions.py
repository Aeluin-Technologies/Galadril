"""Exceptions for galadril-vision."""


class GaladrilVisionError(Exception):
    """Base exception for all vision pipeline errors."""


class KafkaConsumerError(GaladrilVisionError):
    """Raised when Kafka consumer encounters an error."""


class ImageDownloadError(GaladrilVisionError):
    """Raised when an image cannot be downloaded from S3."""

    def __init__(self, storage_path: str, reason: str) -> None:
        self.storage_path = storage_path
        self.reason = reason
        super().__init__(f"Failed to download '{storage_path}': {reason}")


class IdentificationError(GaladrilVisionError):
    """Raised when face identification fails."""


class GraphOperationError(GaladrilVisionError):
    """Raised when Apache AGE graph operation fails."""

    def __init__(self, operation: str, reason: str) -> None:
        self.operation = operation
        self.reason = reason
        super().__init__(f"Graph operation '{operation}' failed: {reason}")


class VectorSearchError(GaladrilVisionError):
    """Raised when pgvectorscale similarity search fails."""


class TenantIsolationError(GaladrilVisionError):
    """Raised when tenant scope is missing or inconsistent."""

    def __init__(self, reason: str, *, tenant_id: str | None = None) -> None:
        self.reason = reason
        self.tenant_id = tenant_id
        detail = reason if tenant_id is None else f"{reason}: {tenant_id}"
        super().__init__(f"Tenant isolation violation: {detail}")
