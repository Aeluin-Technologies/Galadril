"""Multimodal retrieval must pool the trained token and actually consume pixels."""

import io
from typing import cast

import pytest
import torch
from galadril_inference.models.embedding.qwen_vl import (
    Encoder,
    Processor,
    Qwen3VLEmbeddingModel,
)
from PIL import Image


class Output:
    last_hidden_state = torch.tensor(
        [
            [
                [9.0, 0.0] + [0.0] * 62 + [7.0],
                [3.0, 4.0] + [0.0] * 62 + [8.0],
                [0.0, 9.0] + [0.0] * 62 + [9.0],
            ]
        ]
    )


class FakeEncoder:
    device = torch.device("cpu")

    def __call__(self, **inputs: object) -> Output:
        assert inputs["use_cache"] is False
        return Output()


class FakeProcessor:
    def __init__(self) -> None:
        self.messages: object = None
        self.inputs: dict[str, object] = {}

    def apply_chat_template(self, messages: object, **options: object) -> str:
        self.messages = messages
        assert options == {"tokenize": False, "add_generation_prompt": True}
        return "formatted"

    def __call__(self, **inputs: object) -> dict[str, torch.Tensor]:
        self.inputs = inputs
        return {
            "input_ids": torch.tensor([[1, 2, 0]]),
            "attention_mask": torch.tensor([[1, 1, 0]]),
        }


def model() -> tuple[Qwen3VLEmbeddingModel, FakeProcessor]:
    instance = Qwen3VLEmbeddingModel(dimensions=64)
    processor = FakeProcessor()
    instance._encoder = cast(Encoder, FakeEncoder())
    instance._processor = cast(Processor, processor)
    return instance, processor


def test_last_nonpadding_pooling_then_mrl_truncation_and_normalization() -> (
    None
):
    instance, processor = model()
    assert instance.embed("chat") == pytest.approx((0.6, 0.8) + (0.0,) * 62)
    assert processor.inputs["truncation"] is False
    assert processor.inputs["images"] is None


def test_pixels_and_ocr_share_the_same_embedding_profile() -> None:
    instance, processor = model()
    content = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(content, format="PNG")
    profile = instance.profile_id
    instance.embed("ZB679 ARMY", image=content.getvalue())
    images = processor.inputs["images"]
    assert isinstance(images, list) and len(images) == 1
    assert isinstance(images[0], Image.Image)
    assert images[0].size == (40, 20)
    assert "ZB679 ARMY" in str(processor.messages)
    instance.embed("helicopter", query=True)
    assert instance.profile_id == profile


def test_corrupt_images_never_fall_back_to_text_embeddings() -> None:
    instance, _ = model()
    with pytest.raises(ValueError, match="Image decoding rejected"):
        instance.embed("cat", image=b"not an image")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
