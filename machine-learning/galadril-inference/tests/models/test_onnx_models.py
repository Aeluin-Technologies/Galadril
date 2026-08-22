"""Focused unit tests for allocation-light ONNX model helpers."""

from __future__ import annotations

import numpy as np
import pytest
from galadril_inference.models.embedding.siglip import SigLIPModel
from galadril_inference.models.image.grounded_sam import GroundedSamModel
from galadril_inference.models.image.owl import OwlV2Model


def test_owl_scales_and_clamps_center_boxes() -> None:
    """Normalized detector boxes should become valid image-space corners."""
    boxes = np.asarray(
        [[0.5, 0.5, 0.5, 0.5], [0.0, 0.0, 0.5, 0.5]],
        dtype=np.float32,
    )

    scaled = OwlV2Model._scale_boxes(boxes, width=200, height=100)

    np.testing.assert_allclose(scaled[0], [50.0, 25.0, 150.0, 75.0])
    np.testing.assert_allclose(scaled[1], [0.0, 0.0, 50.0, 25.0])


def test_grounded_sam_nms_suppresses_overlapping_lower_score() -> None:
    """NMS should keep the strongest overlap while retaining separate boxes."""
    boxes = np.asarray(
        [
            [0.0, 0.0, 10.0, 10.0],
            [1.0, 1.0, 9.0, 9.0],
            [20.0, 20.0, 30.0, 30.0],
        ],
        dtype=np.float32,
    )
    scores = np.asarray([0.9, 0.5, 0.7], dtype=np.float32)

    assert GroundedSamModel._nms(boxes, scores, threshold=0.4) == [0, 2]


def test_grounded_sam_finds_prompt_token_subsequence() -> None:
    """Prompt alignment should continue from the previous concept span."""
    values = [101, 12, 13, 14, 12, 13, 102]

    assert GroundedSamModel._find_subsequence(values, [12, 13], 3) == (4, 6)
    assert GroundedSamModel._find_subsequence(values, [99], 0) is None


def test_siglip_normalizes_embedding_in_place() -> None:
    """Embedding normalization should reuse the model output buffer."""
    embedding = np.asarray([3.0, 4.0], dtype=np.float32)

    normalized = SigLIPModel._normalize(embedding)

    assert normalized is embedding
    np.testing.assert_allclose(normalized, [0.6, 0.8])


@pytest.mark.parametrize(
    ("model", "compute_type"),
    [(SigLIPModel, "bad"), (OwlV2Model, "bad"), (GroundedSamModel, "bad")],
)
def test_onnx_models_reject_unknown_quantization(
    model: type[SigLIPModel] | type[OwlV2Model] | type[GroundedSamModel],
    compute_type: str,
) -> None:
    """Unknown artifact precision must fail before any network or file access."""
    with pytest.raises(ValueError, match="Unsupported"):
        model._compute_suffix(compute_type)
