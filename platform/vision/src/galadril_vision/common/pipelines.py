"""Loads immutable tenant DAGs through the Registry gRPC boundary."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import orjson
import structlog
from galadril_pipeline.routing import PipelineRouteTable
from galadril_registry_api import PipelineArtifact, RegistryClient

from galadril_vision.common.config import SourceConfig, VisionConfig

logger = structlog.get_logger(__name__)
_PIPELINE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_REVISION = re.compile(r"[A-Za-z0-9_-]{20,128}")


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
    bootstrap: VisionConfig,
    tenant_id: str,
    pipeline_id: str,
    revision_id: str | None = None,
) -> VisionConfig:
    """Loads one current publication or an explicitly pinned revision."""
    _validate_scope(tenant_id, pipeline_id, revision_id)
    client = RegistryClient(bootstrap.registry)
    try:
        validation = await client.validate_tenants((tenant_id,))
        if len(validation) != 1 or not validation[0].exists:
            raise PipelineUnavailable("Pipeline tenant is unavailable")
        if revision_id is not None:
            artifact = await client.get_runtime_pipeline(
                tenant_id, pipeline_id, revision_id
            )
            return _load_artifact(bootstrap, tenant_id, artifact)
        artifacts = await client.list_published_pipelines(tenant_id)
        for artifact in artifacts:
            if artifact.pipeline_id == pipeline_id:
                return _load_artifact(bootstrap, tenant_id, artifact)
        raise PipelineUnavailable("Published pipeline is unavailable")
    except PipelineUnavailable:
        raise
    except Exception as error:
        _log_failure(tenant_id, pipeline_id, error)
        raise PipelineUnavailable(
            "Unable to load published pipeline"
        ) from error
    finally:
        await client.close()


def _load_artifact(
    bootstrap: VisionConfig,
    tenant_id: str,
    artifact: PipelineArtifact,
) -> VisionConfig:
    """Builds execution settings without accepting artifact-side credentials."""
    _validate_scope(tenant_id, artifact.pipeline_id, artifact.revision_id)
    data = orjson.loads(artifact.definition_json)
    if not isinstance(data, dict) or not all(
        isinstance(key, str) for key in data
    ):
        raise PipelineUnavailable("Invalid published pipeline definition")
    definition: Mapping[str, object] = data
    config = VisionConfig.with_pipeline(bootstrap.model_dump(), definition)
    # Shared connector objects are immutable process configuration and can be
    # referenced by every pinned DAG without copying credentials per pipeline.
    config.connectors = bootstrap.connectors
    config.ray = bootstrap.ray
    config.identity_resolution = bootstrap.identity_resolution
    config.name = f"{tenant_id}/{artifact.pipeline_id}/{artifact.revision_id}"
    config.runtime_tenant_id = tenant_id
    config.runtime_pipeline_id = artifact.pipeline_id
    config.runtime_revision_id = artifact.revision_id
    logger.info(
        "pipeline_revision_loaded",
        tenant_id=tenant_id,
        pipeline_id=artifact.pipeline_id,
        revision_id=artifact.revision_id,
        steps=len(config.pipeline),
    )
    return config


def _validate_scope(
    tenant_id: str, pipeline_id: str, revision_id: str | None
) -> None:
    if (
        not _valid_tenant(tenant_id)
        or _PIPELINE.fullmatch(pipeline_id) is None
        or (
            revision_id is not None and _REVISION.fullmatch(revision_id) is None
        )
    ):
        raise PipelineUnavailable("Invalid pipeline deployment scope")


def _valid_tenant(value: str) -> bool:
    """Checks one tenant segment without a shared permissive regex."""
    return (
        1 <= len(value) <= 64
        and value.isascii()
        and all(character.isalnum() or character in "_-" for character in value)
    )


def _log_failure(tenant_id: str, pipeline_id: str, error: Exception) -> None:
    logger.error(
        "pipeline_revision_load_failed",
        tenant_id=tenant_id,
        pipeline_id=pipeline_id,
        error_type=type(error).__name__,
    )
