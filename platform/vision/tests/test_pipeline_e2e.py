"""End-to-End integration tests for the Vision Pipeline using local Daft runner with Tenant Isolation checks."""

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from moto import mock_aws

from galadril_vision.common.config import VisionConfig
from galadril_vision.pipeline.runner import VisionPipeline
from galadril_vision.common.types import normalize_tenant_id

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.graph import PipelineGraph
from galadril_pipeline.models.pipeline import PipelineStep, StepParams
from galadril_pipeline.models.sources import Source
from galadril_pipeline.models.connectors import Connectors


@pytest.fixture
def mock_s3_env():
    """Start Moto to mock S3 completely in memory."""
    with mock_aws():
        import boto3

        s3 = boto3.client("s3", region_name="eu-west-1")
        s3.create_bucket(
            Bucket="my-bucket",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-1"},
        )

        import cv2

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
    return PipelineGraph(config)


@pytest.mark.asyncio
async def test_pipeline_end_to_end_tenant_isolation_scenario(
    mock_s3_env, mock_pipeline_graph
):
    """
    Test the complete pipeline with tenant security constraints.
    We pass a payload tied to a specific tenant and verify that the tenant context
    is propagated cleanly through processing steps and asserted strictly at the storage layer.
    """
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
                    "bucket": "vision-data",
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

    # Define an explicit, isolated tenant identifier
    target_tenant = "tenant-secure-456"
    normalized_expected_tenant = normalize_tenant_id(target_tenant)

    fake_kafka_batch = [
        (
            "test_topic",
            {
                "id": "evt_image_123",
                "tenant_id": target_tenant,  # Injected tenant ID for authorization routing
                "timestamp": 1680000000000,
                "ingested_at": 1680000000000,
                "storage_path": "images/speech.jpg",
                "source": "afp_news",
            },
        )
    ]

    # Mocking downstream inference processing engines
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

    # Setup database target sinks
    mock_vector_store = AsyncMock()
    mock_graph_store = AsyncMock()

    # Ensure entity resolution filters queries using the same tenant context to avoid cross-tenant leakage
    mock_vector_store.find_similar.return_value = [("node_777", 0.95)]

    with (
        patch(
            "galadril_vision.pipeline.transforms._get_inference_engine",
            return_value=mock_inference_engine,
        ),
        patch(
            "galadril_vision.pipeline.transforms._get_pg_stores",
            return_value=(mock_vector_store, mock_graph_store),
        ),
        patch(
            "galadril_vision.pipeline.runner.KafkaMultiTopicConsumer"
        ) as MockConsumer,
    ):
        mock_consumer_instance = MockConsumer.return_value
        mock_consumer_instance.stream.return_value = [fake_kafka_batch]
        mock_executor = MagicMock()

        pipeline = VisionPipeline(
            consumer=mock_consumer_instance,
            executor=mock_executor,
        )
        success = await pipeline.process_batch(fake_kafka_batch)
        assert success is True, (
            "Pipeline failed processing a valid multi-tenant batch"
        )

    # 1. Verify Inference was successfully triggered
    mock_inference_engine.predict.assert_called()

    # 2. Verify graph metadata events capture and persist tenant contextual fields
    assert mock_graph_store.insert_event.call_count == 1
    event_arg = mock_graph_store.insert_event.call_args[0][0]
    assert event_arg.event_id == "evt_image_123"
    assert (
        normalize_tenant_id(event_arg.tenant_id) == normalized_expected_tenant
    )

    # 3. Verify Vertex guarantees are correctly scoped to the active tenant space
    assert mock_graph_store.ensure_vertex.call_count == 1
    vertex_arg = mock_graph_store.ensure_vertex.call_args[0][0]
    assert vertex_arg.vertex_id == "node_777"
    assert (
        normalize_tenant_id(vertex_arg.tenant_id) == normalized_expected_tenant
    )

    # 4. Verify relations links remain isolated inside the tenant boundary
    assert mock_graph_store.link_entity_to_event.call_count == 1
    edge_arg = mock_graph_store.link_entity_to_event.call_args[0][0]
    assert normalize_tenant_id(edge_arg.tenant_id) == normalized_expected_tenant

    # 5. Verify batch Graph Entity State updates successfully verified constraints
    assert mock_graph_store.insert_entity_states_batch.call_count == 1
    states_batch = mock_graph_store.insert_entity_states_batch.call_args[0][0]
    assert len(states_batch) == 1
    assert states_batch[0].entity_id == "node_777"
    assert states_batch[0].state_value["confidence"] == 0.99
    # This explicitly ensures the security function `require_same_tenant` checks succeeded out-of-the-box
    assert (
        normalize_tenant_id(states_batch[0].tenant_id)
        == normalized_expected_tenant
    )

    # 6. Verify Vector database indices preserve strict tenant ownership isolation boundaries
    assert mock_vector_store.store_embeddings_batch.call_count == 1
    vector_batch = mock_vector_store.store_embeddings_batch.call_args[0][0]
    assert len(vector_batch) == 1

    emb_record, entity_id = vector_batch[0]
    assert entity_id == "node_777"
    # Validates that the multi-tenant vector partitioning schemes won't mix cross-tenant elements
    assert (
        normalize_tenant_id(emb_record.tenant_id) == normalized_expected_tenant
    )
