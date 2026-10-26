"""Explicit live smoke test; normal CI never downloads model weights."""

import math
import os
import time
from pathlib import Path

import pytest
from galadril_inference.models.embedding.qwen_vl import Qwen3VLEmbeddingModel
from huggingface_hub import hf_hub_download


def test_real_pixels_distinguish_cats_helicopter_and_military_scene(
    tmp_path: Path,
) -> None:
    root = (
        Path(os.environ["TEST_SRCDIR"])
        / "_main/machine-learning/galadril-inference/examples/images"
    )
    model = Qwen3VLEmbeddingModel()
    started = time.monotonic()
    model.load(str(tmp_path / "models"))
    cats = Path(
        hf_hub_download(
            repo_id="huggingface/documentation-images",
            repo_type="dataset",
            filename="coco_sample.png",
            revision="88a3af27faf4713e57cec838bd1b9f15b2cf3f4e",
            cache_dir=str(tmp_path / "samples"),
        )
    )
    vectors = tuple(
        model.embed("P", image=path.read_bytes())
        for path in (cats, root / "gazelle.png", root / "military_camp.png")
    )
    for query, expected in (
        ("chat", 0),
        ("cat", 0),
        ("hélicoptère", 1),
        ("helicopter", 1),
    ):
        vector = model.embed(query, query=True)
        scores = [
            sum(
                left * right
                for left, right in zip(vector, candidate, strict=True)
            )
            for candidate in vectors
        ]
        assert max(range(len(scores)), key=scores.__getitem__) == expected, (
            query,
            scores,
        )
        assert scores[expected] >= 0.3, (query, scores)
        assert math.isclose(
            sum(value * value for value in vector), 1.0, abs_tol=1e-5
        )
        print({"query": query, "similarities": scores})
    print(
        {
            "profile": model.profile_id,
            "elapsed_seconds": time.monotonic() - started,
        }
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-s"]))
