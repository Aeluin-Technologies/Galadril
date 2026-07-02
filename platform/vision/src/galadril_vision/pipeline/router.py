"""Multi-tenant pipeline routing and execution cache."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple, List
import structlog
import yaml

from galadril_pipeline.runtime.batch import PipelineResult
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
        while self.active_count > 0:
            await asyncio.sleep(0.1)

        if hasattr(self.executor, "_pg_client") and self.executor._pg_client:
            try:
                await self.executor._pg_client.close()
                logger.info("executor_pool_closed_cleanly")
            except Exception as exc:
                logger.error("failed_to_close_pg_client", error=str(exc))


class LRUNode:
    """Internal node for the PipelineLRUCache doubly-linked list."""

    __slots__ = ("key", "tracked_executor", "prev", "next")

    def __init__(
        self, key: PipelineRouteKey, tracked_executor: TrackedExecutor
    ) -> None:
        """Initializes the cache node.

        Args:
            key: Routing key associated with the node.
            tracked_executor: The wrapped executor instance.
        """
        self.key: PipelineRouteKey = key
        self.tracked_executor: TrackedExecutor = tracked_executor
        self.prev: Optional[LRUNode] = None
        self.next: Optional[LRUNode] = None


class PipelineLRUCache:
    """LRU Cache for mapping pipeline route keys to executors."""

    def __init__(self, capacity: int) -> None:
        """Initializes the cache.

        Args:
            capacity: Maximum number of entries allowed in the cache.

        Raises:
            ValueError: If capacity is less than or equal to zero.
        """
        if capacity <= 0:
            raise ValueError(
                "Cache capacity must be strictly greater than zero."
            )
        self._capacity: int = capacity
        self._lookup: Dict[PipelineRouteKey, LRUNode] = {}
        self._head: Optional[LRUNode] = None
        self._tail: Optional[LRUNode] = None

    def get(self, key: PipelineRouteKey) -> Optional[TrackedExecutor]:
        """Retrieves an executor by key and refreshes its LRU position.

        Args:
            key: The route key lookup identifier.

        Returns:
            The matching TrackedExecutor instance if found, else None.
        """
        node = self._lookup.get(key)
        if node is None:
            return None
        self._move_to_head(node)
        return node.tracked_executor

    def put_sync(
        self, key: PipelineRouteKey, tracked_executor: TrackedExecutor
    ) -> Optional[TrackedExecutor]:
        """Updates or inserts an entry in the cache, returning any evicted executor.

        Args:
            key: Routing key for the entry.
            tracked_executor: The executor instance to store.

        Returns:
            The evicted TrackedExecutor if capacity was exceeded, else None.
        """
        evicted_executor = None
        node = self._lookup.get(key)
        if node is not None:
            evicted_executor = node.tracked_executor
            node.tracked_executor = tracked_executor
            self._move_to_head(node)
            return evicted_executor

        if len(self._lookup) >= self._capacity:
            evicted_executor = self._evict_least_recently_used_sync()

        new_node = LRUNode(key, tracked_executor)
        self._lookup[key] = new_node
        self._add_to_head(new_node)
        return evicted_executor

    def _add_to_head(self, node: LRUNode) -> None:
        node.next = self._head
        node.prev = None
        if self._head is not None:
            self._head.prev = node
        self._head = node
        if self._tail is None:
            self._tail = node

    def _remove_node(self, node: LRUNode) -> None:
        if node.prev is not None:
            node.prev.next = node.next
        else:
            self._head = node.next

        if node.next is not None:
            node.next.prev = node.prev
        else:
            self._tail = node.prev

    def _move_to_head(self, node: LRUNode) -> None:
        self._remove_node(node)
        self._add_to_head(node)

    def _evict_least_recently_used_sync(self) -> Optional[TrackedExecutor]:
        if self._tail is None:
            return None
        oldest_node = self._tail
        self._remove_node(oldest_node)
        self._lookup.pop(oldest_node.key, None)
        logger.info(
            "evicting_pipeline_executor_from_cache",
            tenant_id=oldest_node.key.tenant_id,
            topic=oldest_node.key.topic,
        )
        return oldest_node.tracked_executor

    def clear_all_sync(self) -> list[TrackedExecutor]:
        """Evicts and returns all items currently in the cache.

        Returns:
            A list containing all evicted TrackedExecutor instances.
        """
        executors_to_close = []
        while self._tail is not None:
            old_exec = self._evict_least_recently_used_sync()
            if old_exec:
                executors_to_close.append(old_exec)
        return executors_to_close


class MultiTenantPipelineRouter:
    """Manages routing configurations and lifecycle execution chains across tenants."""

    def __init__(
        self,
        *,
        config_bucket: str,
        cache_capacity: int = 50,
        s3_endpoint_url: Optional[str] = None,
        aws_access_key: Optional[str] = None,
        aws_secret_key: Optional[str] = None,
        aws_region: str = "us-east-1",
    ) -> None:
        """Initializes the multi-tenant pipeline router.

        Args:
            config_bucket: Name of the S3 bucket hosting pipeline configs.
            cache_capacity: Maximum number of active pipelines to hold in cache.
            s3_endpoint_url: Optional custom S3 API endpoint.
            aws_access_key: Optional AWS credentials access key.
            aws_secret_key: Optional AWS credentials secret key.
            aws_region: Target AWS region name.
        """
        self._cache = PipelineLRUCache(capacity=cache_capacity)
        self._tenant_s3_index: Dict[str, List[str]] = {}
        self._last_index_fetch: Dict[str, float] = {}
        self._topic_to_key_cache: Dict[Tuple[str, str], str] = {}
        self._creation_tasks: Dict[
            PipelineRouteKey, asyncio.Task[TrackedExecutor]
        ] = {}

        self._s3_client = S3Client(
            bucket=config_bucket,
            endpoint_url=s3_endpoint_url,
            aws_access_key=aws_access_key,
            aws_secret_key=aws_secret_key,
            aws_region=aws_region,
        )

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
            old_exec = self._cache.put_sync(route_key, tracked_exec)
            if old_exec and old_exec is not tracked_exec:
                asyncio.create_task(old_exec.safe_close())

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
                old_exec = self._cache.put_sync(route_key, tracked_exec)
                if old_exec and old_exec is not tracked_exec:
                    asyncio.create_task(old_exec.safe_close())
            finally:
                self._creation_tasks.pop(route_key, None)

        timeout_s = fallback_timeout_s
        if hasattr(tracked_exec.executor.config, "batch_timeout_s"):
            timeout_s = float(fallback_timeout_s) or fallback_timeout_s

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

        async def inspect_key(key: str) -> Optional[tuple[str, bytes]]:
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
        executors_to_close = self._cache.clear_all_sync()
        if executors_to_close:
            await asyncio.gather(
                *(exec.safe_close() for exec in executors_to_close)
            )
        await self._s3_client.close()
