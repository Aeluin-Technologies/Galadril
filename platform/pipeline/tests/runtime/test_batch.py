"""Unit tests for runtime model validation constraints."""

from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult


def test_batch_handle_and_result_defaults() -> None:
    """Guarantees models maintain structural default properties securely."""
    batch = BatchHandle(correlation_id="abc", payload={"items": []})
    assert batch.kafka_offsets == {}
    assert batch.started_at > 0
    assert batch.finished_at is None

    result = PipelineResult(processed_records=10, duration=1.23)
    assert result.processed_records == 10
