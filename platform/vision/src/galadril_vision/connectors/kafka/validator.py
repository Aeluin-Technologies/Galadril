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
    batch: list[tuple[str, dict[str, Any], str]],
) -> ValidatedBatch:
    accepted: list[CanonicalRecord] = []
    rejected: list[SchemaViolation] = []

    for topic, payload, resolved_event_type in batch:
        record_id = (
            str(payload.get("id"))
            if isinstance(payload, dict) and isinstance(payload.get("id"), str)
            else None
        )

        if not isinstance(payload, dict):
            rejected.append(
                SchemaViolation(
                    reason="payload_not_dict",
                    topic=topic,
                    raw={"payload": str(payload)},
                )
            )
            logger.warning(
                "kafka_message_invalid_structure",
                topic=topic,
                reason="payload_not_dict",
            )
            continue

        try:
            normalized = EventNormalizer.normalize(payload, resolved_event_type)
            rec = CanonicalRecord.model_validate(normalized)
            accepted.append(rec)

            logger.debug(
                "kafka_message_normalized_successfully",
                topic=topic,
                record_id=record_id,
                resolved_event_type=resolved_event_type,
            )
        except ValidationError as exc:
            rejected.append(
                SchemaViolation(
                    reason="pydantic_validation_error",
                    record_id=record_id,
                    topic=topic,
                    raw=payload,
                )
            )
            logger.warning(
                "kafka_message_rejected",
                topic=topic,
                record_id=record_id,
                error=str(exc),
            )
        except Exception as exc:
            rejected.append(
                SchemaViolation(
                    reason="normalization_failed",
                    record_id=record_id,
                    topic=topic,
                    raw=payload,
                )
            )
            logger.error(
                "kafka_normalization_failed",
                topic=topic,
                record_id=record_id,
                error=str(exc),
            )

    logger.info(
        "kafka_batch_validation_completed",
        processed=len(batch),
        accepted=len(accepted),
        rejected=len(rejected),
    )

    return ValidatedBatch(accepted=accepted, rejected=rejected)
