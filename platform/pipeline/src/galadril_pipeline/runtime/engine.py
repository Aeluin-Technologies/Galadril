"""Pipeline orchestration engine with dependency control."""

from __future__ import annotations
import abc
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any

from galadril_pipeline.compiler.resources import (
    AbstractStepExecutor,
    NodeStatus,
    NodeTelemetrySnapshot,
    StepRuntimeInput,
    StepRuntimeOutput,
)
from galadril_pipeline.runtime.schemas import PipelineRunContext, StepCheckpoint
from galadril_pipeline.config import PipelineConfig, PipelineStep

logger = logging.getLogger("galadril.runtime.engine")


class AbstractCheckpointStore(abc.ABC):
    """Abstract interface enforcing state persistence contracts for execution runtimes."""

    @abc.abstractmethod
    async def get_checkpoint(
        self, run_id: str, step_name: str
    ) -> StepCheckpoint | None:
        """Retrieves an execution checkpoint for an isolated pipeline step."""

    @abc.abstractmethod
    async def save_checkpoint(
        self, run_id: str, checkpoint: StepCheckpoint
    ) -> None:
        """Persists a verified step execution checkpoint to the persistent layer."""


class AsyncPipelineEngine:
    """Orchestrates deterministic concurrent execution of pipeline graphs with bounded resources."""

    __slots__ = ("_executor", "_checkpoint_store", "_semaphore")

    def __init__(
        self,
        executor: AbstractStepExecutor,
        checkpoint_store: AbstractCheckpointStore,
        max_concurrent_tasks: int = 8,
    ) -> None:
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
        """Computes an exhaustive deterministic cryptographic SHA-256 signature of the execution state."""
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
        """Executes a pipeline graph by dynamically chaining asynchronous execution dependencies."""
        steps_by_name = {step.step: step for step in config.pipeline}
        tasks: dict[str, asyncio.Task[NodeTelemetrySnapshot]] = {}

        for source in config.sources:
            tasks[source.id] = asyncio.create_task(
                self._resolve_source_node(source.id, source.topic)
            )

        for step in config.pipeline:
            self._register_task_recursive(
                step, run_context, steps_by_name, tasks
            )

        pipeline_step_ids = [step.step for step in config.pipeline]
        results = await asyncio.gather(
            *(tasks[sid] for sid in pipeline_step_ids), return_exceptions=False
        )
        return {res.node_id: res for res in results}

    async def _resolve_source_node(
        self, source_id: str, topic: str
    ) -> NodeTelemetrySnapshot:
        """Encapsulates root source execution context initialization."""
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
    ) -> asyncio.Task[NodeTelemetrySnapshot]:
        """Guarantees task placement and prevent race conditions within the task tracking matrix."""
        if step.step in tasks:
            return tasks[step.step]

        for dep_id in step.input_from:
            if dep_id in steps_by_name and dep_id not in tasks:
                self._register_task_recursive(
                    steps_by_name[dep_id], run_context, steps_by_name, tasks
                )

        tasks[step.step] = asyncio.create_task(
            self._orchestrate_step_task(step, run_context, tasks)
        )
        return tasks[step.step]

    async def _orchestrate_step_task(
        self,
        step: PipelineStep,
        run_context: PipelineRunContext,
        tasks: dict[str, asyncio.Task[NodeTelemetrySnapshot]],
    ) -> NodeTelemetrySnapshot:
        """Awaits structural dependencies and evaluates execution node under explicit concurrency control."""
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

        async with self._semaphore:
            checkpoint = await self._execute_with_retry_policy(
                step, run_context, list(upstream_snapshots)
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
    ) -> StepCheckpoint:
        """Invokes executor actions inside a structured retry control loop."""
        policy = step.params.retry_policy
        max_attempts = policy.max_retries + 1
        delay = policy.delay_seconds
        serialized_params = step.params.model_dump(mode="python")

        runtime_input = StepRuntimeInput(
            correlation_id=run_context.correlation_id,
            step_name=step.step,
            step_type=step.type,
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
                    return StepCheckpoint(
                        step_name=step.step,
                        correlation_id=run_context.correlation_id,
                        status=NodeStatus.COMPLETED,
                        completed_at=datetime.now(timezone.utc),
                        records_processed=runtime_output.records_processed,
                        storage_uri_pointers=runtime_output.storage_uri_pointers,
                        payload_checksum=checksum,
                    )
            except Exception as exc:
                logger.error(
                    f"Execution anomaly on node '{step.step}' (Attempt {attempt}/{max_attempts}): {exc}"
                )

            if attempt == max_attempts:
                break
            if attempt < max_attempts:
                await asyncio.sleep(delay)

        checksum_fail = self._compute_state_checksum(
            step.step,
            run_context.run_id,
            NodeStatus.FAILED,
            0,
            [],
            serialized_params,
        )
        return StepCheckpoint(
            step_name=step.step,
            correlation_id=run_context.correlation_id,
            status=NodeStatus.FAILED,
            completed_at=datetime.now(timezone.utc),
            records_processed=0,
            storage_uri_pointers=[],
            payload_checksum=checksum_fail,
        )
