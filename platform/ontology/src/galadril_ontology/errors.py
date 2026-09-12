"""Domain exceptions for ontology wire and runtime compatibility failures."""

from __future__ import annotations


class OntologyError(Exception):
    """Base exception for ontology domain failures."""


class OntologyNotFoundError(OntologyError):
    """Fails closed when a tenant-scoped ontology object is unavailable."""


class OntologyCompatibilityError(OntologyError):
    """Raised when an ontology slice cannot satisfy a processing block."""
