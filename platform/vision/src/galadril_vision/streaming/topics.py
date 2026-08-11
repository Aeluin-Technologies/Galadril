"""Versioned Kafka topic layout for independently scalable worker pools."""

from __future__ import annotations

from dataclasses import dataclass

from galadril_pipeline.events import ResourceClass


@dataclass(frozen=True, slots=True)
class TopicLayout:
    """Names durable pipeline topics without embedding payload cardinality."""

    commands_cpu: str = "pipeline.commands.cpu.v1"
    commands_gpu: str = "pipeline.commands.gpu.v1"
    commands_causal: str = "pipeline.commands.causal.v1"
    results: str = "pipeline.results.v1"
    lineage: str = "pipeline.lineage.v1"
    invalid: str = "pipeline.invalid.v1"
    dead_letter: str = "pipeline.dead-letter.v1"

    def commands_for(self, resource: ResourceClass) -> str:
        """Returns the command topic assigned to an execution resource pool."""
        if resource is ResourceClass.GPU:
            return self.commands_gpu
        if resource is ResourceClass.CAUSAL:
            return self.commands_causal
        return self.commands_cpu
