"""Configuration loader and Dagster asset definition compiler."""

from __future__ import annotations
from pathlib import Path
import dagster as dg
import yaml
from pydantic import ValidationError

from galadril_pipeline.compiler.assets import AssetCompilerFactory
from galadril_pipeline.config import PipelineConfig


class PipelineParser:
    """Loads and transforms raw configuration files into executable runtime structures."""

    @staticmethod
    def from_yaml(file_path: str | Path) -> PipelineConfig:
        """Loads and validates a YAML configuration file against the system schema.

        Args:
            file_path: The filesystem path to the target YAML configuration file.

        Returns:
            A validated PipelineConfig configuration instance.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(
                f"Configuration file does not exist: {path}"
            )

        try:
            with path.open("r", encoding="utf-8") as file:
                data = yaml.safe_load(file)
        except yaml.YAMLError as exc:
            raise ValueError(
                f"Invalid YAML configuration in '{path}'."
            ) from exc

        if data is None:
            raise ValueError(f"Configuration file '{path}' is empty.")

        try:
            return PipelineConfig.model_validate(data)
        except ValidationError as exc:
            raise ValueError(
                f"Configuration validation failed:\n{exc}"
            ) from exc

    @classmethod
    def to_dagster_definitions(cls, config: PipelineConfig) -> dg.Definitions:
        """Compiles a validated pipeline configuration into unified Dagster definitions.

        Args:
            config: The validated PipelineConfig configuration instance.

        Returns:
            A Dagster Definitions object ready to be loaded by the execution engine.
        """
        assets: list[dg.AssetsDefinition] = []
        schedules: list[dg.ScheduleDefinition] = []

        topological_order = config.get_topological_order()

        for source in config.sources:
            assets.append(AssetCompilerFactory.build_source_asset(source))

        for step in config.pipeline:
            # Resolves sequence index to support external tracking layers.
            topological_index = topological_order.index(step.step)
            assets.append(
                AssetCompilerFactory.build_pipeline_asset(
                    step, topological_index
                )
            )

            schedule = AssetCompilerFactory.build_schedule(step)
            if schedule is not None:
                schedules.append(schedule)

        return dg.Definitions(
            assets=assets,
            schedules=schedules,
        )
