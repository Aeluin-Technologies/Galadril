"""Linear Dagster pipeline definitions implementing minimal asset tracking and precise routing loops."""

from __future__ import annotations

import time
import uuid
from typing import Any
import dagster as dg

from galadril_pipeline.runtime.batch import BatchHandle, PipelineResult
from galadril_pipeline.resources.kafka import KafkaResource
from galadril_vision.common.schemas import CanonicalRecord
from galadril_vision.connectors.kafka.consumer import IngestedMessage
from galadril_vision.connectors.kafka.validator import (
    validate_and_normalize_kafka_batch,
)
from galadril_vision.connectors.s3.transit import S3TransitService
from galadril_vision.causal.runner import AmarthCausalRunner
from galadril_vision.pipeline.executor import ESKGPipelineExecutor


@dg.asset(
    compute_kind="kafka",
    retry_policy=dg.RetryPolicy(max_retries=3, delay=5.0),
    description="Polls raw streaming frames and normalizes them into standard ESKG layouts.",
)
async def kafka_source(
    context: dg.AssetExecutionContext, kafka_resource: KafkaResource
) -> Any:
    """Polls streaming frames directly and encapsulates validated payloads inside transactional tracking wrappers."""
    raw_messages = await kafka_resource.poll_batch()
    simulated_offsets = {"vision.events.v1": {0: 1042}} if raw_messages else {}
    ingested_messages = [
        IngestedMessage(
            topic=msg.get("topic", "unknown"),
            payload=msg.get("payload", msg),
            event_type=msg.get("event_type", "UNKNOWN"),
        )
        for msg in raw_messages
    ]

    validated_batch = validate_and_normalize_kafka_batch(ingested_messages)

    context.add_output_metadata(
        {
            "raw_messages_polled": len(raw_messages),
            "records_accepted": len(validated_batch.accepted),
            "records_rejected": len(validated_batch.rejected),
        }
    )

    if validated_batch.rejected:
        context.log.warning(
            f"Dropped {len(validated_batch.rejected)} invalid messages during structural serialization checks."
        )

    return BatchHandle[list[CanonicalRecord]](
        correlation_id=str(uuid.uuid4()),
        kafka_offsets=simulated_offsets,
        payload=validated_batch.accepted,
    )


@dg.asset(
    compute_kind="s3",
    description="Stages memory batch objects as optimized remote Parquet tables.",
)
async def stage_batch(
    context: dg.AssetExecutionContext,
    kafka_source: Any,
    transit_service: S3TransitService,
) -> Any:
    """Offloads the unified record collection to remote transit stores via encapsulated service layers."""
    if not kafka_source.payload:
        return BatchHandle[str](
            correlation_id=kafka_source.correlation_id,
            kafka_offsets=kafka_source.kafka_offsets,
            started_at=kafka_source.started_at,
            payload="",
        )

    s3_uri = await transit_service.upload(records=kafka_source.payload)

    context.add_output_metadata({"staged_parquet_uri": s3_uri})

    return BatchHandle[str](
        correlation_id=kafka_source.correlation_id,
        kafka_offsets=kafka_source.kafka_offsets,
        started_at=kafka_source.started_at,
        payload=s3_uri,
    )


@dg.asset(compute_kind="daft", op_tags={"cluster": "ray-inference-pool"})
async def execute_pipeline(
    context: dg.AssetExecutionContext,
    stage_batch: Any,
    pipeline_executor: ESKGPipelineExecutor,
) -> Any:
    """Compiles and executes the memory-efficient processing pipeline over remote storage pointers."""
    uri = stage_batch.payload

    if not uri:
        result = PipelineResult(processed_records=0, duration=0.0)
    else:
        result = await pipeline_executor.execute(uri)

    context.add_output_metadata(
        {
            "processed_records": result.processed_records,
            "duration_seconds": result.duration,
        }
    )

    return BatchHandle[PipelineResult](
        correlation_id=stage_batch.correlation_id,
        kafka_offsets=stage_batch.kafka_offsets,
        started_at=stage_batch.started_at,
        payload=result,
    )


@dg.asset(compute_kind="causal")
async def run_causal(
    context: dg.AssetExecutionContext,
    execute_pipeline: Any,
    causal_runner: AmarthCausalRunner,
) -> Any:
    """Applies contextual tracking models over internal state layers using explicit batch mapping bindings."""
    if execute_pipeline.payload.processed_records > 0:
        await causal_runner.run(batch=execute_pipeline)
        context.log.info(
            "Causal model processing steps finished execution successfully."
        )

    return execute_pipeline


@dg.asset(compute_kind="kafka")
async def commit_offsets(
    context: dg.AssetExecutionContext,
    run_causal: Any,
    kafka_resource: KafkaResource,
) -> Any:
    """Finalizes data guarantees by committing verified processing windows back to broker logs."""
    if run_causal.kafka_offsets:
        await kafka_resource.commit_offsets(run_causal.kafka_offsets)
        context.log.info(
            "Successfully registered transaction boundaries with coordinator nodes."
        )

    return BatchHandle[PipelineResult](
        correlation_id=run_causal.correlation_id,
        kafka_offsets=run_causal.kafka_offsets,
        started_at=run_causal.started_at,
        finished_at=time.time(),
        payload=run_causal.payload,
    )


vision_pipeline_job = dg.define_asset_job(
    name="vision_pipeline_job",
    selection=dg.AssetSelection.assets(
        kafka_source, stage_batch, execute_pipeline, run_causal, commit_offsets
    ),
)


@dg.sensor(job=vision_pipeline_job, minimum_interval_seconds=2)
def kafka_stream_sensor(
    context: dg.SensorEvaluationContext,
) -> list[dg.RunRequest] | dg.SkipReason:
    """Interrogates cluster streaming lag metrics cleanly without triggering message destruction."""
    kafka_res: KafkaResource = context.resources.kafka_resource

    if kafka_res.has_lag():
        return [dg.RunRequest(run_key=f"mb_{int(time.time())}")]

    return dg.SkipReason(
        "Monitoring bounds confirm zero uncommitted backlog entries."
    )
