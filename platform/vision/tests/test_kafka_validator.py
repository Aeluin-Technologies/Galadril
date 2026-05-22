from galadril_vision.connectors.kafka.validator import (  # type: ignore
    validate_and_normalize_kafka_batch,
)


def test_validate_and_normalize_accepts_valid_payload() -> None:
    batch = [
        (
            "raw",
            {
                "id": "abc",
                "timestamp": 1_700_000_000_000,
                "ingested_at": 1_700_000_000_001,
                "source": "sensor",
                "storage_path": "s3://x",
            },
        )
    ]
    out = validate_and_normalize_kafka_batch(batch)
    assert len(out.accepted) == 1
    assert len(out.rejected) == 0
    assert out.accepted[0].record_id == "abc"


def test_validate_and_normalize_rejects_non_dict() -> None:
    batch = [("raw", {"id": "abc"}), ("raw", "nope")]
    out = validate_and_normalize_kafka_batch(batch)
    assert len(out.accepted) == 1
    assert len(out.rejected) == 1
