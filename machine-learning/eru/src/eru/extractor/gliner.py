"""Entity extraction module utilizing user-supplied GLiNER artifacts."""

from __future__ import annotations

from os import PathLike
from typing import Any

import structlog

from eru.common.exceptions import ExtractionError, ModelResolutionError
from eru.common.types import EntityMention, ExtractedCandidate

logger = structlog.get_logger(__name__)


class GlinerExtractor:
    """Extracts entity mentions from raw text using a pretrained GLiNER model.

    Leverages pre-computed label embeddings to efficiently query and batch predict
    named entities within specified text blocks.

    Attributes:
        labels: List of classification strings the model should scan for.
        threshold: Minimum confidence score required to retain an extraction.
        model: Loaded GLiNER model instance.
        label_embeddings: Encoded text representation vectors of the target labels.
    """

    def __init__(
        self,
        labels: list[str],
        model: Any | None = None,
        model_path: str | PathLike[str] | None = None,
        threshold: float = 0.3,
        device: str = "cpu",
    ):
        """Initializes GLiNER from a caller-provided model or local artifact path."""
        self.threshold = threshold
        self.labels = labels

        log = logger.bind(
            threshold=threshold,
            device=device,
            label_count=len(labels),
        )
        log.info("initializing_gliner_extractor")

        try:
            if model is not None:
                log.debug("loading_provided_model_instance")
                self.model = model.to(device) if hasattr(model, "to") else model
            elif model_path is not None:
                log.info(
                    "loading_model_from_pretrained_path",
                    model_path=str(model_path),
                )
                from gliner import GLiNER

                self.model = GLiNER.from_pretrained(str(model_path)).to(device)
            else:
                raise ModelResolutionError(
                    "GLiNER requires a loaded model or an explicit local model_path."
                )

            log.debug("encoding_target_labels")
            self.label_embeddings = self.model.encode_labels(labels)
            log.info("gliner_extractor_initialized_successfully")

        except Exception as e:
            log.exception("gliner_extractor_initialization_failed")
            raise ExtractionError(
                f"Failed to initialize GLiNER model: {e}"
            ) from e

    def extract(self, text: str) -> list[ExtractedCandidate]:
        """Extracts candidate entity mentions from a provided raw string payload.

        Args:
            text: Raw document text context.

        Returns:
            A list of structural ExtractedCandidate tokens passing the threshold rule.

        Raises:
            ExtractionError: If prediction or model processing crashes.
        """
        text_length = len(text)
        if not text.strip():
            logger.debug(
                "gliner_extraction_skipped_empty_text", text_length=text_length
            )
            return []

        log = logger.bind(text_length=text_length)
        log.info("gliner_extraction_started")

        try:
            # Predict boundaries using precomputed embeddings (unwrap single-item batch)
            outputs = self.model.batch_predict_with_embeds(
                [text],
                self.label_embeddings,
                self.labels,
            )[0]

            log.debug(
                "gliner_raw_predictions_received",
                raw_predictions_count=len(outputs),
            )

            entities = []
            for entity in outputs:
                score = float(entity.get("score", 0.0))

                if score < self.threshold:
                    continue

                mention = EntityMention(
                    text=entity["text"],
                    start_char=entity["start"],
                    end_char=entity["end"],
                    score=score,
                )

                entities.append(
                    ExtractedCandidate(
                        text=mention.text,
                        labels=[entity["label"]],
                        mentions=[mention],
                        confidence=score,
                    )
                )

            log.info(
                "gliner_extraction_completed",
                total_extracted_candidates=len(entities),
                filtered_out_count=len(outputs) - len(entities),
            )
            return entities

        except Exception as e:
            log.exception("gliner_extraction_sequence_failed")
            raise ExtractionError(
                f"GLiNER extraction sequence failed: {e}"
            ) from e
