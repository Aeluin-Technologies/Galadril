"""Structural instance-graph vocabulary, independent of Registry definitions."""

from __future__ import annotations

from enum import StrEnum, unique

from galadril_ontology import ResourceKind, active_ontology_slice
from galadril_ontology.identity import normalize_tenant_id, validate_resource_id
from pydantic import BaseModel, ConfigDict, Field, field_validator


@unique
class GraphNodeKind(StrEnum):
    """Closed structural roles from the ESKG v1 contract."""

    ENTITY = "Entity"
    EVENT = "Event"
    STATE = "State"
    LATENT_IDENTITY = "LatentIdentity"
    INFERENCE = "Inference"
    DECISION = "Decision"
    CAUSAL_VARIABLE = "CausalVariable"
    METRIC = "Metric"


ONTOLOGY_KINDS: dict[GraphNodeKind, ResourceKind] = {
    GraphNodeKind.ENTITY: ResourceKind.OBJECT_TYPE,
    GraphNodeKind.EVENT: ResourceKind.EVENT_TYPE,
    GraphNodeKind.STATE: ResourceKind.PROPERTY,
    GraphNodeKind.LATENT_IDENTITY: ResourceKind.OBJECT_TYPE,
    GraphNodeKind.CAUSAL_VARIABLE: ResourceKind.PROPERTY,
}


class OntologyReference(BaseModel):
    """Pins classification without embedding an ontology definition in AGE."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str
    ontology_id: str = Field(min_length=1, max_length=128)
    revision_id: str = Field(pattern=r"^[A-Za-z0-9_-]{20,128}$")
    resource_id: str
    resource_kind: ResourceKind

    @field_validator("tenant_id")
    @classmethod
    def _tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("resource_id")
    @classmethod
    def _resource(cls, value: str) -> str:
        return validate_resource_id(value)


def resolve_ontology_reference(
    resource_id: object, tenant_id: str, kind: GraphNodeKind
) -> OntologyReference | None:
    """Derives provenance exclusively from the active, validated Registry slice."""
    if resource_id is None:
        return None
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError("ontology resource identifier is required")
    active = active_ontology_slice()
    if active is None:
        raise ValueError("classification requires an active Registry slice")
    if active.metadata.tenant_id != normalize_tenant_id(tenant_id):
        raise ValueError("classification crosses the active ontology tenant")
    resource = active.ontology.get(resource_id)
    if resource is None:
        raise ValueError("ontology classification resource is unavailable")
    if ONTOLOGY_KINDS.get(kind) != resource.kind:
        raise ValueError(
            "ontology resource kind is incompatible with graph kind"
        )
    return OntologyReference(
        tenant_id=active.metadata.tenant_id,
        ontology_id=active.metadata.ontology_id,
        revision_id=active.metadata.revision_id,
        resource_id=resource.resource_id,
        resource_kind=resource.kind,
    )


RELATION_ENDPOINTS: dict[
    str, tuple[tuple[GraphNodeKind, GraphNodeKind], ...]
] = {
    "TRIGGERS": ((GraphNodeKind.STATE, GraphNodeKind.EVENT),),
    "LEADS_TO": ((GraphNodeKind.EVENT, GraphNodeKind.STATE),),
    "EVOLUTION": ((GraphNodeKind.STATE, GraphNodeKind.STATE),),
    "CONTAIN": ((GraphNodeKind.EVENT, GraphNodeKind.EVENT),),
    "OCCUR": ((GraphNodeKind.EVENT, GraphNodeKind.ENTITY),),
    "INFLUENCE": ((GraphNodeKind.EVENT, GraphNodeKind.ENTITY),),
    "HAS_INFERENCE": ((GraphNodeKind.EVENT, GraphNodeKind.INFERENCE),),
    "CONSIDERED_CANDIDATE": (
        (GraphNodeKind.INFERENCE, GraphNodeKind.ENTITY),
        (GraphNodeKind.INFERENCE, GraphNodeKind.LATENT_IDENTITY),
    ),
    "HAS_DECISION": ((GraphNodeKind.INFERENCE, GraphNodeKind.DECISION),),
    "SELECTED_TARGET": (
        (GraphNodeKind.DECISION, GraphNodeKind.ENTITY),
        (GraphNodeKind.DECISION, GraphNodeKind.LATENT_IDENTITY),
    ),
    "PROMOTED_AS": ((GraphNodeKind.LATENT_IDENTITY, GraphNodeKind.ENTITY),),
    "MERGED_WITH": (
        (GraphNodeKind.LATENT_IDENTITY, GraphNodeKind.LATENT_IDENTITY),
    ),
    "STATE_OF": ((GraphNodeKind.STATE, GraphNodeKind.ENTITY),),
    "CAUSES": ((GraphNodeKind.CAUSAL_VARIABLE, GraphNodeKind.CAUSAL_VARIABLE),),
    "METRIC_INFLUENCE": ((GraphNodeKind.METRIC, GraphNodeKind.METRIC),),
}
for _relation in ("APPEARS_IN", "PARTICIPATED_IN", "DERIVED_FROM", "OBSERVED"):
    RELATION_ENDPOINTS[_relation] = (
        (GraphNodeKind.ENTITY, GraphNodeKind.EVENT),
    )
for _relation in ("MENTIONS", "DESCRIBES"):
    RELATION_ENDPOINTS[_relation] = (
        (GraphNodeKind.EVENT, GraphNodeKind.ENTITY),
    )
for _relation in ("SUPERSEDES", "REVOKES"):
    RELATION_ENDPOINTS[_relation] = tuple(
        (kind, kind)
        for kind in (
            GraphNodeKind.EVENT,
            GraphNodeKind.STATE,
            GraphNodeKind.INFERENCE,
            GraphNodeKind.DECISION,
            GraphNodeKind.LATENT_IDENTITY,
        )
    )


def relation_endpoints(
    relation: str,
) -> tuple[tuple[GraphNodeKind, GraphNodeKind], ...]:
    """Rejects arbitrary ontology link names at the graph boundary."""
    pairs = RELATION_ENDPOINTS.get(relation)
    if pairs is None:
        raise ValueError("unsupported structural graph relation")
    return pairs
