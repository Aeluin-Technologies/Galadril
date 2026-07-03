"""Compiles pipelines into execution chains via Dagster."""

from __future__ import annotations

import uuid
from typing import Any, cast
import dagster as dg
import structlog

from galadril_pipeline.compiler.resources import (
    NodeTelemetrySnapshot,
    StepRuntimeInput,
)
from galadril_pipeline.config import PipelineStep, Source
from galadril_pipeline.resources.kafka import KafkaResource
from galadril_pipeline.runtime.batch import BatchHandle
from galadril_pipeline.runtime.schemas import NodeStatus

logger = structlog.get_logger(__name__)


def _get_payload_size(payload: Any) -> int:
    """Safely estimates or extracts the record count from a batch payload."""
    if payload is None:
        return 0
    if hasattr(payload, "processed_records"):
        return cast(int, payload.processed_records)
    if isinstance(payload, (str, bytes)):
        return 1 if payload else 0
    if hasattr(payload, "__len__"):
        return len(payload)
    return 1


class AssetCompilerFactory:
    """Factory for constructing Dagster assets from pipeline specifications."""

    __slots__ = ()

    @staticmethod
    def build_source_asset(source: Source) -> dg.AssetsDefinition:
        """Creates a Dagster asset for Kafka source ingestion.

        Args:
            source: The source configuration containing topic and identification details.

        Returns:
            A Dagster AssetsDefinition representing the data ingestion node.
        """

        @dg.asset(
            name=source.id,
            group_name="sources",
            required_resource_keys={"kafka"},
            metadata={
                "topic": source.topic,
                "telemetry_layer": "kafka_ingress",
            },
        )
        async def source_node(
            context: dg.AssetExecutionContext,
        ) -> BatchHandle[Any]:
            kafka: KafkaResource = context.resources.kafka
            batch = await kafka.poll_batch()

            if not batch:
                return BatchHandle(
                    correlation_id="noop",
                    payload=[],
                    kafka_offsets={},
                )

            batch_handle = BatchHandle(
                correlation_id=str(uuid.uuid4()),
                payload=batch,
                kafka_offsets={},
            )

            context.add_output_metadata(
                metadata={
                    "correlation_id": batch_handle.correlation_id,
                    "record_count": _get_payload_size(batch_handle.payload),
                }
            )
            return batch_handle

        return source_node

    @staticmethod
    def build_pipeline_asset(
        step: PipelineStep, topological_index: int
    ) -> dg.AssetsDefinition:
        """Creates a Dagster asset for pipeline step execution.

        Args:
            step: The configuration for the specific processing step.
            topological_index: The index of the step within the pipeline's execution order.

        Returns:
            A Dagster AssetsDefinition representing the processing node.
        """
        ins = {dep: dg.AssetIn(key=dg.AssetKey(dep)) for dep in step.input_from}

        # Wire up the Dagster retry policy using the real fields defined in StepParams configuration
        retry_config = getattr(step.params, "retry_policy", None)
        dagster_retry_policy = (
            dg.RetryPolicy(
                max_retries=retry_config.max_retries,
                delay=retry_config.delay_seconds,
            )
            if retry_config and retry_config.max_retries > 0
            else None
        )

        @dg.asset(
            name=step.step,
            ins=ins,
            required_resource_keys={"pipeline_executor"},
            retry_policy=dagster_retry_policy,
            metadata={
                "step_type": step.type.value,
                "topological_index": topological_index,
            },
        )
        async def compute_node(
            context: dg.AssetExecutionContext,
            **upstream_assets: BatchHandle[Any],
        ) -> BatchHandle[Any]:
            input_batch: BatchHandle[Any] | None = None
            for handle in upstream_assets.values():
                if (
                    isinstance(handle, BatchHandle)
                    and _get_payload_size(handle.payload) > 0
                ):
                    input_batch = handle
                    break

            if not input_batch or _get_payload_size(input_batch.payload) == 0:
                return input_batch or BatchHandle(
                    correlation_id="noop",
                    payload=[],
                    kafka_offsets={},
                )

            executor = context.resources.pipeline_executor

            runtime_input = StepRuntimeInput(
                correlation_id=input_batch.correlation_id,
                step_name=step.step,
                step_type=step.type.value,
                batch=input_batch,
                params=step.params.model_dump()
                if hasattr(step.params, "model_dump")
                else {},
                upstream_states=[
                    NodeTelemetrySnapshot(
                        node_id=step.step,
                        status=NodeStatus.RUNNING,
                        records_mutated=_get_payload_size(input_batch.payload),
                    )
                ],
            )

            runtime_output = await executor.execute_step(runtime_input)

            context.add_output_metadata(
                metadata={
                    "correlation_id": input_batch.correlation_id,
                    "records_processed": runtime_output.records_processed,
                    "latency": dg.MetadataValue.float(
                        runtime_output.latency_seconds
                    ),
                }
            )

            if runtime_output.status == NodeStatus.FAILED:
                raise RuntimeError(
                    f"Step failed inside backend computing matrix: {runtime_output.error_details}"
                )

            return runtime_output.batch

        return compute_node
