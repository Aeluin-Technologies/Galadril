"""High-performance Kafka resource integration utilizing Dagster context lifecycles."""

from __future__ import annotations

import asyncio
from typing import Any
import dagster as dg
from confluent_kafka import Consumer, TopicPartition, KafkaError
from pydantic import Field, PrivateAttr
import orjson


class KafkaResource(dg.ConfigurableResource):
    """Lifecycle-managed streaming resource handling fast watermark offset checks and record parsing."""

    bootstrap_servers: str = Field(
        description="Comma-separated broker network addresses."
    )
    group_id: str = Field(
        description="Unique consumer group identifier mapping assignment contexts."
    )
    topics: list[str] = Field(
        description="Target streaming topics to monitor and process."
    )

    _consumer: Consumer | None = PrivateAttr(default=None)

    def setup_for_execution(self, context: dg.InitResourceContext) -> None:
        """Initializes the underlying streaming client and registers target partition subscriptions."""
        self._consumer = Consumer(
            {
                "bootstrap.servers": self.bootstrap_servers,
                "group.id": self.group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
            }
        )
        self._consumer.subscribe(self.topics)

    def teardown_after_execution(self, context: dg.InitResourceContext) -> None:
        """Closes active network sockets and leaves the consumer group cleanly."""
        if self._consumer:
            self._consumer.close()

    def has_lag(self) -> bool:
        """Checks partition high watermarks against current positions non-destructively."""
        if not self._consumer:
            return False
        try:
            assigned_partitions = self._consumer.assignment()
            if not assigned_partitions:
                return False

            positions = self._consumer.position(assigned_partitions)
            for tp in positions:
                current_offset = tp.offset
                _, high_watermark = self._consumer.get_watermark_offsets(
                    tp, timeout=1.0
                )
                if high_watermark > max(0, current_offset):
                    return True
        except Exception:
            return False
        return False

    def get_current_offsets(self) -> dict[str, dict[int, int]]:
        """Retrieves and calculates active partition offsets for checkpoint references."""
        offsets: dict[str, dict[int, int]] = {}
        if not self._consumer:
            return offsets

        try:
            assigned_partitions = self._consumer.assignment()
            if not assigned_partitions:
                return offsets

            positions = self._consumer.position(assigned_partitions)
            for tp in positions:
                if tp.offset >= 0:
                    topic = tp.topic
                    partition = tp.partition
                    if topic not in offsets:
                        offsets[topic] = {}
                    offsets[topic][partition] = tp.offset - 1
        except Exception:
            return offsets
        return offsets

    async def poll_batch(
        self, max_records: int = 1000, timeout_s: float = 1.0
    ) -> list[dict[str, Any]]:
        """Accumulates individual messages into validated collections up to specific boundaries."""
        if not self._consumer:
            return []

        records: list[dict[str, Any]] = []
        start_time = asyncio.get_running_loop().time()

        while len(records) < max_records:
            elapsed = asyncio.get_running_loop().time() - start_time
            remaining = timeout_s - elapsed
            if remaining <= 0:
                break

            msg = await asyncio.to_thread(
                self._consumer.poll, timeout=max(0.01, remaining)
            )
            if msg is None:
                break

            err = msg.error()
            if err is not None:
                if err.code() == KafkaError._PARTITION_EOF:
                    continue
                break

            try:
                val = msg.value()
                payload_data = orjson.loads(val) if val else {}
                key = msg.key()
                records.append(
                    {
                        "record_id": key.decode("utf-8")
                        if key is not None
                        else "",
                        "storage_path": payload_data.get("storage_path")
                        if isinstance(payload_data, dict)
                        else None,
                        "tenant_id": payload_data.get("tenant_id", "default")
                        if isinstance(payload_data, dict)
                        else "default",
                        "event_type": "stream_event",
                        "raw_payload": payload_data,
                        "metadata": {},
                        "source": msg.topic(),
                    }
                )
            except Exception:
                continue

        return records

    async def commit_offsets(self, offsets: dict[str, dict[int, int]]) -> None:
        """Applies explicit processing checkpoints back to broker coordinators."""
        if not self._consumer:
            return

        topic_partitions = [
            TopicPartition(topic, partition, offset + 1)
            for topic, partitions in offsets.items()
            for partition, offset in partitions.items()
        ]
        await asyncio.to_thread(
            self._consumer.commit, offsets=topic_partitions, asynchronous=False
        )
