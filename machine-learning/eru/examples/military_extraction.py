"""Demonstrate the Eru pipeline on a short military text."""

import json
import logging
import sys
from typing import Literal, get_args

import structlog
from eru.common.types import RelationDef
from eru.engine import EruEngine
from eru.extractor.entity_merger import DefaultEntityMerger
from eru.extractor.gliner import GlinerExtractor
from eru.llm.llamacpp import LlamaCppConfig, LlamaCppJsonModel
from eru.llm.reference_resolver import OutlinesReferenceResolver
from eru.llm.semantic_normalizer import OutlinesSemanticNormalizer
from eru.logic.simple import ConstraintValidator
from eru.reasoner.outlines import OutlinesReasoner
from eru.reasoner.relation_candidates import DefaultRelationCandidateGenerator
from eru.schema import GraphSchema, RelationConstraint
from huggingface_hub import hf_hub_download, snapshot_download
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

labels = Literal[
    "WEAPON",
    "FACILITY",
    "DATE",
    "PERSON",
    "ORGANIZATION",
    "INTENT",
    "VEHICLE",
    "LOCATION",
    "EVENT",
    "QUANTITY",
    "TEMPORAL_SPAN",
]


class MilitaryEntity(BaseModel):
    """An extracted entity from the military domain."""

    id: str = Field(description="Unique identifier for the entity.")
    text: str = Field(description="The exact text span from the prompt.")
    type: labels


class MilitaryRelation(BaseModel):
    """A relation connecting two military entities."""

    source_id: str
    target_id: str
    relation_type: Literal[
        "triggers",
        "leads_to",
        "occurs_at",
        "target",
        "authorized_by",
        "employs",
        "located_in",
        "aims_to",
        "has_duration",
        "has_intent",
        "has_volume",
        "has_duration",
    ]


class MilitaryGraph(BaseModel):
    """The full ESKG graph output."""

    entities: list[MilitaryEntity]
    relations: list[MilitaryRelation]


relation_definitions = [
    RelationDef(
        name="authorized_by",
        description="Legal or command hierarchy link between an operation and a decision-maker.",
        examples=[
            "A mission approved by a General",
            "A strike ordered by the High Command",
        ],
    ),
    RelationDef(
        name="employs",
        description="The use of a specific asset, weapon, or unit during an event.",
        examples=[
            "A task force using radar systems",
            "An infantry squad utilizing night vision",
        ],
    ),
    RelationDef(
        name="target",
        description="The specific objective, facility, or enemy force being engaged.",
        examples=[
            "Artillery hitting a bridge",
            "Sabotage directed at a fuel depot",
        ],
    ),
    RelationDef(
        name="occurs_at",
        description="Temporal or geographical anchoring of an action.",
        examples=[
            "Tactical movement at 2300 hours",
            "Clash occurring in the DMZ",
        ],
    ),
    RelationDef(
        name="aims_to",
        description="Connects an EVENT to a STRATEGIC_OBJECTIVE. It describes the intended military effect.",
        examples=[
            "Operation X aims to neutralize air defenses",
            "The strike aims to disrupt supply lines",
        ],
    ),
    RelationDef(
        name="has_duration",
        description="Links an EVENT to a DATE span or duration.",
        examples=["The drill lasted 4 hours", "Phase 1 occurs within 24 hours"],
    ),
    RelationDef(
        name="located_in",
        description="Physical containment of a facility or unit within a broader area.",
        examples=[
            "A bunker inside the mountain range",
            "A fleet stationed in the Mediterranean",
        ],
    ),
    RelationDef(
        name="has_intent",
        description="Links a PERSON or ORGANIZATION or ACTOR or VEHICULE or WEAPON to an implicit INTENT or purpose.",
    ),
    RelationDef(
        name="has_volume",
        description="Links a military action or EVENT to a specific number, count, or quantity representing its scale.",
        examples=[
            "1,000 target engagements",
            "scaling beyond 1,250 strikes",
        ],
    ),
    RelationDef(
        name="has_duration",
        description="Links an EVENT to a specific duration, phase timeframe, or temporal span.",
        examples=["within the first 24-48 hours", "The drill lasted 4 hours"],
    ),
]

