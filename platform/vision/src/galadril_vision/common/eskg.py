"""Structural instance-graph vocabulary, independent of Registry definitions."""

from __future__ import annotations

from enum import StrEnum, unique

from galadril_ontology import GolSymbolKind, active_gol_lease
from galadril_ontology.identity import normalize_tenant_id, validate_symbol_key
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


ONTOLOGY_KINDS: dict[GraphNodeKind, GolSymbolKind] = {
    GraphNodeKind.ENTITY: GolSymbolKind.ENTITY,
    GraphNodeKind.EVENT: GolSymbolKind.ENTITY,
    GraphNodeKind.STATE: GolSymbolKind.FIELD,
    GraphNodeKind.LATENT_IDENTITY: GolSymbolKind.ENTITY,
    GraphNodeKind.CAUSAL_VARIABLE: GolSymbolKind.FIELD,
}


class GolReference(BaseModel):
    """Pins classification without embedding an ontology definition in AGE."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    tenant_id: str
    ontology_id: str = Field(min_length=1, max_length=128)
    ontology_version: str = Field(min_length=1, max_length=128)
    source_revision: str = Field(min_length=1, max_length=128)
    generation_id: str = Field(min_length=1, max_length=128)
    symbol_key: str
    symbol_kind: GolSymbolKind

    @field_validator("tenant_id")
    @classmethod
    def _tenant(cls, value: str) -> str:
        return normalize_tenant_id(value)

    @field_validator("symbol_key")
    @classmethod
    def _resource(cls, value: str) -> str:
        return validate_symbol_key(value)


def resolve_gol_reference(
    resource_id: object, tenant_id: str, kind: GraphNodeKind
) -> GolReference | None:
    """Derives provenance exclusively from the active, validated GOL calculation."""
    if resource_id is None:
        return None
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError("GOL symbol identifier is required")
    active = active_gol_lease()
    if active is None:
        raise ValueError("classification requires an active GOL calculation")
    if active.tenant_id != normalize_tenant_id(tenant_id):
        raise ValueError("classification crosses the active ontology tenant")
    resource = active.symbol(resource_id)
    if resource is None:
        raise ValueError("ontology classification resource is unavailable")
    if ONTOLOGY_KINDS.get(kind) != resource.kind:
        raise ValueError("GOL symbol kind is incompatible with graph kind")
    return GolReference(
        tenant_id=active.tenant_id,
        ontology_id=resource.key.partition("::")[0],
        ontology_version=resource.version,
        source_revision=active.source_revision,
        generation_id=active.generation_id,
        symbol_key=resource.key,
        symbol_kind=resource.kind,
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
