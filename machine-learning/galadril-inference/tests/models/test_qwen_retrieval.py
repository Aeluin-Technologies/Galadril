"""Embedding profiles cannot silently truncate, change pooling or advertise vision."""

import sys
import types
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from galadril_inference.common.exceptions import SchemaValidationError
from galadril_inference.common.types import PredictionRequest
from galadril_inference.models.embedding.qwen3 import (
    EmbeddingContext,
    Qwen3EmbeddingModel,
)


class Context:
    def __init__(self, output: object) -> None:
        self.output = output
        self.inputs: list[str] = []

    def tokenize(
        self, text: bytes, *, add_bos: bool = True, special: bool = False
    ) -> list[int]:
        return [1, 2, 3, 4]

    def embed(
        self, text: str, *, normalize: bool = False, truncate: bool = True
    ) -> object:
        assert normalize is False and truncate is False
        self.inputs.append(text)
        return self.output

    def close(self) -> None:
        return None


def test_pooled_native_batch_can_hold_the_complete_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    artifact = tmp_path / "model.gguf"
    artifact.write_bytes(b"weights")
    received: dict[str, object] = {}

    def native(**kwargs: object) -> Context:
        received.update(kwargs)
        return Context([1.0, 0.0])

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        types.SimpleNamespace(hf_hub_download=lambda **kwargs: str(artifact)),
    )
    monkeypatch.setitem(
        sys.modules, "llama_cpp", types.SimpleNamespace(Llama=native)
    )
    Qwen3EmbeddingModel().load(str(tmp_path), n_ctx=8192)
    assert received["n_batch"] == received["n_ubatch"] == 8192


def test_historical_q6_artifacts_are_not_silently_replaced_by_another_space() -> (
    None
):
    assert Qwen3EmbeddingModel.artifact("0.6b", "q6_k") == (
        "batiai/Qwen3-Embedding-0.6B-GGUF",
        "Qwen3-Embedding-0.6B-Q6_K.gguf",
    )
    assert (
        Qwen3EmbeddingModel.artifact("8b", "q8_0")[0]
        == "Qwen/Qwen3-Embedding-8B-GGUF"
    )


def model(
    output: object, *, mode: str = "pooled"
) -> tuple[Qwen3EmbeddingModel, Context]:
    instance = Qwen3EmbeddingModel()
    context = Context(output)
    instance._ctx = cast(EmbeddingContext, context)
    instance._embedding_mode = mode
    return instance, context


def test_query_instructions_and_document_vectors_share_explicit_profile() -> (
    None
):
    instance, context = model([3.0, 4.0])
    result = instance.predict(
        PredictionRequest(
            model_name="qwen_embedding",
            features={
                "text": "appeal",
                "task": "Retrieve legal passages",
                "dimensions": 2,
            },
        )
    )
    np.testing.assert_allclose(result.prediction["embedding"], [0.6, 0.8])
    assert context.inputs == [
        "Instruct: Retrieve legal passages\nQuery: appeal"
    ]
    assert result.prediction["pooling"] == "last"
    assert "vision" not in instance.meta().description.lower()
    assert "multimodal" not in instance.meta().description.lower()


def test_late_chunking_pools_contextual_token_spans_and_preserves_locations() -> (
    None
):
    instance, _ = model(
        [[3.0, 0.0], [1.0, 0.0], [0.0, 2.0], [0.0, 4.0]], mode="tokens"
    )
    result = instance.predict(
        PredictionRequest(
            model_name="qwen_embedding",
            features={
                "text": "whole document",
                "dimensions": 2,
                "token_spans": [[0, 2], [2, 4]],
            },
        )
    )
    assert result.prediction["embeddings"] == [[1.0, 0.0], [0.0, 1.0]]
    assert result.prediction["token_spans"] == [[0, 2], [2, 4]]
    assert result.prediction["pooling"] == "late-mean"
    query = instance.predict(
        PredictionRequest(
            model_name="qwen_embedding",
            features={"text": "query", "dimensions": 2},
        )
    )
    assert query.prediction["pooling"] == "late-mean"


@pytest.mark.parametrize(
    "features",
    [
        {"text": "x", "dimensions": 0},
        {"text": "x", "dimensions": True},
        {"text": "x", "dimensions": 1025},
        {"text": "x", "token_spans": [[0, 2]]},
        {"text": "x", "image": "unsupported"},
        {"text": 123},
    ],
)
def test_invalid_features_fail_before_native_inference(
    features: dict[str, object],
) -> None:
    instance, context = model([1.0, 0.0])
    with pytest.raises(SchemaValidationError):
        instance.predict(
            PredictionRequest(model_name="qwen_embedding", features=features)
        )
    assert not context.inputs


@pytest.mark.parametrize(
    "output", [[float("nan"), 1.0], [0.0, 0.0], [[1.0, 0.0]]]
)
def test_invalid_native_vectors_do_not_enter_an_index(output: object) -> None:
    instance, _ = model(output)
    with pytest.raises(SchemaValidationError):
        instance.predict(
            PredictionRequest(
                model_name="qwen_embedding",
                features={"text": "x", "dimensions": 2},
            )
        )


def test_late_chunking_rejects_missing_tokens_and_invalid_spans() -> None:
    instance, _ = model([[1.0, 0.0], [1.0, 0.0]], mode="tokens")
    with pytest.raises(SchemaValidationError):
        instance.predict(
            PredictionRequest(
                model_name="qwen_embedding",
                features={
                    "text": "x",
                    "dimensions": 2,
                    "token_spans": [[0, 4]],
                },
            )
        )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__]))
