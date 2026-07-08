"""Configuration utility for the Dagster orchestration pipeline."""

from galadril_vision.common.config import VisionConfig


def load_vision_config(pipeline_path: str = "bootstrap.yaml") -> VisionConfig:
    """Parses and returns the core VisionConfig object synchronously."""
    return VisionConfig.from_yaml(pipeline_path)
