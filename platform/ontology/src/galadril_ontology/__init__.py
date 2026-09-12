"""Ontology wire models and Vision runtime compatibility contracts."""

from galadril_ontology.errors import (
    OntologyCompatibilityError,
    OntologyError,
    OntologyNotFoundError,
)
from galadril_ontology.identity import (
    normalize_tenant_id,
    require_same_tenant,
)
from galadril_ontology.model import (
    Ontology,
    OntologyResource,
    ResourceKind,
)
from galadril_ontology.runtime import (
    BlockOntologyContract,
    OntologyRuntimeManager,
    OntologySlice,
    OntologySliceMetadata,
    OntologySliceRequest,
    active_ontology_slice,
)

__all__ = [
    "BlockOntologyContract",
    "Ontology",
    "OntologyCompatibilityError",
    "OntologyError",
    "OntologyNotFoundError",
    "OntologyRuntimeManager",
    "OntologySlice",
    "OntologySliceMetadata",
    "OntologySliceRequest",
    "OntologyResource",
    "ResourceKind",
    "active_ontology_slice",
    "normalize_tenant_id",
    "require_same_tenant",
]
