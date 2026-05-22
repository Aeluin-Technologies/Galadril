"""Kafka payload validation and normalization for galadril-vision."""

from __future__ import annotations

from typing import Any

import structlog
from pydantic import ValidationError

from galadril_vision.common.schemas import (
    CanonicalRecord,
    SchemaViolation,
    ValidatedBatch,
)
from galadril_vision.connectors.kafka.schemas import EventNormalizer

logger = structlog.get_logger(__name__)


def validate_and_normalize_kafka_batch(
    batch: list[tuple[str, dict[str, Any]]],
) -> ValidatedBatch:
    accepted: list[CanonicalRecord] = []
    rejected: list[SchemaViolation] = []

    for topic, payload in batch:
        if not isinstance(payload, dict):
            rejected.append(
                SchemaViolation(
                    reason="payload_not_dict",
                    topic=topic,
                    raw={"payload": str(payload)},
                )
            )
            continue

        try:
            normalized = EventNormalizer.normalize(payload)
            rec = CanonicalRecord.model_validate(normalized)
            accepted.append(rec)
        except ValidationError as exc:
            rejected.append(
                SchemaViolation(
                    reason="pydantic_validation_error",
                    record_id=str(payload.get("id"))
                    if isinstance(payload.get("id"), str)
                    else None,
                    topic=topic,
                    raw=payload,
                )
            )
            logger.warning(
                "kafka_message_rejected", topic=topic, error=str(exc)
            )
        except Exception as exc:
            rejected.append(
                SchemaViolation(
                    reason="normalization_failed",
                    record_id=str(payload.get("id"))
                    if isinstance(payload.get("id"), str)
                    else None,
                    topic=topic,
                    raw=payload,
                )
            )
            logger.error(
                "kafka_normalization_failed", topic=topic, error=str(exc)
            )

    return ValidatedBatch(accepted=accepted, rejected=rejected)
