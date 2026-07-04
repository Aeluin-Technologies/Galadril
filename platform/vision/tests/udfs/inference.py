"""Unit tests for the model inference batch execution logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
from daft import Series
from galadril_vision.compute.udfs.inference import (
    CustomS3Loader,
    _get_inference_engine,
    run_inference_udf,
)


class TestCustomS3Loader:
    """Verifies interface structural configurations on CustomS3Loader."""

    def test_subclass_instantiation(self) -> None:
        """Validates backwards-compatible S3 inheritance parameters."""
        with patch(
            "galadril_inference.storage.s3.S3Loader.__init__"
        ) as mock_init:
            CustomS3Loader(bucket="b", prefix="p", endpoint_url="e")
            mock_init.assert_called_once_with(
                bucket="b", prefix="p", endpoint_url="e"
            )


class TestInferenceEngineCache:
    """Verifies isolation, caching routines and exception setups."""

    @pytest.mark.asyncio
    async def test_get_inference_engine_lifecycle(self) -> None:
        """Verifies cached engines share memory addresses on subsequent lookups."""
        with (
            patch(
                "galadril_vision.compute.udfs.inference._INFERENCE_ENGINES", {}
            ),
            patch(
                "galadril_vision.compute.udfs.inference.InferenceEngine"
            ) as mock_engine_cls,
        ):
            mock_engine = MagicMock()
            mock_engine.load_model = AsyncMock()
            mock_engine_cls.return_value = mock_engine

            engine_one = await _get_inference_engine("model_a", "b", "p", "e")
            engine_two = await _get_inference_engine("model_a", "b", "p", "e")

            assert engine_one == mock_engine
            assert engine_two == mock_engine
            mock_engine.load_model.assert_called_once_with("model_a")


class TestRunInferenceUdf:
    """Tests the run_inference_udf function across processing scenarios."""

    @pytest.mark.asyncio
    async def test_critical_engine_initialization_failure(self) -> None:
        """Ensures failure exceptions map to structured RuntimeError bubbles."""
        raw_items = Series.from_pylist([{"data": "val"}])
        record_ids = Series.from_pylist(["id_1"])

        with patch(
            "galadril_vision.compute.udfs.inference._get_inference_engine",
            side_effect=ValueError("Load Err"),
        ):
            with pytest.raises(
                RuntimeError,
                match="Critical: Failed to initialize Inference Engine",
            ):
                await run_inference_udf(
                    raw_items,
                    record_ids,
                    model_name="m",
                    models_bucket="b",
                    models_prefix="p",
                )

    @pytest.mark.asyncio
    async def test_engine_resolves_to_none(self) -> None:
        """Ensures that resolving to a None engine triggers an explicit error."""
        raw_items = Series.from_pylist([{"data": "val"}])
        record_ids = Series.from_pylist(["id_1"])

        with patch(
            "galadril_vision.compute.udfs.inference._get_inference_engine",
            return_value=None,
        ):
            with pytest.raises(
                RuntimeError, match="Inference Engine resolved to None"
            ):
                await run_inference_udf(
                    raw_items,
                    record_ids,
                    model_name="m",
                    models_bucket="b",
                    models_prefix="p",
                )

    @pytest.mark.asyncio
    async def test_process_batch_with_skips_and_success(self) -> None:
        """Evaluates batch computations processing empty rows alongside structural objects."""
        raw_items = Series.from_pylist(
            [
                None,
                {
                    "data": "text_payload",
                    "modality": "text",
                    "mime_type": "text/plain",
                    "storage_path": "s3://a",
                    "metadata": {},
                    "raw_payload": {},
                },
                np.array([1, 2, 3]),
            ]
        )
        record_ids = Series.from_pylist(["id_none", "id_dict", "id_raw"])

        mock_engine = MagicMock()
        mock_prediction = MagicMock()
        mock_prediction.prediction = "pred_val"
        mock_prediction.confidence = 0.95
        mock_prediction.model_version = "v1"
        mock_engine.predict.return_value = mock_prediction

        with (
            patch(
                "galadril_vision.compute.udfs.inference._get_inference_engine",
                return_value=mock_engine,
            ),
            patch(
                "galadril_vision.compute.udfs.inference._normalize_data_modality",
                return_value="text",
            ),
        ):
            results = await run_inference_udf(
                raw_items,
                record_ids,
                model_name="m",
                models_bucket="b",
                models_prefix="p",
            )

            assert len(results) == 3
            assert results[0] == {
                "record_id": "id_none",
                "error": "No raw data",
            }
            assert results[1]["record_id"] == "id_dict"
            assert results[1]["prediction"] == "pred_val"
            assert results[1]["raw_modality"] == "text"
            assert results[2]["record_id"] == "id_raw"
            assert results[2]["raw_modality"] == "image"

    @pytest.mark.asyncio
    async def test_process_item_runtime_failure(self) -> None:
        """Validates that nested execution runtime exceptions format traceback logs."""
        raw_items = Series.from_pylist([{"data": "bad_data"}])
        record_ids = Series.from_pylist(["id_fail"])

        mock_engine = MagicMock()
        mock_engine.predict.side_effect = TypeError("Internal Predict Fail")

        with patch(
            "galadril_vision.compute.udfs.inference._get_inference_engine",
            return_value=mock_engine,
        ):
            results = await run_inference_udf(
                raw_items,
                record_ids,
                model_name="m",
                models_bucket="b",
                models_prefix="p",
            )
            assert len(results) == 1
            assert results[0]["record_id"] == "id_fail"
            assert "Internal Predict Fail" in results[0]["error"]