relation_constraints = [
    RelationConstraint(
        relation="authorized_by",
        allowed_source={"EVENT"},
        allowed_target={"PERSON", "ORGANIZATION"},
    ),
    RelationConstraint(
        relation="employs",
        allowed_source={"EVENT", "ORGANIZATION"},
        allowed_target={"WEAPON", "VEHICLE"},
    ),
    RelationConstraint(
        relation="target",
        allowed_source={"EVENT", "WEAPON"},
        allowed_target={"FACILITY", "LOCATION", "ORGANIZATION"},
    ),
    RelationConstraint(
        relation="occurs_at",
        allowed_source={"EVENT"},
        allowed_target={"DATE", "LOCATION"},
    ),
    RelationConstraint(
        relation="aims_to", allowed_source={"EVENT"}, allowed_target={"INTENT"}
    ),
    RelationConstraint(
        relation="has_duration",
        allowed_source={"EVENT"},
        allowed_target={"DATE", "METRIC_VALUE"},
    ),
    RelationConstraint(
        relation="located_in",
        allowed_source={"FACILITY", "LOCATION", "ORGANIZATION"},
        allowed_target={"LOCATION"},
    ),
    RelationConstraint(
        relation="has_intent",
        allowed_source={"PERSON", "ORGANIZATION", "VEHICLE", "WEAPON"},
        allowed_target={"INTENT"},
    ),
    RelationConstraint(
        relation="has_volume",
        allowed_source={"EVENT"},
        allowed_target={"QUANTITY"},
    ),
    RelationConstraint(
        relation="has_duration",
        allowed_source={"EVENT"},
        allowed_target={"TEMPORAL_SPAN"},
    ),
]


def main() -> None:
    logger.info("downloading_or_resolving_model_artifacts")

    gliner2_model_path = snapshot_download(
        repo_id="knowledgator/gliner-bi-base-v2.0"
    )
    gguf_model_path = hf_hub_download(
        repo_id="bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF",
        filename="Qwen_Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
    )

    logger.info(
        "model_artifacts_cached_locally",
        gliner_path=gliner2_model_path,
        gguf_path=gguf_model_path,
    )

    text = (
        "Operation EPIC FURY commenced at 0115Z, 28 FEB 26, under directive authority of the U.S. President, "
        "executed by CENTCOM joint task elements against high-value targets within Iranian territory. "
        "Strike packages composed of B-2 stealth bombers, F-35 multirole fighters, and naval surface combatants "
        "(CVN and DDG groups) delivered precision-guided munitions and TLAM salvos against IRGC C2 nodes, "
        "integrated air defense systems, and ballistic missile complexes. Initial operational tempo exceeded "
        "1,000 target engagements within the first 24–48 hours, scaling beyond 1,250 strikes to degrade "
        "adversary warfighting capability and deny strategic missile deployment vectors. Command oversight "
        "included senior defense leadership (e.g., SecDef-level authority), coordinating multi-domain "
        "operations (air, land, maritime, cyber) across the CENTCOM AOR, including key nodes such as "
        "Kharg Island and dispersed missile launch infrastructure."
    )

    schema = GraphSchema(
        entity_model=MilitaryEntity,
        relation_model=MilitaryRelation,
        graph_model=MilitaryGraph,
        relation_defs=relation_definitions,
        constraints=relation_constraints,
        implicit_entity_types=["INTENT"],
    )

    logger.info("initializing_pipeline_components")

    config = LlamaCppConfig(model_path=gguf_model_path)
    llm = LlamaCppJsonModel.from_config(config)

    extractor = GlinerExtractor(
        labels=list(get_args(labels)),
        model_path=gliner2_model_path,
    )

    coreference = OutlinesReferenceResolver(model=llm)
    normalizer = OutlinesSemanticNormalizer(model=llm)
    entity_merger = DefaultEntityMerger()

    relation_candidates = DefaultRelationCandidateGenerator(
        schema=schema, allow_self_loops=False
    )

    reasoner = OutlinesReasoner(model=llm, max_new_tokens=256)

    validator = ConstraintValidator(
        constraints=relation_constraints,
        get_entities=lambda g: g.entities,
        get_relations=lambda g: g.relations,
        entity_id="id",
        entity_type="type",
        relation_type="relation_type",
        source="source_id",
        target="target_id",
    )

    engine = EruEngine(
        schema=schema,
        extractor=extractor,
        coreference=coreference,
        normalizer=normalizer,
        entity_merger=entity_merger,
        relation_candidates=relation_candidates,
        reasoner=reasoner,
        validator=validator,
    )

    logger.info("submitting_payload_to_eru_engine")
    result_graph = engine.process(text)
    logger.info("payload_processing_completed_successfully")

    print(json.dumps(result_graph.model_dump(), indent=2))


if __name__ == "__main__":
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )

    main()
