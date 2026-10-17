"""Ray composition root for application execution and built-in adapters."""

from __future__ import annotations

import gc
from collections.abc import Callable, Mapping, Sequence

import structlog
from galadril_ontology import OntologyRuntimeManager
from galadril_pipeline.events import PipelineCommand
from pydantic import JsonValue

from galadril_vision.adapters.builtin import BuiltinAdapterFactory
from galadril_vision.application.errors import (
    CommandProcessingError as CommandProcessingError,
)
from galadril_vision.application.execution import (
    ExecutionRoute,
    PipelineExecutor,
    StepHandler,
)
from galadril_vision.common.config import VisionConfig

logger = structlog.get_logger(__name__)

type HandlerFactory = Callable[[VisionConfig], Mapping[str, StepHandler]]


class VisionCommandProcessor:
    """Keeps transport cleanup outside replaceable step implementations."""

    __slots__ = ("_executor",)

    def __init__(
        self,
        config: VisionConfig | Sequence[VisionConfig],
        *,
        ontology_runtime: OntologyRuntimeManager | None = None,
        handler_factory: HandlerFactory | None = None,
    ) -> None:
        """Snapshots trusted settings before constructing route-owned adapters."""
        configs = (
            (config,) if isinstance(config, VisionConfig) else tuple(config)
        )
        factory = (
            handler_factory
            if handler_factory is not None
            else BuiltinAdapterFactory()
        )
        routes: list[ExecutionRoute] = []
        for item in configs:
            snapshot = item.model_copy(deep=True)
            pipeline = snapshot.to_pipeline_config()
            routes.append(
                ExecutionRoute(
                    execution_id=snapshot.name,
                    tenant_id=snapshot.runtime_tenant_id,
                    pipeline_id=snapshot.ontology_pipeline_id,
                    revision_id=snapshot.runtime_revision_id or snapshot.name,
                    steps=tuple(pipeline.pipeline),
                    handlers=factory(snapshot),
                )
            )
        self._executor = PipelineExecutor(
            routes, ontology_runtime=ontology_runtime
        )

    async def process(self, command: PipelineCommand) -> dict[str, JsonValue]:
        """Preserves mandatory request cleanup for every injected handler."""
        try:
            return await self._executor.process(command)
        finally:
            self._sanitize(command)

    @staticmethod
    def _sanitize(command: PipelineCommand) -> None:
        """Drops request-local state without destroying reusable backend pools."""
        structlog.contextvars.clear_contextvars()
        collected = gc.collect()
        if command.resource_class.value == "gpu":
            try:
                import torch
            except ImportError:
                logger.warning("ray_actor_gpu_cache_cleanup_unavailable")
            else:
                torch.cuda.empty_cache()
        logger.debug(
            "ray_actor_command_sanitized",
            collected_objects=collected,
            resource_class=command.resource_class.value,
        )
