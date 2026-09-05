"""Loads immutable tenant DAGs and indexes them for one shared Vision runtime."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import structlog
from galadril_ontology.backends.terminus import TerminusClient, document_named
from galadril_pipeline.routing import PipelineRouteTable

from galadril_vision.common.config import SourceConfig, VisionConfig

logger = structlog.get_logger(__name__)
_TENANT = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PIPELINE = re.compile(r"[A-Za-z0-9_-]{1,128}")


class PipelineUnavailable(RuntimeError):
    """A runtime cannot obtain an exact validated tenant publication."""


class PipelineRuntimeRegistry:
    """Precomputed tenant, source, command, and route indexes for all DAGs."""

    __slots__ = ("_by_command", "_by_source", "_configs", "_routes")

    def __init__(self, configs: Sequence[VisionConfig]) -> None:
        if not configs:
            raise PipelineUnavailable("No published pipeline is available")
        by_command: dict[str, VisionConfig] = {}
        by_source: dict[tuple[str | None, str], list[VisionConfig]] = {}
        routes: dict[str, PipelineRouteTable] = {}
        for config in configs:
            if config.name in by_command:
                raise PipelineUnavailable(
                    "Duplicate pipeline execution identity"
                )
            by_command[config.name] = config
            routes[config.name] = PipelineRouteTable(
                config.to_pipeline_config()
            )
            for source in config.sources:
                key = (config.runtime_tenant_id, source.id)
                by_source.setdefault(key, []).append(config)
        self._configs = tuple(configs)
        self._by_command = by_command
        self._by_source = {
            key: tuple(values) for key, values in by_source.items()
        }
        self._routes = routes

    @property
    def configs(self) -> tuple[VisionConfig, ...]:
        return self._configs

    @property
    def sources(self) -> tuple[SourceConfig, ...]:
        """Returns source contracts used to build the shared schema index."""
        return tuple(
            source for config in self._configs for source in config.sources
        )

    @property
    def topics(self) -> tuple[str, ...]:
        """Returns the stable union consumed by the shared ingress group."""
        return tuple(
            sorted(
                {
                    source.topic
                    for config in self._configs
                    for source in config.sources
                }
            )
        )

    def for_ingress(
        self, tenant_id: str, source_id: str
    ) -> tuple[VisionConfig, ...]:
        """Returns exact tenant DAGs plus an optional explicit local example."""
        exact = self._by_source.get((tenant_id, source_id), ())
        local = self._by_source.get((None, source_id), ())
        return (*exact, *local)

    def for_ingress_identity(
        self,
        tenant_id: str,
        pipeline_id: str,
        revision_id: str,
        source_id: str,
    ) -> VisionConfig:
        """Resolves one source only through its trusted immutable publication."""
        execution_identity = f"{tenant_id}/{pipeline_id}/{revision_id}"
        config = self._by_command.get(execution_identity)
        if (
            config is None
            or config.runtime_tenant_id != tenant_id
            or config.runtime_pipeline_id != pipeline_id
            or config.runtime_revision_id != revision_id
            or config not in self._by_source.get((tenant_id, source_id), ())
        ):
            raise PipelineUnavailable(
                "Ingress identity does not match a published tenant revision"
            )
        return config

    def for_command(self, tenant_id: str, pipeline: str) -> VisionConfig:
        """Resolves a command only when tenant and immutable revision agree."""
        config = self._by_command.get(pipeline)
        if config is None or not config.accepts_command(tenant_id, pipeline):
            raise PipelineUnavailable(
                "Command does not match a loaded tenant or revision"
            )
        return config

    def routes_for(self, pipeline: str) -> PipelineRouteTable:
        try:
            return self._routes[pipeline]
        except KeyError as error:
            raise PipelineUnavailable(
                "Pipeline route table is unavailable"
            ) from error


async def load_published_pipeline(
    bootstrap: VisionConfig, tenant_id: str, pipeline_id: str
) -> VisionConfig:
    """Pins one publication for compatibility with explicit API consumers."""
    _validate_scope(tenant_id, pipeline_id)
    client = TerminusClient(bootstrap.connectors.terminusdb)
    try:
        _, documents = await client.read(tenant_id)
        entry = document_named(documents, "pipeline/" + pipeline_id)
        return await _load_entry(client, bootstrap, tenant_id, entry)
    except PipelineUnavailable:
        raise
    except Exception as error:
        _log_failure(tenant_id, pipeline_id, error)
        raise PipelineUnavailable(
            "Unable to load published pipeline"
        ) from error
    finally:
        await client.close()


async def load_published_pipelines(
    bootstrap: VisionConfig,
) -> tuple[VisionConfig, ...]:
    """Pins every publication reachable through configured tenant capabilities."""
    client = TerminusClient(bootstrap.connectors.terminusdb)
    loaded: list[VisionConfig] = []
    try:
        for tenant_id in sorted(bootstrap.connectors.terminusdb.tenants):
            _, documents = await client.read(tenant_id)
            entries = sorted(
                (
                    document
                    for document in documents
                    if isinstance(document.get("pipeline_id"), str)
                    and document.get("deleted_at_ms") is None
                    and isinstance(document.get("published_revision_id"), str)
                ),
                key=lambda document: str(document.get("pipeline_id")),
            )
            for entry in entries:
                loaded.append(
                    await _load_entry(client, bootstrap, tenant_id, entry)
                )
    except PipelineUnavailable:
        raise
    except Exception as error:
        logger.error(
            "pipeline_catalog_load_failed", error_type=type(error).__name__
        )
        raise PipelineUnavailable(
            "Unable to load published pipeline catalogue"
        ) from error
    finally:
        await client.close()
    if not loaded:
        raise PipelineUnavailable("No published pipeline is available")
    return tuple(loaded)


async def _load_entry(
    client: TerminusClient,
    bootstrap: VisionConfig,
    tenant_id: str,
    entry: Mapping[str, object],
) -> VisionConfig:
    pipeline_id = entry.get("pipeline_id")
    revision = entry.get("published_revision_id")
    if not isinstance(pipeline_id, str):
        raise PipelineUnavailable("Published pipeline identifier is invalid")
    _validate_scope(tenant_id, pipeline_id)
    if not isinstance(revision, str) or entry.get("deleted_at_ms") is not None:
        raise PipelineUnavailable("Published pipeline is unavailable")
    _, pinned = await client.read(tenant_id, ref=revision, commit=True)
    pinned_entry = document_named(pinned, "pipeline/" + pipeline_id)
    data = pinned_entry.get("definition")
    if not isinstance(data, dict):
        raise PipelineUnavailable("Invalid published pipeline definition")
    config = VisionConfig.with_pipeline(bootstrap.model_dump(), data)
    # Published DAGs vary, while trusted connector and compute settings are
    # process-wide. Reuse those immutable references instead of retaining one
    # copy of every tenant capability and secret per pipeline.
    config.connectors = bootstrap.connectors
    config.ray = bootstrap.ray
    config.identity_resolution = bootstrap.identity_resolution
    config.name = f"{tenant_id}/{pipeline_id}/{revision}"
    config.runtime_tenant_id = tenant_id
    config.runtime_pipeline_id = pipeline_id
    config.runtime_revision_id = revision
    logger.info(
        "pipeline_revision_loaded",
        tenant_id=tenant_id,
        pipeline_id=pipeline_id,
        revision_id=revision,
        steps=len(config.pipeline),
    )
    return config


def _validate_scope(tenant_id: str, pipeline_id: str) -> None:
    if (
        _TENANT.fullmatch(tenant_id) is None
        or _PIPELINE.fullmatch(pipeline_id) is None
    ):
        raise PipelineUnavailable("Invalid pipeline deployment scope")


def _log_failure(tenant_id: str, pipeline_id: str, error: Exception) -> None:
    logger.error(
        "pipeline_revision_load_failed",
        tenant_id=tenant_id,
        pipeline_id=pipeline_id,
        error_type=type(error).__name__,
    )
