"""Multi-tenant pipeline routing and execution cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog
import yaml
from galadril_pipeline.runtime.batch import PipelineResult
from moka_py import Moka

from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore
from galadril_vision.connectors.postgres.vector import VectorStore
from galadril_vision.connectors.s3.client import S3Client
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

logger = structlog.get_logger(__name__)


class PipelineRouteKey:
    """Routing key mapping a tenant to an incoming message profile."""

    __slots__ = ("tenant_id", "topic")

    def __init__(self, tenant_id: str, topic: str) -> None:
        """Initializes the route key.

        Args:
            tenant_id: Unique identifier for the tenant.
            topic: Message topic string.
        """
        self.tenant_id: str = tenant_id
        self.topic: str = topic

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, PipelineRouteKey):
            return False
        return self.tenant_id == other.tenant_id and self.topic == other.topic

    def __hash__(self) -> int:
        return hash((self.tenant_id, self.topic))


class TrackedExecutor:
    """Wraps an ESKGPipelineExecutor to track active execution count across macro-phases."""

    def __init__(self, executor: ESKGPipelineExecutor) -> None:
        """Initializes the tracked executor.

        Args:
            executor: The underlying pipeline executor instance.
        """
        self.executor = executor
        self.active_count = 0
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def execute_parquet(self, parquet_uri: str) -> PipelineResult:
        """Routes processing execution over a remote Parquet file layer.

        Args:
            parquet_uri: Target S3/MinIO pointer to compile the dataframe over.

        Returns:
            A populated PipelineResult execution summary object.

        Raises:
            RuntimeError: If called while the executor instance is closing.
        """
        if self._closed:
            raise RuntimeError(
                "Cannot execute graph on a closing executor pool."
            )
        self.active_count += 1
        try:
            return await self.executor.execute(parquet_uri)
        finally:
            self.active_count -= 1

    async def safe_close(self) -> None:
        """Waits for active tasks to drain before terminating database clients."""
        self._closed = True
        if self._close_task is None:

            async def _drain_and_close() -> None:
                while self.active_count > 0:
                    await asyncio.sleep(0.1)

                if (
                    hasattr(self.executor, "_pg_client")
                    and self.executor._pg_client
                ):
                    try:
                        await self.executor._pg_client.close()
                        logger.info("executor_pool_closed_cleanly")
                    except Exception as exc:
                        logger.error(
                            "failed_to_close_pg_client", error=str(exc)
                        )

            self._close_task = asyncio.create_task(_drain_and_close())

        await self._close_task


class MultiTenantPipelineRouter:
    """Manages routing configurations and lifecycle execution chains across tenants."""

    def __init__(
        self,
        *,
        config_bucket: str,
        cache_capacity: int = 50,
        ttl_seconds: float | None = 3600.0,
        tti_seconds: float | None = 1800.0,
        s3_endpoint_url: str | None = None,
        aws_access_key: str | None = None,
        aws_secret_key: str | None = None,
        aws_region: str = "us-east-1",
    ) -> None:
        """Initializes the multi-tenant pipeline router.

        Args:
            config_bucket: Name of the S3 bucket hosting pipeline configs.
            cache_capacity: Maximum number of active pipelines to hold in cache.
            ttl_seconds: Max lifespan duration (Time-To-Live) for any entry.
            tti_seconds: Max idle time duration (Time-To-Idle) before automatic cleanup.
            s3_endpoint_url: Optional custom S3 API endpoint.
            aws_access_key: Optional AWS credentials access key.
            aws_secret_key: Optional AWS credentials secret key.
            aws_region: Target AWS region name.
        """
        self._active_keys: set[PipelineRouteKey] = set()
        self._cache: Moka[PipelineRouteKey, TrackedExecutor] = Moka(
            capacity=cache_capacity,
            ttl=ttl_seconds,
            tti=tti_seconds,
            eviction_listener=self._eviction_listener,
        )
        self._tenant_s3_index: dict[str, list[str]] = {}
        self._last_index_fetch: dict[str, float] = {}
        self._topic_to_key_cache: dict[tuple[str, str], str] = {}
        self._creation_tasks: dict[
            PipelineRouteKey, asyncio.Task[TrackedExecutor]
        ] = {}

        self._s3_client = S3Client(
            bucket=config_bucket,
            endpoint_url=s3_endpoint_url,
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            aws_region=aws_region,
        )

    def _eviction_listener(
        self, key: PipelineRouteKey, value: TrackedExecutor, cause: str
    ) -> None:
        """Triggers clean up behavior automatically upon entry removal or replacement."""
        if cause != "replaced":
            self._active_keys.discard(key)

        if cause in ("size", "expired"):
            logger.info(
                "evicting_pipeline_executor_from_cache",
                tenant_id=key.tenant_id,
                topic=key.topic,
                reason=cause,
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(value.safe_close())
        except RuntimeError:
            pass

    async def pre_warm_tenant_pipeline(
        self, tenant_id: str, topic: str
    ) -> None:
        """Builds and caches a tenant pipeline executor prior to receiving traffic.

        Args:
            tenant_id: Target tenant identifier.
            topic: Target message topic.
        """
        route_key = PipelineRouteKey(tenant_id=tenant_id, topic=topic)
        if self._cache.get(route_key) is None:
            tracked_exec = await self._discover_and_build_executor(route_key)
            self._cache.set(route_key, tracked_exec)
            self._active_keys.add(route_key)

    async def dispatch_parquet(
        self,
        route_key: PipelineRouteKey,
        parquet_uri: str,
        fallback_timeout_s: float = 60.0,
    ) -> PipelineResult:
        """Routes a remote Parquet micro-batch reference directly to the target tenant engine.

        Args:
            route_key: Composite routing signature containing tenant and topic context.
            parquet_uri: S3 storage pointer containing the schema-validated records.
            fallback_timeout_s: Default timeout window constraints applied to computation.

        Returns:
            The materialized PipelineResult structured evaluation metadata.
        """
        tracked_exec = self._cache.get(route_key)

        if tracked_exec is None:
            if route_key not in self._creation_tasks:
                self._creation_tasks[route_key] = asyncio.create_task(
                    self._discover_and_build_executor(route_key)
                )
            try:
                tracked_exec = await asyncio.wait_for(
                    asyncio.shield(self._creation_tasks[route_key]),
                    timeout=fallback_timeout_s,
                )
                self._cache.set(route_key, tracked_exec)
                self._active_keys.add(route_key)
            finally:
                self._creation_tasks.pop(route_key, None)

        timeout_s = fallback_timeout_s

        config_obj = getattr(
            tracked_exec.executor, "vision_config", None
        ) or getattr(tracked_exec.executor, "config", None)
        if config_obj and hasattr(config_obj, "batch_timeout_s"):
            config_timeout = config_obj.batch_timeout_s
            if config_timeout is not None:
                timeout_s = float(config_timeout)

        return await asyncio.wait_for(
            tracked_exec.execute_parquet(parquet_uri),
            timeout=max(timeout_s, 0.001),
        )

    async def _async_fetch_and_match(self, tenant_id: str, topic: str) -> bytes:
        """Queries S3 to resolve and download the pipeline configuration definition."""
        if not tenant_id or not all(
            c.isalnum() or c in "-_" for c in tenant_id
        ):
            raise ValueError(f"Unsafe tenant_id token received: {tenant_id}")
        if not topic or not all(c.isalnum() or c in "-_" for c in topic):
            raise ValueError(f"Unsafe topic stream token received: {topic}")

        prefix = f"{tenant_id}/pipelines/"
        now = time.time()

        if tenant_id not in self._tenant_s3_index or (
            now - self._last_index_fetch.get(tenant_id, 0.0) > 300.0
        ):
            keys = await self._s3_client.list_object_keys(prefix)
            self._tenant_s3_index[tenant_id] = keys
            self._last_index_fetch[tenant_id] = now

        yaml_keys = self._tenant_s3_index[tenant_id]
        cache_key = (tenant_id, topic)

        if cache_key in self._topic_to_key_cache:
            resolved_key = self._topic_to_key_cache[cache_key]
            if resolved_key in yaml_keys:
                return await self._s3_client.get_object_bytes(resolved_key)

        exact_match_key = f"{prefix}{topic}.yaml"
        if exact_match_key in yaml_keys:
            content = await self._s3_client.get_object_bytes(exact_match_key)
            self._topic_to_key_cache[cache_key] = exact_match_key
            return content

        async def inspect_key(key: str) -> tuple[str, bytes] | None:
            try:
                content = await self._s3_client.get_object_bytes(key)
                parsed = yaml.safe_load(content)
                for source in parsed.get("sources", []):
                    if source.get("topic") == topic:
                        return key, content
            except Exception as exc:
                logger.warning(
                    "failed_inspect_pipeline_config", key=key, error=str(exc)
                )
            return None

        tasks = [asyncio.create_task(inspect_key(k)) for k in yaml_keys]
        try:
            for completed_task in asyncio.as_completed(tasks):
                result = await completed_task
                if result:
                    matched_key, content = result
                    self._topic_to_key_cache[cache_key] = matched_key
                    return content
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

        raise FileNotFoundError(
            f"No pipeline matching topic '{topic}' for tenant '{tenant_id}'"
        )

    async def _discover_and_build_executor(
        self, route_key: PipelineRouteKey
    ) -> TrackedExecutor:
        """Resolves config files from cloud storage and initializes underlying compute clients."""
        raw_content = await self._async_fetch_and_match(
            route_key.tenant_id, route_key.topic
        )
        parsed_data = yaml.safe_load(raw_content.decode("utf-8"))
        cfg = VisionConfig.model_validate(parsed_data)

        pg_client = PostgresClient(cfg.postgres)
        await pg_client.connect()

        vector_store = VectorStore(pg_client, cfg.postgres)
        graph_store = GraphStore(pg_client, cfg.postgres)
        await vector_store.initialize()
        await graph_store.initialize()

        base_executor = ESKGPipelineExecutor(
            config=cfg.to_pipeline_config(),
            vision_config=cfg,
            pg_client=pg_client,
            vector_store=vector_store,
            graph_store=graph_store,
        )
        return TrackedExecutor(base_executor)

    async def close(self) -> None:
        """Closes all tracked cache executors and releases underlying S3 resources."""
        executors_to_close = []
        keys_to_remove = list(self._active_keys)

        for key in keys_to_remove:
            exec = self._cache.get(key)
            if exec:
                executors_to_close.append(exec)
            self._cache.remove(key)

        if executors_to_close:
            await asyncio.gather(
                *(exec.safe_close() for exec in executors_to_close)
            )
        await self._s3_client.close()
