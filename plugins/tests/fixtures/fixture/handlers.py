"""Test-only reviewed bundle; never installed in production runtime images."""

import asyncio
from collections.abc import Mapping

from galadril_pipeline.config import PipelineStep
from galadril_pipeline.events import PipelineCommand
from galadril_plugins import PluginScope
from pydantic import JsonValue


class Counter:
    def __init__(self, scope: PluginScope, step: PipelineStep) -> None:
        self.scope = scope

    async def execute(
        self, command: PipelineCommand, step: PipelineStep
    ) -> dict[str, JsonValue]:
        return {"count": 2, "tenant": self.scope.tenant_id}


class Listener:
    def __init__(self, environment: Mapping[str, str]) -> None:
        self.environment = environment

    async def run(self, stop: asyncio.Event) -> None:
        stop.set()
