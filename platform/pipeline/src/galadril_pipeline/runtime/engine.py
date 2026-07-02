"""Pipeline orchestration engine with dependency control."""

from __future__ import annotations
import abc
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any

from galadril_pipeline.runtime.schemas import (
    AbstractStepExecutor,
    NodeStatus,
    NodeTelemetrySnapshot,
    PipelineRunContext,
    StepCheckpoint,
    StepRuntimeInput,
    StepRuntimeOutput,
)
from galadril_pipeline.config import PipelineConfig, PipelineStep
from galadril_pipeline.runtime.batch import BatchHandle

logger = logging.getLogger("galadril.runtime.engine")


class AbstractCheckpointStore(abc.ABC):
    """Interface for persisting execution states of pipeline steps."""

    @abc.abstractmethod
    async def get_checkpoint(
        self, run_id: str, step_name: str
    ) -> StepCheckpoint | None:
        """Retrieves an execution checkpoint for a specific pipeline step.

        Args:
            run_id: The unique identifier for the pipeline run.
            step_name: The name of the pipeline step.

        Returns:
            The StepCheckpoint if found, otherwise None.
        """

    @abc.abstractmethod
    async def save_checkpoint(
        self, run_id: str, checkpoint: StepCheckpoint
    ) -> None:
        """Persists a step execution checkpoint to the storage layer.

        Args:
            run_id: The unique identifier for the pipeline run.
            checkpoint: The execution state to persist.
        """


