"""Loads declarative pipeline configuration and compiles streaming routes."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from galadril_pipeline.config import PipelineConfig
from galadril_pipeline.routing import PipelineRouteTable


class PipelineParser:
    """Parses pipeline YAML into validated real-time routing structures."""

    @staticmethod
    def from_yaml(file_path: str | Path) -> PipelineConfig:
        """Loads and validates a pipeline configuration from YAML."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Configuration file target missing: {path}"
            )
        with path.open("r", encoding="utf-8") as file:
            data = yaml.safe_load(file)
        if data is None:
            raise ValueError(f"Empty context matching: {path}")
        try:
            return PipelineConfig.model_validate(data)
        except ValidationError as error:
            raise ValueError(
                f"Validation failure across model properties:\n{error}"
            ) from error

    @staticmethod
    def compile_routes(config: PipelineConfig) -> PipelineRouteTable:
        """Compiles constant-time FastStream routes from validated configuration."""
        return PipelineRouteTable(config)
