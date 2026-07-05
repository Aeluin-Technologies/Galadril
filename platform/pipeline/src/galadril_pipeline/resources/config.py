"""Configuration resources for the Dagster orchestration pipeline."""

from functools import cached_property
import dagster as dg
from galadril_pipeline.config import PipelineConfig
from galadril_vision.common.config import VisionConfig


class VisionConfigResource(dg.ConfigurableResource):
    """Orchestration resource wrapping cached platform settings parsing logic."""

    pipeline_path: str = "bootstrap.yaml"

    @cached_property
    def vision_config(self) -> VisionConfig:
        """Parses and returns the core VisionConfig object."""
        return VisionConfig.from_yaml(self.pipeline_path)

    @cached_property
    def pipeline_config(self) -> PipelineConfig:
        """Converts VisionConfig to PipelineConfig."""
        return self.vision_config.to_pipeline_config()
