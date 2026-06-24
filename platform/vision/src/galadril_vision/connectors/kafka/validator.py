"""Kafka payload validation and normalization for galadril-vision."""

from __future__ import annotations

import structlog
from pydantic import ValidationError

from galadril_vision.common.schemas import (
    CanonicalRecord,
    SchemaViolation,
    ValidatedBatch,
)
from galadril_vision.connectors.kafka.consumer import IngestedMessage
from galadril_vision.connectors.kafka.schemas import EventNormalizer

logger = structlog.get_logger(__name__)


def validate_and_normalize_kafka_batch(
    batch: list[IngestedMessage],
) -> ValidatedBatch:
    accepted: list[CanonicalRecord] = []
    rejected: list[SchemaViolation] = []

    for msg in batch:
        record_id = (
            str(msg.payload.get("id"))
            if isinstance(msg.payload, dict)
            and isinstance(msg.payload.get("id"), str)
            else None
        )

        if not isinstance(msg.payload, dict):
            rejected.append(
                SchemaViolation(
                    reason="payload_not_dict",
                    topic=msg.topic,
                    raw={"payload": str(msg.payload)},
                )
            )
            logger.warning(
                "kafka_message_invalid_structure",
                topic=msg.topic,
                reason="payload_not_dict",
            )
            continue

        try:
            normalized = EventNormalizer.normalize(msg.payload, msg.event_type)
            rec = CanonicalRecord.model_validate(normalized)
            accepted.append(rec)

            logger.debug(
                "kafka_message_normalized_successfully",
                topic=msg.topic,
                record_id=record_id,
                resolved_event_type=msg.event_type,
            )
        except ValidationError as exc:
            rejected.append(
                SchemaViolation(
                    reason="pydantic_validation_error",
                    record_id=record_id,
                    topic=msg.topic,
                    raw=msg.payload,
                )
            )
            logger.warning(
                "kafka_message_rejected",
                topic=msg.topic,
                record_id=record_id,
                error=str(exc),
            )
        except Exception as exc:
            rejected.append(
                SchemaViolation(
                    reason="normalization_failed",
                    record_id=record_id,
                    topic=msg.topic,
                    raw=msg.payload,
                )
            )
            logger.error(
                "kafka_normalization_failed",
                topic=msg.topic,
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
