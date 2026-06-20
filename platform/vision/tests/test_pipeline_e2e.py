"""End-to-end integration tests for the Vision Pipeline."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from moto import mock_aws

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.models.connectors import Connectors
from galadril_pipeline.models.pipeline import PipelineStep, StepParams
from galadril_pipeline.models.sources import Source

from galadril_vision.common.config import VisionConfig
from galadril_vision.common.types import normalize_tenant_id
from galadril_vision.pipeline import postgres_tasks
from galadril_vision.pipeline.executor import ESKGPipelineExecutor
from galadril_vision.pipeline.runner import VisionPipeline


@pytest.fixture
def mock_s3_env():
    """Start Moto to mock S3 completely in memory."""
    with mock_aws():
        import boto3
        import cv2

        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(
            Bucket="my-bucket",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )

        dummy_img = np.zeros((10, 10, 3), dtype=np.uint8)
        _, img_encoded = cv2.imencode(".jpg", dummy_img)
        s3.put_object(
            Bucket="my-bucket",
            Key="raw/images/speech.jpg",
            Body=img_encoded.tobytes(),
        )
        yield s3


@pytest.fixture
def mock_pipeline_graph():
    """Create a minimal pipeline graph configuration for testing."""
    config = PipelineConfig(
        name="test_vision_pipeline",
        connectors=Connectors(),
        sources=[Source(id="source_1", topic="test_topic")],
        pipeline=[
            PipelineStep(
                step="inf",
                type="inference",
                model="vision.FaceModel",
                input_from=["source_1"],
            ),
            PipelineStep(
                step="res",
                type="resolve",
                input_from=["inf"],
                params=StepParams.model_validate({"modality": "face"}),
            ),
            PipelineStep(
                step="snk",
                type="sink",
                input_from=["res"],
                params=StepParams.model_validate({"entity_type": "PERSON"}),
            ),
        ],
    )
    return config


class _Transaction:
    """Async transaction context manager used by the fake PostgreSQL client."""

    async def __aenter__(self) -> "_Transaction":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Connection:
    """Fake PostgreSQL connection for graph and vector batching."""

    __slots__ = ("executed",)

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _Transaction:
        """Return a no-op transaction scope."""
        return _Transaction()

    async def execute(self, query: str, params: tuple[object, ...]) -> None:
        """Record SQL statements executed by the sink helper."""
        self.executed.append((query, params))


class _PostgresClient:
    """Fake PostgreSQL client exposing the async connection context."""

    __slots__ = ("_config", "conn")

    def __init__(self, config: object) -> None:
        self._config = config
        self.conn = _Connection()

    @asynccontextmanager
    async def connection(self):
        """Yield the fake connection."""
        yield self.conn


@pytest.mark.asyncio
async def test_pipeline_end_to_end_tenant_isolation_scenario(
    mock_s3_env, mock_pipeline_graph
):
    """Verify tenant context survives the full pipeline execution path."""
    vision_config = VisionConfig.model_validate(
        {
            "name": "test-vision",
            "connectors": {
                "kafka": {
                    "brokers": ["localhost:9092"],
                    "schema_registry": "http://schema-registry:8081",
                    "consumer_group": "vision-test",
                },
                "s3": {
                    "endpoint": "http://minio:9000",
                    "access_key": "access",
                    "secret_key": "secret",
                    "region": "eu-west-1",
                    "bucket": "my-bucket",
                    "models_bucket": "models",
                },
                "postgres": {
                    "database": "galadril",
                    "host": "postgres",
                    "user": "galadril",
                    "password": "galadril",
                },
                "spicedb": {
                    "endpoint": "http://spicedb:50051",
                    "token": "token",
                },
            },
        }
    )

    target_tenant = "tenant-secure-456"
    normalized_expected_tenant = normalize_tenant_id(target_tenant)
    fake_kafka_batch = [
        (
            "test_topic",
            {
                "id": "evt_image_123",
                "tenant_id": target_tenant,
                "timestamp": 1680000000000,
                "ingested_at": 1680000000000,
                "storage_path": "images/speech.jpg",
                "source": "afp_news",
            },
        )
    ]

    mock_inference_engine = MagicMock()
    mock_prediction = MagicMock()
    mock_prediction.prediction = {
        "faces": [
            {
                "text": "Famous Person XX",
                "bbox": [10, 10, 50, 50],
                "confidence": 0.99,
                "embedding": [0.1, 0.2, 0.3, 0.4],
            }
        ]
    }
    mock_prediction.confidence = 0.99
    mock_prediction.model_version = "v1"
    mock_inference_engine.predict.return_value = mock_prediction

    mock_vector_store = AsyncMock()
    mock_vector_store.has_embeddings.return_value = True
    mock_vector_store.find_similar.return_value = [("node_777", 0.95)]
    mock_graph_store = AsyncMock()
    mock_graph_store.insert_event_on_connection = AsyncMock()
    mock_graph_store.ensure_vertex_on_connection = AsyncMock()
    mock_graph_store.create_edge_on_connection = AsyncMock()
    mock_graph_store.insert_entity_states_batch_on_connection = AsyncMock()
    mock_vector_store.store_embeddings_batch_on_connection = AsyncMock()

    mock_pg_client = _PostgresClient(vision_config.postgres)
    with (
        patch(
            "galadril_vision.pipeline.transforms._get_inference_engine",
            return_value=mock_inference_engine,
        ),
        patch(
            "galadril_vision.pipeline.postgres_tasks.get_pg_stores",
            return_value=(mock_pg_client, mock_vector_store, mock_graph_store),
        ),
    ):
        executor = ESKGPipelineExecutor(
            mock_pipeline_graph,
            vision_config,
            mock_vector_store,
            mock_graph_store,
            MagicMock(_config=vision_config.postgres),
        )
        pipeline = VisionPipeline(consumer=MagicMock(), executor=executor)
        success = await pipeline.process_batch(fake_kafka_batch)
        assert success is True

    mock_inference_engine.predict.assert_called()
    assert mock_graph_store.insert_event_on_connection.call_count == 1
    event_arg = mock_graph_store.insert_event_on_connection.call_args[0][1]
    assert event_arg.event_id == "evt_image_123"
    assert (
        normalize_tenant_id(event_arg.tenant_id) == normalized_expected_tenant
    )

    assert mock_graph_store.ensure_vertex_on_connection.call_count == 1
    vertex_arg = mock_graph_store.ensure_vertex_on_connection.call_args[0][1]
    assert vertex_arg.vertex_id == "node_777"
    assert (
        normalize_tenant_id(vertex_arg.tenant_id) == normalized_expected_tenant
    )

    assert mock_graph_store.create_edge_on_connection.call_count == 1
    edge_arg = mock_graph_store.create_edge_on_connection.call_args[0][1]
    assert normalize_tenant_id(edge_arg.tenant_id) == normalized_expected_tenant

    assert (
        mock_graph_store.insert_entity_states_batch_on_connection.call_count
        == 1
    )
    states_batch = (
        mock_graph_store.insert_entity_states_batch_on_connection.call_args[0][
            1
        ]
    )
    assert len(states_batch) == 1
    assert states_batch[0].entity_id == "node_777"
    assert states_batch[0].state_value["confidence"] == 0.99
    assert (
        normalize_tenant_id(states_batch[0].tenant_id)
        == normalized_expected_tenant
    )

    assert (
        mock_vector_store.store_embeddings_batch_on_connection.call_count == 1
    )
    vector_batch = (
        mock_vector_store.store_embeddings_batch_on_connection.call_args[0][1]
    )
    assert len(vector_batch) == 1

    emb_record, entity_id = vector_batch[0]
    assert entity_id == "node_777"
    assert (
        normalize_tenant_id(emb_record.tenant_id) == normalized_expected_tenant
    )
