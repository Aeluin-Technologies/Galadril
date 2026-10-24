"""Independent service lifecycle; discovery never starts external-account work."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from collections.abc import Mapping
from typing import Protocol, cast

import structlog

from galadril_plugins import PluginRegistry

logger = structlog.get_logger(__name__)


class PluginService(Protocol):
    async def run(self, stop: asyncio.Event) -> None: ...


async def run_service(
    worker: PluginService, stop: asyncio.Event, *, grace_seconds: float = 20
) -> None:
    """Bound cooperative shutdown so the orchestrator can restart stuck inputs."""
    if grace_seconds <= 0:
        raise ValueError("Shutdown grace period must be positive")
    running = asyncio.create_task(worker.run(stop))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, pending = await asyncio.wait(
            (running, stopping), return_when=asyncio.FIRST_COMPLETED
        )
        if running in done:
            await running
        else:
            try:
                await asyncio.wait_for(running, timeout=grace_seconds)
            except TimeoutError:
                raise RuntimeError(
                    "Plugin service shutdown deadline exceeded"
                ) from None
    finally:
        stopping.cancel()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, stopping, return_exceptions=True)


async def serve(
    plugin: str, service: str, environment: Mapping[str, str]
) -> None:
    factory = PluginRegistry.discover().service(plugin, service)
    worker = cast(PluginService, factory(environment))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    logger.info("plugin.service.started", plugin_id=plugin, service_id=service)
    try:
        await run_service(worker, stop)
    except Exception as error:
        logger.error(
            "plugin.service.failed",
            plugin_id=plugin,
            service_id=service,
            error_type=type(error).__name__,
        )
        raise RuntimeError("Plugin service execution failed") from None
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        logger.info(
            "plugin.service.stopped", plugin_id=plugin, service_id=service
        )


def main() -> None:
    from galadril_plugins.telemetry import telemetry

    parser = argparse.ArgumentParser(
        description="Run one explicitly deployed official plugin microservice."
    )
    parser.add_argument("plugin")
    parser.add_argument("service")
    args = parser.parse_args()
    with telemetry(f"galadril-plugin-{args.plugin}-{args.service}"):
        asyncio.run(serve(args.plugin, args.service, os.environ))


if __name__ == "__main__":
    main()
