"""Transforms structural definitions into functional runtime orchestration topologies."""

from __future__ import annotations

from pathlib import Path
import dagster as dg
import yaml
from pydantic import ValidationError

from galadril_pipeline.compiler.assets import AssetCompilerFactory
from galadril_pipeline.config import PipelineConfig


class PipelineParser:
    """Parses and compiles pipeline configurations into executable topologies."""

    @staticmethod
    def from_yaml(file_path: str | Path) -> PipelineConfig:
        """Loads and validates a pipeline configuration from a YAML file.

        Args:
            file_path: The filesystem path to the YAML configuration file.

        Returns:
            The validated PipelineConfig instance.

        Raises:
            FileNotFoundError: If the specified file does not exist.
            ValueError: If the file is empty or fails Pydantic schema validation.
        """
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
        except ValidationError as exc:
            raise ValueError(
                f"Validation failure across model properties:\n{exc}"
            ) from exc

    @classmethod
    def to_dagster_definitions(cls, config: PipelineConfig) -> dg.Definitions:
        """Compiles a validated pipeline configuration into unified Dagster definitions.

        Args:
            config: The validated PipelineConfig configuration instance.

        Returns:
            A Dagster Definitions object containing the compiled pipeline assets.
        """
        assets: list[dg.AssetsDefinition] = []

        topological_order = config.get_topological_order()

        for source in config.sources:
            assets.append(AssetCompilerFactory.build_source_asset(source))

        for step in config.pipeline:
            topological_index = topological_order.index(step.step)
            assets.append(
                AssetCompilerFactory.build_pipeline_asset(
                    step, topological_index
                )
            )

        return dg.Definitions(
            assets=assets,
        )
