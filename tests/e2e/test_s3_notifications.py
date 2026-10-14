"""Verifies conditional-copy notifications with the shipped storage and relay."""

from __future__ import annotations

import subprocess
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

import boto3
import httpx
from pydantic import BaseModel, Field
from testcontainers.core.container import DockerContainer
from testcontainers.core.network import Network
from testcontainers.core.waiting_utils import wait_for_logs

ROOT = Path(__file__).absolute().parents[2]
RUSTFS = "rustfs/rustfs:1.0.1@sha256:1803faef57627e2d9c2e7d89d655d712ddded5389040054987163043fecb6a3c"
BENTO = "ghcr.io/warpstreamlabs/bento:1.22.0@sha256:ef674bebae45793c982c350d8f37c2c7661133c1dc65a5517fb977b36dc09841"


class Bucket(BaseModel):
    name: str


class Object(BaseModel):
    key: str


class Location(BaseModel):
    bucket: Bucket
    object: Object


class Event(BaseModel):
    event_name: str = Field(alias="eventName")
    s3: Location


class Notification(BaseModel):
    records: tuple[Event, ...] = Field(alias="Records")


class S3NotificationTest(unittest.TestCase):
    def _ready(self, url: str) -> None:
        deadline = time.monotonic() + 60
        with httpx.Client(timeout=2) as http:
            while time.monotonic() < deadline:
                try:
                    if http.get(url).is_success:
                        return
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
        self.fail("Service did not become ready: " + url)

    def test_promoted_object_reaches_kafka(self) -> None:
        """Tests copy events separately from the ML pipeline's startup latency."""
        with Network() as network:
            collector = (
                DockerContainer("otel/opentelemetry-collector-contrib:0.162.0")
                .with_network(network)
                .with_network_aliases("otel-collector")
                .with_volume_mapping(
                    str(ROOT / "tests/e2e/fixtures/otel-collector.yaml"),
                    "/otel-collector.yaml",
                    "ro",
                )
                .with_command("--config=/otel-collector.yaml")
            )
            broker = (
                DockerContainer("redpandadata/redpanda:v26.2.2")
                .with_network(network)
                .with_network_aliases("redpanda")
                .with_command(
                    "redpanda start --mode dev-container --smp 1 --memory 512M "
                    "--kafka-addr 0.0.0.0:9092 --advertise-kafka-addr redpanda:9092"
                )
            )
            relay = (
                DockerContainer(BENTO)
                .with_network(network)
                .with_network_aliases("s3-notifications")
                .with_exposed_ports(4195)
                .with_volume_mapping(
                    str(ROOT / "infrastructure/docker/s3-notifications.yaml"),
                    "/s3-notifications.yaml",
                    "ro",
                )
                .with_command("-c /s3-notifications.yaml")
            )
            storage = (
                DockerContainer(RUSTFS)
                .with_network(network)
                .with_network_aliases("rustfs")
                .with_exposed_ports(9000)
                .with_env("RUSTFS_ACCESS_KEY", "integration-access")
                .with_env("RUSTFS_SECRET_KEY", "integration-secret")
                .with_env("RUSTFS_CHECK_UPDATE", "false")
                .with_env("RUSTFS_OBS_LOG_STDOUT_ENABLED", "true")
                .with_env("RUSTFS_NOTIFY_ENABLE", "true")
                .with_env(
                    "RUSTFS_OUTBOUND_ALLOW_ORIGINS",
                    "http://s3-notifications:4195",
                )
                .with_env("RUSTFS_NOTIFY_WEBHOOK_ENABLE_PRIMARY", "on")
                .with_env(
                    "RUSTFS_NOTIFY_WEBHOOK_ENDPOINT_PRIMARY",
                    "http://s3-notifications:4195/events",
                )
                .with_env(
                    "RUSTFS_NOTIFY_WEBHOOK_QUEUE_DIR_PRIMARY",
                    "/data/.rustfs.sys/notification-queue",
                )
                .with_command("rustfs /data")
            )
            with ExitStack() as running:
                for container in (broker, collector):
                    running.enter_context(container)
                wait_for_logs(
                    broker, "Successfully started Redpanda!", timeout=60
                )
                wrapped = broker.get_wrapped_container()
                created = subprocess.run(
                    [
                        "docker",
                        "exec",
                        wrapped.id,
                        "rpk",
                        "topic",
                        "create",
                        "s3-notification",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=40,
                    check=False,
                )
                self.assertEqual(created.returncode, 0, created.stderr)
                running.enter_context(relay)
                self._ready(
                    f"http://{relay.get_container_host_ip()}:{relay.get_exposed_port(4195)}/ready"
                )
                running.enter_context(storage)
                endpoint = f"http://{storage.get_container_host_ip()}:{storage.get_exposed_port(9000)}"
                self._ready(endpoint + "/health/ready")
                client = boto3.client(
                    "s3",
                    endpoint_url=endpoint,
                    aws_access_key_id="integration-access",
                    aws_secret_access_key="integration-secret",
                    region_name="us-east-1",
                )
                try:
                    for bucket in ("staging", "lake"):
                        client.create_bucket(Bucket=bucket)
                    client.put_bucket_notification_configuration(
                        Bucket="lake",
                        NotificationConfiguration={
                            "QueueConfigurations": [
                                {
                                    "Id": "promotions",
                                    "QueueArn": "arn:rustfs:sqs:us-east-1:primary:webhook",
                                    "Events": ["s3:ObjectCreated:*"],
                                }
                            ]
                        },
                    )
                    client.put_object(
                        Bucket="staging", Key="source", Body=b"evidence"
                    )
                    client.copy_object(
                        Bucket="lake",
                        Key="promoted",
                        CopySource="staging/source",
                        IfNoneMatch="*",
                    )
                    result = subprocess.run(
                        [
                            "docker",
                            "exec",
                            wrapped.id,
                            "rpk",
                            "topic",
                            "consume",
                            "s3-notification",
                            "-n",
                            "1",
                            "-o",
                            "start",
                            "--format",
                            "%v",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=40,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    received = Notification.model_validate_json(result.stdout)
                    self.assertEqual(
                        [
                            (
                                event.event_name,
                                event.s3.bucket.name,
                                event.s3.object.key,
                            )
                            for event in received.records
                        ],
                        [("s3:ObjectCreated:Copy", "lake", "promoted")],
                    )
                except Exception:
                    for name, container in (
                        ("storage", storage),
                        ("relay", relay),
                        ("broker", broker),
                    ):
                        print(
                            name,
                            container.get_wrapped_container()
                            .logs(tail=80)
                            .decode(errors="replace"),
                        )
                    raise
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
