"""Multi-tenant pipeline routing with optimized caching, warming, and rigorous resource guarantees."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional, Tuple, List
import boto3
from botocore.config import Config
import structlog
import yaml

from galadril_vision.common.config import VisionConfig
from galadril_vision.connectors.postgres.client import PostgresClient
from galadril_vision.connectors.postgres.graph import GraphStore
from galadril_vision.connectors.postgres.vector import VectorStore
from galadril_vision.pipeline.executor import ESKGPipelineExecutor

logger = structlog.get_logger(__name__)


class PipelineRouteKey:
    """Zero-allocation structural routing key mapping a tenant to an incoming message profile."""

    __slots__ = ("tenant_id", "topic", "event_type")

    def __init__(self, tenant_id: str, topic: str, event_type: str) -> None:
        self.tenant_id: str = tenant_id
        self.topic: str = topic
        self.event_type: str = event_type

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, PipelineRouteKey):
            return False
        return self.tenant_id == other.tenant_id and self.topic == other.topic

    def __hash__(self) -> int:
        return hash((self.tenant_id, self.topic))


class LRUNode:
    __slots__ = ("key", "executor", "prev", "next")

    def __init__(
        self, key: PipelineRouteKey, executor: ESKGPipelineExecutor
    ) -> None:
        self.key: PipelineRouteKey = key
        self.executor: ESKGPipelineExecutor = executor
        self.prev: Optional[LRUNode] = None
        self.next: Optional[LRUNode] = None


class PipelineLRUCache:
    """Allocation-conscious async LRU Cache for isolating active tenant ESKGPipelineExecutors."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError(
                "Cache capacity must be strictly greater than zero."
            )
        self._capacity: int = capacity
        self._lookup: Dict[PipelineRouteKey, LRUNode] = {}
        self._head: Optional[LRUNode] = None
        self._tail: Optional[LRUNode] = None

    def get(self, key: PipelineRouteKey) -> Optional[ESKGPipelineExecutor]:
        node = self._lookup.get(key)
        if node is None:
            return None
        self._move_to_head(node)
        return node.executor

    async def put(
        self, key: PipelineRouteKey, executor: ESKGPipelineExecutor
    ) -> None:
        node = self._lookup.get(key)
        if node is not None:
            old_executor = node.executor
            node.executor = executor
            self._move_to_head(node)

            if old_executor is not executor:
                if (
                    hasattr(old_executor, "_pg_client")
                    and old_executor._pg_client
                ):
                    try:
                        await old_executor._pg_client.close()
                    except Exception as exc:
                        logger.error(
                            "failed_to_close_overwritten_executor_pool",
                            error=str(exc),
                        )
            return

        if len(self._lookup) >= self._capacity:
            await self._evict_least_recently_used()

        new_node = LRUNode(key, executor)
        self._lookup[key] = new_node
        self._add_to_head(new_node)

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

    async def _evict_least_recently_used(self) -> None:
        if self._tail is None:
            return
        oldest_node = self._tail
        self._remove_node(oldest_node)
        self._lookup.pop(oldest_node.key, None)

        try:
            logger.info(
                "evicting_pipeline_executor_from_cache",
                tenant_id=oldest_node.key.tenant_id,
                topic=oldest_node.key.topic,
            )
            if (
                hasattr(oldest_node.executor, "_pg_client")
                and oldest_node.executor._pg_client
            ):
                await oldest_node.executor._pg_client.close()
        except Exception as exc:
            logger.error(
                "failed_to_cleanly_close_evicted_executor_pool", error=str(exc)
            )

    async def clear_all(self) -> None:
        while self._tail is not None:
            await self._evict_least_recently_used()


