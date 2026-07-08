"""Eru ESKG extraction model for unstructured text."""

from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import structlog
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
from eru.types import RelationDef
from pydantic import Field, create_model

from galadril_inference.common.exceptions import (
    ModelLoadError,
    SchemaValidationError,
)
from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.models.base import BaseModel as GaladrilBaseModel

logger = structlog.get_logger(__name__)

_MODEL_NAME = "eru"
_MODEL_VERSION = "1.0.0"


class EruExtractorModel(GaladrilBaseModel):
    """Agnostic relation extraction using the Eru 3-layer architecture."""

    def __init__(self) -> None:
        self._llm: LlamaCppJsonModel | None = None
        self._gliner2_path: Path | None = None

    def meta(self) -> ModelMeta:
        """Returns model metadata consumed by galadril-inference."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Hybrid ESKG extraction (GLiNER2 + GGUF SLM).",
            tags={
                "domain": "nlp",
                "task": "relation_extraction",
                "backend": "eru",
            },
        )

    def download(self, target_path: str) -> None:
        """Prepare the artifact root; upstream fetching is handled externally."""
        root = Path(target_path)
        root.mkdir(parents=True, exist_ok=True)
        (root / "llm").mkdir(exist_ok=True)
        (root / "gliner2").mkdir(exist_ok=True)

    def load(self, artifact_path: str) -> None:
        """Load GGUF SLM via LlamaCppJsonModel and track GLiNER2 paths."""
        root = Path(artifact_path)
        try:
            llm_path = self._resolve_llm_path(root)
            self._gliner2_path = self._resolve_gliner2_path(root)

            config = LlamaCppConfig(
                model_path=str(llm_path),
                n_ctx=4096,
                n_gpu_layers=-1,
                temperature=0.0,
            )

            # Instantiate using the correct class factory method
            self._llm = LlamaCppJsonModel.from_config(config)

            logger.info(
                "model_loaded",
                slm_artifact=str(llm_path),
                gliner2_artifact=str(self._gliner2_path),
                model_name=_MODEL_NAME,
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release loaded model references and run deterministic cleanup."""
        self._llm = None
        self._gliner2_path = None
        gc.collect()
        logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run the Eru extraction pipeline using loaded external artifacts."""
        self._ensure_loaded()
        assert self._gliner2_path is not None

        text = self._require_text(request)
        labels = self._string_list_feature(
            request,
            "entity_labels",
            ["PERSON", "ORGANIZATION", "LOCATION", "EVENT"],
        )
        open_types = self._string_list_feature(
            request,
            "open_entity_types",
            ["INTENT", "CONCEPT"],
        )
        relation_defs, constraints = self._relation_config(
            request.features.get("relation_defs", []),
            labels,
            open_types,
        )

        DynamicEntity = create_model(
            "DynamicEntity",
            id=(str, Field(..., description="Unique identifier")),
            text=(str, Field(..., description="Exact text span")),
            type=(str, Field(..., description="Entity type")),
        )
        DynamicRelation = create_model(
            "DynamicRelation",
            source_id=(str, ...),
            target_id=(str, ...),
            relation_type=(str, ...),
        )
        DynamicGraph = create_model(
            "DynamicGraph",
            entities=(list[DynamicEntity], ...),
            relations=(list[DynamicRelation], ...),
        )
        schema = GraphSchema(
            entity_model=DynamicEntity,
            relation_model=DynamicRelation,
            graph_model=DynamicGraph,
            relation_defs=relation_defs,
            constraints=constraints,
            implicit_entity_types=open_types,
        )

        try:
            # Instantiate GlinerExtractor directly with the requested lifecycle labels
            extractor = GlinerExtractor(
                model_path=str(self._gliner2_path), labels=labels
            )

            # LlamaCppJsonModel instance acts directly as the required Outlines callable backend
            coreference = OutlinesReferenceResolver(model=self._llm)
            normalizer = OutlinesSemanticNormalizer(model=self._llm)
            relation_candidates = DefaultRelationCandidateGenerator(
                schema=schema
            )
            reasoner = OutlinesReasoner(model=self._llm, max_new_tokens=256)
            validator = ConstraintValidator(
                constraints=constraints,
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
                entity_merger=DefaultEntityMerger(),
                relation_candidates=relation_candidates,
                # Explicit type ignore bypasses strict variance check on BaseModel vs TGraph
                reasoner=reasoner,  # type: ignore[argument-type]
                validator=validator,
            )
            result_graph = engine.process(text)
        except Exception as exc:
            raise RuntimeError(f"Eru extraction failed: {exc}") from exc

        return PredictionResult(
            model_name=_MODEL_NAME,
            model_version=_MODEL_VERSION,
            prediction={
                "entities": [e.model_dump() for e in result_graph.entities],
                "relations": [r.model_dump() for r in result_graph.relations],
            },
            confidence=1.0,
        )

    def input_schema(self) -> dict[str, Any]:
        """Returns the JSON input schema for extraction requests."""
        return {
            "type": "object",
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "entity_labels": {"type": "array", "items": {"type": "string"}},
                "open_entity_types": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "relation_defs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name", "description"],
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "allowed_sources": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "allowed_targets": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "examples": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        }

    def output_schema(self) -> dict[str, Any]:
        """Returns the JSON output schema for extraction responses."""
        return {
            "type": "object",
            "properties": {
                "entities": {"type": "array"},
                "relations": {"type": "array"},
            },
        }

    def _ensure_loaded(self) -> None:
        """Raises if required artifacts have not been loaded."""
        if self._llm is None or self._gliner2_path is None:
            raise ModelLoadError(_MODEL_NAME, "Eru models are not loaded.")

    def _require_text(self, request: PredictionRequest) -> str:
        """Validates and returns the mandatory text feature."""
        text = request.features.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SchemaValidationError(
                _MODEL_NAME, ["Missing 'text' feature."]
            )
        return text

    def _string_list_feature(
        self,
        request: PredictionRequest,
        key: str,
        default: list[str],
    ) -> list[str]:
        """Reads a string-list feature with defensive validation."""
        value = request.features.get(key, default)
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise SchemaValidationError(
                _MODEL_NAME, [f"Invalid '{key}' feature."]
            )
        return value

    def _relation_config(
        self,
        raw_relations: Any,
        labels: list[str],
        open_types: list[str],
    ) -> tuple[list[RelationDef], list[RelationConstraint]]:
        """Builds relation definitions and type constraints from request data."""
        if not raw_relations:
            all_types = set(labels) | set(open_types)
            return (
                [
                    RelationDef(
                        name="related_to",
                        description="Generic relation between two extracted entities.",
                    )
                ],
                [
                    RelationConstraint(
                        relation="related_to",
                        allowed_source=all_types,
                        allowed_target=all_types,
                    )
                ],
            )

        if not isinstance(raw_relations, list):
            raise SchemaValidationError(
                _MODEL_NAME, ["Invalid 'relation_defs'."]
            )

        relation_defs: list[RelationDef] = []
        constraints: list[RelationConstraint] = []
        fallback_types = set(labels) | set(open_types)

        for raw in raw_relations:
            if not isinstance(raw, dict):
                raise SchemaValidationError(
                    _MODEL_NAME, ["Invalid relation entry."]
                )

            relation = RelationDef(
                name=str(raw.get("name", "")),
                description=str(raw.get("description", "")),
                examples=list(raw.get("examples", [])),
            )
            if not relation.name or not relation.description:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["Relation entries require 'name' and 'description'."],
                )

            allowed_sources = self._relation_types(
                raw.get("allowed_sources"),
                fallback_types,
                "allowed_sources",
            )
            allowed_targets = self._relation_types(
                raw.get("allowed_targets"),
                fallback_types,
                "allowed_targets",
            )
            relation_defs.append(relation)
            constraints.append(
                RelationConstraint(
                    relation=relation.name,
                    allowed_source=allowed_sources,
                    allowed_target=allowed_targets,
                )
            )

        return relation_defs, constraints

    def _relation_types(
        self,
        value: Any,
        fallback: set[str],
        key: str,
    ) -> set[str]:
        """Validates relation endpoint type lists."""
        if value is None:
            return fallback
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise SchemaValidationError(
                _MODEL_NAME, [f"Invalid '{key}' values."]
            )
        return set(value)

    def _resolve_llm_path(self, root: Path) -> Path:
        """Resolves the GGUF artifact file from a Galadril artifact root."""
        if root.is_file() and root.suffix.lower() == ".gguf":
            return root

        llm_dir = root / "llm"
        search_root = llm_dir if llm_dir.is_dir() else root
        matches = sorted(search_root.glob("*.gguf"))
        if len(matches) != 1:
            raise ModelLoadError(
                _MODEL_NAME,
                f"Expected exactly one GGUF file under '{search_root}'.",
            )
        return matches[0]

    def _resolve_gliner2_path(self, root: Path) -> Path:
        """Resolves the GLiNER2 artifact directory from a Galadril artifact root."""
        if root.is_file():
            root = root.parent

        gliner2_dir = root / "gliner2"
        if gliner2_dir.is_dir():
            return gliner2_dir

        gliner_dir = root / "gliner"
        if gliner_dir.is_dir():
            return gliner_dir

        raise ModelLoadError(
            _MODEL_NAME,
            f"Expected a GLiNER2 artifact directory under '{root}'.",
        )
