"""Dynamic asset factory."""

import asyncio
import json
import uuid
import dagster as dg

from galadril_pipeline.compiler.resources import (
    NodeStatus,
    NodeTelemetrySnapshot,
    StepRuntimeInput,
    StepRuntimeOutput,
)
from galadril_pipeline.config import PipelineStep, Source, TriggerType


class AssetCompilerFactory:
    """Compiles pipeline schemas into validated asynchronous software assets."""

    __slots__ = ()

    @staticmethod
    def build_source_asset(source: Source) -> dg.AssetsDefinition:
        """Compiles an ingestion source configuration into a tracked root asset."""

        @dg.asset(
            name=source.id,
            group_name="sources",
            metadata={
                "topic": source.topic,
                "schema_path": source.schema_path,
                "telemetry_layer": "kafka_ingress",
            },
        )
        async def source_node(context):
            correlation_id = str(uuid.uuid4())
            node_snapshot = NodeTelemetrySnapshot(
                node_id=source.id,
                status=NodeStatus.COMPLETED,
                records_mutated=0,
                storage_uri_pointers=[f"kafka://{source.topic}"],
            )
            runtime_payload = {
                "correlation_id": correlation_id,
                "snapshot": node_snapshot.model_dump(mode="python"),
            }

            yield dg.Output(
                value=runtime_payload,
                metadata={
                    "correlation_id": correlation_id,
                    "snapshot": dg.MetadataValue.json(
                        runtime_payload["snapshot"]
                    ),
                },
            )

        return source_node

    @staticmethod
    def build_pipeline_asset(
        step: PipelineStep, topological_index: int
    ) -> dg.AssetsDefinition:
        """Transforms a structural step specification into a non-blocking execution asset."""

        upstream_keys = [dg.AssetKey(dep) for dep in step.input_from]

        retry_policy = dg.RetryPolicy(
            max_retries=step.params.retry_policy.max_retries,
            delay=step.params.retry_policy.delay_seconds,
        )

        @dg.asset(
            name=step.step,
            deps=upstream_keys,
            retry_policy=retry_policy,
            required_resource_keys={"executor"},
            group_name="pipeline",
            metadata={
                "step_type": step.type.value,
                "topological_index": topological_index,
                "telemetry_layer": f"processing_{step.type.value}",
            },
        )
        async def compute_node(context):
            context.log.info(
                f"Asynchronously validating inputs for node: '{step.step}'."
            )

            executor = context.resources.executor

            resolved_upstream_states = []
            active_correlation_id = None

            for dep_id in step.input_from:
                dep_key = dg.AssetKey(dep_id)

                if context.instance:
                    latest_event = await asyncio.to_thread(
                        context.instance.get_latest_materialization_event,
                        dep_key,
                    )
                else:
                    latest_event = None

                if (
                    not latest_event
                    or not latest_event.asset_materialization
                    or not latest_event.asset_materialization.metadata
                ):
                    context.log.warning(
                        f"Upstream asset '{dep_id}' provided an empty execution context."
                    )
                    resolved_upstream_states.append(
                        NodeTelemetrySnapshot(
                            node_id=dep_id,
                            status=NodeStatus.FAILED,
                            records_mutated=0,
                        )
                    )
                    continue

                metadata_map = latest_event.asset_materialization.metadata
                raw_snapshot = metadata_map.get("snapshot")
                raw_correlation = metadata_map.get("correlation_id")

                if not raw_snapshot:
                    raise ValueError(
                        f"Contract violation: upstream payload from '{dep_id}' is malformed."
                    )

                if active_correlation_id is None and raw_correlation:
                    active_correlation_id = getattr(
                        raw_correlation, "value", str(raw_correlation)
                    )

                snapshot_data = (
                    raw_snapshot.data
                    if hasattr(raw_snapshot, "data")
                    else getattr(raw_snapshot, "value", raw_snapshot)
                )

                if isinstance(snapshot_data, str):
                    try:
                        snapshot_data = json.loads(snapshot_data)
                    except json.JSONDecodeError as json_err:
                        raise ValueError(
                            f"Contract violation: upstream payload from '{dep_id}' contains invalid JSON serialization."
                        ) from json_err

                resolved_upstream_states.append(
                    NodeTelemetrySnapshot.model_validate(snapshot_data)
                )

            correlation_id = active_correlation_id or str(uuid.uuid4())
            runtime_input = StepRuntimeInput(
                correlation_id=correlation_id,
                step_name=step.step,
                step_type=step.type,
                params=step.params.model_dump(mode="python"),
                upstream_states=resolved_upstream_states,
            )

            try:
                runtime_output = await executor.execute_step(runtime_input)
            except Exception as exc:
                raise RuntimeError(
                    f"Fatal unhandled process anomaly in platform engine during node execution: '{step.step}'."
                ) from exc

            if not isinstance(runtime_output, StepRuntimeOutput):
                raise TypeError(
                    "Contract breach: target engine failed to return a validated StepRuntimeOutput."
                )

            if runtime_output.status != NodeStatus.COMPLETED:
                raise RuntimeError(
                    f"Platform processing rejected by executor client. Context: {runtime_output.error_details}"
                )

            node_snapshot = NodeTelemetrySnapshot(
                node_id=step.step,
                status=NodeStatus.COMPLETED,
                records_mutated=runtime_output.records_processed,
                storage_uri_pointers=runtime_output.storage_uri_pointers,
            )

            output_payload = {
                "correlation_id": correlation_id,
                "snapshot": node_snapshot.model_dump(mode="python"),
                "metrics": runtime_output.metrics,
            }

            yield dg.Output(
                value=output_payload,
                metadata={
                    "correlation_id": correlation_id,
                    "records_processed": runtime_output.records_processed,
                    "snapshot": dg.MetadataValue.json(
                        output_payload["snapshot"]
                    ),
                    "storage_uri_pointers": dg.MetadataValue.json(
                        runtime_output.storage_uri_pointers
                    ),
                    "metrics": dg.MetadataValue.json(runtime_output.metrics),
                },
            )

        return compute_node

    @staticmethod
    def build_schedule(step: PipelineStep) -> dg.ScheduleDefinition | None:
        """Compiles standard cron definitions into standalone target asset schedules."""
        if step.params.trigger is not TriggerType.CRON:
            return None
        return dg.ScheduleDefinition(
            name=f"schedule_{step.step}",
            cron_schedule=step.params.cron,
            target=dg.AssetSelection.assets(step.step),
        )
