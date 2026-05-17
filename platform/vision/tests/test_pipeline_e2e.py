"""End-to-End integration tests for the Vision Pipeline using local Daft runner."""

from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from moto import mock_aws

from galadril_vision.common.config import VisionConfig
from galadril_vision.pipeline.runner import VisionPipeline

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
async def test_pipeline_end_to_end_scenario(mock_s3_env, mock_pipeline_graph):
    """
    Test the pipeline by passing an image and a text. We check if Daft executes
    the DAG and correctly calls the DB mock insertion.
    """
    vision_config = VisionConfig()

    fake_kafka_batch = [
        (
            "test_topic",
            {
                "id": "evt_image_123",
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
    mock_graph_store = AsyncMock()
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

        async with VisionPipeline(
            vision_config, mock_pipeline_graph
        ) as pipeline:
            await pipeline.process_batch(fake_kafka_batch)

    mock_inference_engine.predict.assert_called()

    assert mock_graph_store.insert_event.call_count == 1
    event_arg = mock_graph_store.insert_event.call_args[0][0]
    assert "evt_image_123" in event_arg.event_id

    assert mock_graph_store.ensure_vertex.call_count == 1
    vertex_arg = mock_graph_store.ensure_vertex.call_args[0][0]
    assert vertex_arg.vertex_id == "node_777"

    assert mock_graph_store.link_entity_to_event.call_count == 1

    assert mock_graph_store.insert_entity_states_batch.call_count == 1
    states_batch = mock_graph_store.insert_entity_states_batch.call_args[0][0]
    assert len(states_batch) == 1
    assert states_batch[0].entity_id == "node_777"
    assert states_batch[0].state_value["confidence"] == 0.99

    assert mock_vector_store.store_embeddings_batch.call_count == 1
    vector_batch = mock_vector_store.store_embeddings_batch.call_args[0][0]
    assert len(vector_batch) == 1
    assert vector_batch[0][1] == "node_777"