class MultiTenantPipelineRouter:
    """Discovers, parses, and caches multi-tenant pipeline definitions with robust deduplication."""

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
        self._config_bucket = config_bucket
        self._cache = PipelineLRUCache(capacity=cache_capacity)

        self._tenant_s3_index: Dict[str, List[str]] = {}
        self._last_index_fetch: Dict[str, float] = {}
        self._topic_to_key_cache: Dict[Tuple[str, str], str] = {}
        self._creation_tasks: Dict[
            PipelineRouteKey, asyncio.Task[ESKGPipelineExecutor]
        ] = {}

        boto_config = Config(
            region_name=aws_region,
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            max_pool_connections=50,
        )

        self._s3_client = boto3.client(
            "s3",
            endpoint_url=s3_endpoint_url,
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            config=boto_config,
        )

    async def pre_warm_tenant_pipeline(
        self, tenant_id: str, topic: str, event_type: str
    ) -> None:
        """Explicitly builds and caches an executor configuration BEFORE traffic arrives."""
        route_key = PipelineRouteKey(
            tenant_id=tenant_id, topic=topic, event_type=event_type
        )
        if self._cache.get(route_key) is None:
            executor = await self._discover_and_build_executor(route_key)
            await self._cache.put(route_key, executor)

    async def dispatch_batch(
        self,
        route_key: PipelineRouteKey,
        records: list[dict[str, Any]],
        fallback_timeout_s: float = 30.0,
    ) -> None:
        """Dispatches record arrays directly into their targeted environment, guarding against race conditions."""
        executor = self._cache.get(route_key)

        if executor is None:
            if route_key not in self._creation_tasks:
                self._creation_tasks[route_key] = asyncio.create_task(
                    self._discover_and_build_executor(route_key)
                )
            try:
                executor = await self._creation_tasks[route_key]
                await self._cache.put(route_key, executor)
            finally:
                self._creation_tasks.pop(route_key, None)

        # Enforce the tenant-specific batch timeout.
        timeout_s = executor.batch_timeout_s
        if timeout_s is None:
            timeout_s = fallback_timeout_s

        if timeout_s is not None:
            await asyncio.wait_for(
                executor.execute_batch(records),
                timeout=max(float(timeout_s), 0.001),
            )
        else:
            await executor.execute_batch(records)

    def _sync_fetch_and_match(self, tenant_id: str, topic: str) -> bytes:
        """Validates incoming input structures, listing S3 keys within a strict metadata TTL lifecycle."""
        if not tenant_id or not all(
            c.isalnum() or c in "-_" for c in tenant_id
        ):
            raise ValueError(
                f"Unsafe or malformed tenant_id provided: {tenant_id}"
            )

        prefix = f"{tenant_id}/pipelines/"
        now = time.time()

        if tenant_id not in self._tenant_s3_index or (
            now - self._last_index_fetch.get(tenant_id, 0.0) > 300.0
        ):
            paginator = self._s3_client.get_paginator("list_objects_v2")
            pages = paginator.paginate(
                Bucket=self._config_bucket, Prefix=prefix
            )
            keys: List[str] = []
            for page in pages:
                for obj in page.get("Contents", []):
                    k = obj.get("Key")
                    if k and (k.endswith(".yaml") or k.endswith(".yml")):
                        keys.append(k)
            self._tenant_s3_index[tenant_id] = keys
            self._last_index_fetch[tenant_id] = now

        yaml_keys = self._tenant_s3_index[tenant_id]

        cache_key = (tenant_id, topic)
        if cache_key in self._topic_to_key_cache:
            resolved_key = self._topic_to_key_cache[cache_key]
            if resolved_key in yaml_keys:
                response = self._s3_client.get_object(
                    Bucket=self._config_bucket, Key=resolved_key
                )
                return response["Body"].read()

        exact_match_key = f"{prefix}{topic}.yaml"
        if exact_match_key in yaml_keys:
            response = self._s3_client.get_object(
                Bucket=self._config_bucket, Key=exact_match_key
            )
            self._topic_to_key_cache[cache_key] = exact_match_key
            return response["Body"].read()

        for key in yaml_keys:
            response = self._s3_client.get_object(
                Bucket=self._config_bucket, Key=key
            )
            content = response["Body"].read()
            try:
                parsed = yaml.safe_load(content)
                for source in parsed.get("sources", []):
                    if source.get("topic") == topic:
                        self._topic_to_key_cache[cache_key] = key
                        return content
            except Exception:
                continue

        raise FileNotFoundError(
            f"No pipeline matching topic {topic} for tenant {tenant_id}"
        )

    async def _discover_and_build_executor(
        self, route_key: PipelineRouteKey
    ) -> ESKGPipelineExecutor:
        """Discovers and builds executor instances, leveraging threads for blocking logic."""
        raw_content = await asyncio.to_thread(
            self._sync_fetch_and_match, route_key.tenant_id, route_key.topic
        )

        parsed_data = yaml.safe_load(raw_content.decode("utf-8"))
        cfg = VisionConfig.model_validate(parsed_data)

        pg_client = PostgresClient(cfg.postgres)
        await pg_client.connect()

        vector_store = VectorStore(pg_client, cfg.postgres)
        graph_store = GraphStore(pg_client, cfg.postgres)
        await vector_store.initialize()
        await graph_store.initialize()

        return ESKGPipelineExecutor(
            config=cfg.to_pipeline_config(),
            vision_config=cfg,
            vector_store=vector_store,
            graph_store=graph_store,
            pg_client=pg_client,
        )

    async def close(self) -> None:
        await self._cache.clear_all()
