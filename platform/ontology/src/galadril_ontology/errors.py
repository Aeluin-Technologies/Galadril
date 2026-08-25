"""Domain exceptions for ontology validation and version control."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """Describes one semantic invariant violation."""

    code: str
    message: str
    resource_id: str | None = None
    path: tuple[str, ...] = ()


class OntologyError(Exception):
    """Base exception for ontology domain failures."""


class OntologyNotFoundError(OntologyError):
    """Fails closed when a tenant-scoped ontology object is unavailable."""


class BranchAlreadyExistsError(OntologyError):
    """Raised when a tenant branch name is already allocated."""


class ConcurrentHeadUpdateError(OntologyError):
    """Raised when branch HEAD changed after a caller read it."""


class InvalidOntologyChangeError(OntologyError):
    """Raised when a semantic operation cannot apply to its current overlay."""


class BaseVersionMismatchError(OntologyError):
    """Raised when branches must synchronize before they can merge."""


class OntologyCompatibilityError(OntologyError):
    """Raised when an ontology slice cannot satisfy a processing block."""


class OntologyValidationError(OntologyError):
    """Carries all semantic validation failures for an effective ontology."""

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        self.issues = issues
        summary = "; ".join(issue.message for issue in issues)
        super().__init__(f"Ontology validation failed: {summary}")
