"""Infrastructure resources and connector bindings for Dagster execution."""

from __future__ import annotations

from galadril_pipeline.resources.causal import CausalRunnerResource
from galadril_pipeline.resources.config import VisionConfigResource
from galadril_pipeline.resources.kafka import KafkaResource, VisionKafkaResource
from galadril_pipeline.resources.postgres import PostgresResource
from galadril_pipeline.resources.s3 import S3ClientResource

__all__ = [
    "CausalRunnerResource",
    "VisionConfigResource",
    "KafkaResource",
    "VisionKafkaResource",
    "PostgresResource",
    "S3ClientResource",
]
