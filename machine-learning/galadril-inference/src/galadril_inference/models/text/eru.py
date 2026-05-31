"""Eru ESKG extraction model for unstructured text."""

from __future__ import annotations

import gc
from typing import Any

import structlog
from pydantic import create_model, Field

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

_MODEL_NAME = "eru_extractor"
_MODEL_VERSION = "1.0.0"


class EruExtractorModel(GaladrilBaseModel):
    """Agnostic relation extraction using the Eru 3-layer architecture."""

    def __init__(self) -> None:
        self._llm = None
        self._hf_model = None
        self._hf_tokenizer = None

    def meta(self) -> ModelMeta:
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description="Hybrid ESKG extraction (GLiNER + SLM).",
            tags={"domain": "nlp", "task": "relation_extraction"},
        )

    def load(self, artifact_path: str = "Qwen/Qwen2.5-0.5B-Instruct") -> None:
        """Load both GLiNER and the Reasoner SLM."""
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
            import outlines
            from gliner import GLiNER
            from eru.extractor.gliner import GlinerExtractor
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME, f"Missing dependencies: {exc}"
            ) from exc

        try:
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self._hf_model = AutoModelForCausalLM.from_pretrained(
                artifact_path, device_map=device
            )
            self._hf_tokenizer = AutoTokenizer.from_pretrained(artifact_path)
            self._llm = outlines.from_transformers(
                self._hf_model, self._hf_tokenizer
            )
            logger.info("eru_models_loaded", slm=artifact_path, device=device)
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def cleanup(self) -> None:
        """Release VRAM."""
        self._llm = None
        self._hf_model = None
        self._hf_tokenizer = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            gc.collect()

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Run the 3-layer Eru extraction pipeline dynamically."""
        self._ensure_loaded()

        from eru.engine import EskgEngine
        from eru.extractor.gliner import GlinerExtractor
        from eru.reasoner.outlines import OutlinesReasoner
        from eru.logic.simple import EskgLogicValidator
        from eru.types import RelationDef

        text = request.features.get("text")
        if not text:
            raise SchemaValidationError(
                _MODEL_NAME, ["Missing 'text' feature."]
            )

        labels = request.features.get(
            "entity_labels", ["PERSON", "ORGANIZATION", "LOCATION", "EVENT"]
        )
        open_types = request.features.get(
            "open_entity_types", ["INTENT", "CONCEPT"]
        )
        raw_relations = request.features.get("relation_defs", [])

        relation_defs = []
        relation_types = []
        for rel in raw_relations:
            r_def = RelationDef(**rel)
            relation_defs.append(r_def)
            relation_types.append(r_def.name)

        if not relation_types:
            relation_types = ["related_to"]

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

        try:
            extractor = GlinerExtractor(labels=labels, threshold=0.3)

            reasoner = OutlinesReasoner(
                model=self._llm,
                relation_defs=relation_defs,
                open_entity_types=open_types,
            )

            validator = EskgLogicValidator(
                get_entities=lambda g: g.entities,
                get_relations=lambda g: g.relations,
                entity_type_attr="type",
                relation_type_attr="relation_type",
                source_attr="source_id",
                target_attr="target_id",
            )

            engine = EskgEngine(
                schema=DynamicGraph,
                extractor=extractor,
                reasoner=reasoner,
                validator=validator,
            )

            result_graph = engine.process(text)
            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction={
                    "entities": [e.model_dump() for e in result_graph.entities],
                    "relations": [
                        r.model_dump() for r in result_graph.relations
                    ],
                },
                confidence=1.0,
            )

        except Exception as exc:
            raise RuntimeError(f"Eru extraction failed: {exc}") from exc

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["text", "entity_labels", "relation_defs"],
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
        return {
            "type": "object",
            "properties": {
                "entities": {"type": "array"},
                "relations": {"type": "array"},
            },
        }

    def _ensure_loaded(self) -> None:
        if self._llm is None:
            raise ModelLoadError(_MODEL_NAME, "Eru models are not loaded.")
