"""Bounded failures do not reveal protected resource metadata."""

from uuid import UUID


class GolError(Exception):
    """The admitted GOL operation could not be completed."""

    __slots__ = ("diagnostic_code", "occurrence_id")

    def __init__(
        self,
        message: str,
        *,
        diagnostic_code: str | None = None,
        occurrence_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.occurrence_id = occurrence_id


class GolDenied(GolError):
    """An authority or provenance obligation was not satisfied."""


class GolUnavailable(GolError):
    """No usable immutable runtime projection or decision was available."""