class AsyncPipelineEngine:
    """Orchestrates asynchronous, concurrent execution of pipeline graphs."""

    __slots__ = ("_executor", "_checkpoint_store", "_semaphore")

    def __init__(
        self,
        executor: AbstractStepExecutor,
        checkpoint_store: AbstractCheckpointStore,
        max_concurrent_tasks: int = 8,
    ) -> None:
        """Initializes the engine.

        Args:
            executor: The backend service responsible for executing individual steps.
            checkpoint_store: The storage implementation for persisting run state.
            max_concurrent_tasks: The maximum number of concurrent task executions allowed.
        """
        self._executor = executor
        self._checkpoint_store = checkpoint_store
        self._semaphore = asyncio.Semaphore(max_concurrent_tasks)

    @staticmethod
    def _compute_state_checksum(
        step_name: str,
        run_id: str,
        status: NodeStatus,
        records: int,
        uris: list[str],
        params: dict[str, Any],
    ) -> str:
        """Computes a SHA-256 hash for the execution state to ensure data integrity.

        Args:
            step_name: The name of the step.
            run_id: The unique identifier for the pipeline run.
            status: The execution status of the node.
            records: The number of records processed.
            uris: A list of storage URI pointers.
            params: The parameters used for the execution.

        Returns:
            A hexadecimal string representing the state checksum.
        """
        state_data = {
            "step_name": step_name,
            "run_id": run_id,
            "status": status.value,
            "records_processed": records,
            "storage_uris": sorted(uris),
            "params": params,
        }
        serialized = json.dumps(state_data, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def execute_pipeline(
        self, config: PipelineConfig, run_context: PipelineRunContext
    ) -> dict[str, NodeTelemetrySnapshot]:
        """Executes the pipeline configuration graph.

        Args:
            config: The pipeline configuration containing sources and steps.
            run_context: The context for the current pipeline run.

        Returns:
            A dictionary mapping step names to their final telemetry snapshots.
        """
        steps_by_name = {step.step: step for step in config.pipeline}
        tasks: dict[str, asyncio.Task[NodeTelemetrySnapshot]] = {}

        computed_batches: dict[str, BatchHandle[Any]] = {}

        for source in config.sources:
            computed_batches[source.id] = BatchHandle(
                correlation_id=run_context.correlation_id,
                payload=[],
                kafka_offsets={},
            )
            tasks[source.id] = asyncio.create_task(
                self._resolve_source_node(source.id, source.topic)
            )

        for step in config.pipeline:
            self._register_task_recursive(
                step, run_context, steps_by_name, tasks, computed_batches
            )

        pipeline_step_ids = [step.step for step in config.pipeline]
        try:
            results = await asyncio.gather(
                *(tasks[sid] for sid in pipeline_step_ids),
                return_exceptions=False,
            )
        except Exception:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            raise
        return {res.node_id: res for res in results}

    async def _resolve_source_node(
        self, source_id: str, topic: str
    ) -> NodeTelemetrySnapshot:
        """Initializes the execution context for a root source node."""
        return NodeTelemetrySnapshot(
            node_id=source_id,
            status=NodeStatus.COMPLETED,
            records_mutated=0,
            storage_uri_pointers=[f"kafka://{topic}"],
        )

    def _register_task_recursive(
        self,
        step: PipelineStep,
        run_context: PipelineRunContext,
        steps_by_name: dict[str, PipelineStep],
        tasks: dict[str, asyncio.Task[NodeTelemetrySnapshot]],
        computed_batches: dict[str, BatchHandle[Any]],
    ) -> asyncio.Task[NodeTelemetrySnapshot]:
        """Recursively registers pipeline steps as asyncio tasks based on dependencies."""
        if step.step in tasks:
            return tasks[step.step]

        for dep_id in step.input_from:
            if dep_id in steps_by_name and dep_id not in tasks:
                self._register_task_recursive(
                    steps_by_name[dep_id],
                    run_context,
                    steps_by_name,
                    tasks,
                    computed_batches,
                )

        tasks[step.step] = asyncio.create_task(
            self._orchestrate_step_task(
                step, run_context, tasks, computed_batches
            )
        )
        return tasks[step.step]

    async def _orchestrate_step_task(
        self,
        step: PipelineStep,
        run_context: PipelineRunContext,
        tasks: dict[str, asyncio.Task[NodeTelemetrySnapshot]],
        computed_batches: dict[str, BatchHandle[Any]],
    ) -> NodeTelemetrySnapshot:
        """Manages the execution lifecycle and concurrency for a specific step."""
        try:
            upstream_snapshots = await asyncio.gather(
                *(tasks[dep] for dep in step.input_from)
            )
        except Exception as exc:
            logger.error(
                f"Upstream dependency failure propagated to node: '{step.step}'."
            )
            return NodeTelemetrySnapshot(
                node_id=step.step, status=NodeStatus.FAILED, records_mutated=0
            )

        for snapshot in upstream_snapshots:
            if snapshot.status != NodeStatus.COMPLETED:
                logger.warning(
                    f"Step '{step.step}' skipped due to upstream degradation in '{snapshot.node_id}'."
                )
                return NodeTelemetrySnapshot(
                    node_id=step.step,
                    status=NodeStatus.SKIPPED,
                    records_mutated=0,
                )

        input_batch: BatchHandle[Any] | None = None
        for dep in step.input_from:
            if dep in computed_batches:
                if len(computed_batches[dep].payload) > 0:
                    input_batch = computed_batches[dep]
                    break

        if not input_batch:
            for dep in step.input_from:
                if dep in computed_batches:
                    input_batch = computed_batches[dep]
                    break
            if not input_batch:
                input_batch = BatchHandle(
                    correlation_id=run_context.correlation_id,
                    payload=[],
                    kafka_offsets={},
                )

        async with self._semaphore:
            existing_checkpoint = await self._checkpoint_store.get_checkpoint(
                run_context.run_id, step.step
            )
            if (
                existing_checkpoint
                and existing_checkpoint.status == NodeStatus.COMPLETED
            ):
                logger.info(
                    f"Step '{step.step}' already completed successfully. Skipping execution."
                )
                checkpoint = existing_checkpoint
                computed_batches[step.step] = BatchHandle(
                    correlation_id=checkpoint.correlation_id,
                    payload=[],
                    kafka_offsets={},
                )
            else:
                (
                    checkpoint,
                    output_batch,
                ) = await self._execute_with_retry_policy(
                    step, run_context, list(upstream_snapshots), input_batch
                )
                if output_batch is not None:
                    computed_batches[step.step] = output_batch
                else:
                    computed_batches[step.step] = BatchHandle(
                        correlation_id=run_context.correlation_id,
                        payload=[],
                        kafka_offsets={},
                    )

        await self._checkpoint_store.save_checkpoint(
            run_context.run_id, checkpoint
        )

        if checkpoint.status != NodeStatus.COMPLETED:
            return NodeTelemetrySnapshot(
                node_id=step.step, status=NodeStatus.FAILED, records_mutated=0
            )

        return NodeTelemetrySnapshot(
            node_id=step.step,
            status=NodeStatus.COMPLETED,
            records_mutated=checkpoint.records_processed,
            storage_uri_pointers=checkpoint.storage_uri_pointers,
        )

    async def _execute_with_retry_policy(
        self,
        step: PipelineStep,
        run_context: PipelineRunContext,
        upstream_snapshots: list[NodeTelemetrySnapshot],
        batch: BatchHandle[Any],
    ) -> tuple[StepCheckpoint, BatchHandle[Any] | None]:
        """Invokes executor actions applying the configured retry policy."""
        policy = step.params.retry_policy
        max_attempts = policy.max_retries + 1
        delay = policy.delay_seconds
        serialized_params = step.params.model_dump(mode="python")

        runtime_input = StepRuntimeInput(
            correlation_id=run_context.correlation_id,
            step_name=step.step,
            step_type=step.type.value
            if hasattr(step.type, "value")
            else step.type,
            batch=batch,
            params=serialized_params,
            upstream_states=upstream_snapshots,
        )

        for attempt in range(1, max_attempts + 1):
            try:
                runtime_output = await self._executor.execute_step(
                    runtime_input
                )
                if not isinstance(runtime_output, StepRuntimeOutput):
                    raise TypeError(
                        "Contract breach: target engine execution failed to return StepRuntimeOutput."
                    )

                if runtime_output.status == NodeStatus.COMPLETED:
                    checksum = self._compute_state_checksum(
                        step.step,
                        run_context.run_id,
                        NodeStatus.COMPLETED,
                        runtime_output.records_processed,
                        runtime_output.storage_uri_pointers,
                        serialized_params,
                    )
                    checkpoint = StepCheckpoint(
                        step_name=step.step,
                        correlation_id=run_context.correlation_id,
                        status=NodeStatus.COMPLETED,
                        completed_at=datetime.now(timezone.utc),
                        records_processed=runtime_output.records_processed,
                        storage_uri_pointers=runtime_output.storage_uri_pointers,
                        payload_checksum=checksum,
                    )
                    return checkpoint, runtime_output.batch
                else:
                    logger.warning(
                        f"Step '{step.step}' returned non-completed status '{runtime_output.status}' (Attempt {attempt}/{max_attempts}). Details: {runtime_output.error_details}"
                    )
            except Exception as exc:
                logger.error(
                    f"Execution anomaly on node '{step.step}' (Attempt {attempt}/{max_attempts}): {exc}"
                )

            if attempt == max_attempts:
                break
            await asyncio.sleep(delay)

        checksum_fail = self._compute_state_checksum(
            step.step,
            run_context.run_id,
            NodeStatus.FAILED,
            0,
            [],
            serialized_params,
        )
        checkpoint_fail = StepCheckpoint(
            step_name=step.step,
            correlation_id=run_context.correlation_id,
            status=NodeStatus.FAILED,
            completed_at=datetime.now(timezone.utc),
            records_processed=0,
            storage_uri_pointers=[],
            payload_checksum=checksum_fail,
        )
        return checkpoint_fail, None
