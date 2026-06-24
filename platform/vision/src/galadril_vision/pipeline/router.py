"""Multi-tenant pipeline routing with optimized async caching and real-time warming."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple, List

import structlog
import yaml

from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore
from galadril_vision.connectors.postgres.vector import VectorStore
from galadril_vision.connectors.s3.client import S3Client
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

logger = structlog.get_logger(__name__)


class PipelineRouteKey:
    """Zero-allocation structural routing key mapping a tenant to an incoming message profile."""

    __slots__ = ("tenant_id", "topic")

    def __init__(self, tenant_id: str, topic: str) -> None:
        self.tenant_id: str = tenant_id
        self.topic: str = topic

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, PipelineRouteKey):
            return False
        return self.tenant_id == other.tenant_id and self.topic == other.topic

    def __hash__(self) -> int:
        return hash((self.tenant_id, self.topic))


class TrackedExecutor:
    """Wraps an ESKGPipelineExecutor to track active batches and defer connection closures."""

    def __init__(self, executor: ESKGPipelineExecutor) -> None:
        """Initializes the TrackedExecutor instance."""
        self.executor = executor
        self.active_count = 0
        self._closed = False

    async def execute_batch(self, records: list[dict[str, Any]]) -> None:
        if self._closed:
            raise RuntimeError("Cannot execute batch on a closing executor.")
        self.active_count += 1
        try:
            await self.executor.execute_batch(records)
        finally:
            self.active_count -= 1

    async def safe_close(self) -> None:
        """Waits for active tasks to drain before terminating underlying database pools."""
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
        self.key: PipelineRouteKey = key
        self.tracked_executor: TrackedExecutor = tracked_executor
        self.prev: Optional[LRUNode] = None
        self.next: Optional[LRUNode] = None


class PipelineLRUCache:
    """Synchronous allocation-conscious LRU Cache preventing capacity race conditions."""

    def __init__(self, capacity: int) -> None:
        """Initializes the LRU cache with a strict maximum capacity."""
        if capacity <= 0:
            raise ValueError(
                "Cache capacity must be strictly greater than zero."
            )
        self._capacity: int = capacity
        self._lookup: Dict[PipelineRouteKey, LRUNode] = {}
        self._head: Optional[LRUNode] = None
        self._tail: Optional[LRUNode] = None

    def get(self, key: PipelineRouteKey) -> Optional[TrackedExecutor]:
        """Retrieves an executor by key and moves it to the head of the cache."""
        node = self._lookup.get(key)
        if node is None:
            return None
        self._move_to_head(node)
        return node.tracked_executor

    def put_sync(
        self, key: PipelineRouteKey, tracked_executor: TrackedExecutor
    ) -> Optional[TrackedExecutor]:
        """Atomically updates cache pointers and returns the evicted executor (if any) for async cleanup."""
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
        executors_to_close = []
        while self._tail is not None:
            old_exec = self._evict_least_recently_used_sync()
            if old_exec:
                executors_to_close.append(old_exec)
        return executors_to_close


class MultiTenantPipelineRouter:
    """Discovers, parses, and caches multi-tenant pipelines."""

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
        """Initializes the pipeline router and AWS client configuration."""
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
        """Explicitly builds and caches an executor configuration BEFORE traffic arrives."""
        route_key = PipelineRouteKey(tenant_id=tenant_id, topic=topic)
        if self._cache.get(route_key) is None:
            tracked_exec = await self._discover_and_build_executor(route_key)
            old_exec = self._cache.put_sync(route_key, tracked_exec)
            if old_exec and old_exec is not tracked_exec:
                asyncio.create_task(old_exec.safe_close())

    async def dispatch_batch(
        self,
        route_key: PipelineRouteKey,
        records: list[dict[str, Any]],
        fallback_timeout_s: float = 30.0,
    ) -> None:
        """Dispatches record arrays using explicit execution timeouts and race-condition guards."""
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

        timeout_s = tracked_exec.executor.batch_timeout_s or fallback_timeout_s
        await asyncio.wait_for(
            tracked_exec.execute_batch(records),
            timeout=max(float(timeout_s), 0.001),
        )

    async def _async_fetch_and_match(self, tenant_id: str, topic: str) -> bytes:
        """Asynchronously discovers and fetches configuration binaries using structured multiplexing."""
        if not tenant_id or not all(
            c.isalnum() or c in "-_" for c in tenant_id
        ):
            raise ValueError(f"Unsafe tenant_id: {tenant_id}")
        if not topic or not all(c.isalnum() or c in "-_" for c in topic):
            raise ValueError(f"Unsafe topic: {topic}")

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
            vector_store=vector_store,
            graph_store=graph_store,
            pg_client=pg_client,
        )
        return TrackedExecutor(base_executor)

    async def close(self) -> None:
        """Safely cleans up all executors and releases S3 handles during shutdown."""
        executors_to_close = self._cache.clear_all_sync()
        if executors_to_close:
            await asyncio.gather(
                *(exec.safe_close() for exec in executors_to_close)
            )
        await self._s3_client.close()
