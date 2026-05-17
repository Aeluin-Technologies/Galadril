from __future__ import annotations

from typing import TYPE_CHECKING, Any
import structlog
import daft

from galadril_vision.pipeline.transforms import (
    download_images_udf,
    run_inference_udf,
    resolve_entities_udf,
    sink_to_db_udf,
)

if TYPE_CHECKING:
    from galadril_pipeline.config import PipelineConfig
    from galadril_vision.common.config import VisionConfig

logger = structlog.get_logger(__name__)


class ESKGPipelineExecutor:
    """Executes the pipeline completely in distributed Ray space."""

    def __init__(
        self,
        config: PipelineConfig,
        vision_config: VisionConfig,
    ) -> None:
        self.config = config
        self.vision_config = vision_config

    async def execute_batch(self, batch: list[dict[str, Any]]) -> None:
        """Process a batch through the DAG."""
        if not batch:
            return

        df = daft.from_pylist(batch)

        if "source" not in df.column_names:
            df = df.with_column("source", daft.lit("unknown"))

        if "storage_path" in df.column_names:
            df = df.with_column(
                "image_data",
                download_images_udf(
                    df["storage_path"],
                    df["record_id"],
                    bucket=self.vision_config.image_store.bucket,
                    prefix=self.vision_config.image_store.prefix,
                    endpoint_url=self.vision_config.image_store.endpoint_url,
                ),
            )

        for step in self.config.pipeline:
            if step.type == "inference":
                model_name = step.model.split(".")[-1].lower()
                action = (
                    step.params.get("action", "embed")
                    if step.params
                    else "embed"
                )
                df = df.with_column(
                    f"{step.step}_result",
                    run_inference_udf(
                        df["image_data"],
                        df["record_id"],
                        artifact_bucket=self.vision_config.inference.bucket,
                        artifact_prefix=self.vision_config.inference.prefix,
                        artifact_endpoint_url=self.vision_config.inference.endpoint_url,
                        model_name=model_name,
                        action=action,
                    ),
                )

            elif step.type == "resolve":
                modality = (
                    step.params.get("modality", "face")
                    if step.params
                    else "face"
                )
                threshold = (
                    step.params.get("threshold", 0.8) if step.params else 0.8
                )
                input_col = f"{step.input_from[0]}_result"

                df = df.with_column(
                    f"{step.step}_resolved",
                    resolve_entities_udf(
                        df[input_col],
                        postgres_dsn=str(self.vision_config.postgres.dsn),
                        modality=modality,
                        threshold=threshold,
                    ),
                )

            elif step.type == "sink":
                input_col = f"{step.input_from[0]}_resolved"
                entity_type = (
                    step.params.get("entity_type", "PERSON")
                    if step.params
                    else "PERSON"
                )
                modality = (
                    step.params.get("modality", "face")
                    if step.params
                    else "face"
                )

                df = df.with_column(
                    f"{step.step}_status",
                    sink_to_db_udf(
                        df[input_col],
                        df["record_id"],
                        df["source"],
                        postgres_dsn=str(self.vision_config.postgres.dsn),
                        entity_type=entity_type,
                        modality=modality,
                    ),
                )

        df.collect()
        logger.info("distributed_batch_executed", size=len(batch))
