"""Pinned Qwen3-VL embeddings use trained last-token pooling for text and pixels."""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING, Protocol, cast

import structlog

if TYPE_CHECKING:
    import torch

_REPO = "Qwen/Qwen3-VL-Embedding-2B"
_REVISION = "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda"
_INSTRUCTION = "Represent the user's input."
_MAX_LENGTH = 8192
_MAX_PIXELS = 262144
_log = structlog.get_logger(__name__)


class Encoded(Protocol):
    last_hidden_state: torch.Tensor


class Encoder(Protocol):
    device: torch.device

    def __call__(self, **inputs: object) -> Encoded: ...


class Processor(Protocol):
    def apply_chat_template(
        self, messages: object, **options: object
    ) -> str: ...
    def __call__(self, **inputs: object) -> dict[str, torch.Tensor]: ...


class Qwen3VLEmbeddingModel:
    __slots__ = ("dimensions", "_encoder", "_processor")

    def __init__(self, *, dimensions: int = 1024) -> None:
        if not 64 <= dimensions <= 2048:
            raise ValueError("Unsupported Qwen3-VL embedding dimension")
        self.dimensions = dimensions
        self._encoder: Encoder | None = None
        self._processor: Processor | None = None

    @property
    def profile_id(self) -> str:
        return hashlib.sha256(
            f"{_REPO}:{_REVISION}:bf16:{self.dimensions}:{_MAX_LENGTH}:{_MAX_PIXELS}:{_INSTRUCTION}:last-token:mrl-l2:v1".encode()
        ).hexdigest()

    def load(self, path: str) -> None:
        """Load the backbone directly; no generative head or remote Python is needed."""
        if self._encoder is not None:
            return
        import torch
        from huggingface_hub import snapshot_download
        from transformers import Qwen3VLModel, Qwen3VLProcessor

        snapshot = snapshot_download(
            repo_id=_REPO,
            revision=_REVISION,
            cache_dir=path,
            allow_patterns=[
                "*.safetensors",
                "*.json",
                "*.jinja",
                "merges.txt",
                "tokenizer.model",
            ],
        )
        encoder, loading = Qwen3VLModel.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
            dtype=torch.bfloat16,
            attn_implementation="sdpa",
            output_loading_info=True,
        )
        if (
            loading.get("missing_keys")
            or loading.get("unexpected_keys")
            or loading.get("mismatched_keys")
        ):
            raise ValueError(
                "Qwen3-VL checkpoint does not match the embedding backbone"
            )
        encoder.eval()
        processor = Qwen3VLProcessor.from_pretrained(
            snapshot,
            local_files_only=True,
            trust_remote_code=False,
            padding_side="right",
            min_pixels=4096,
            max_pixels=_MAX_PIXELS,
        )
        self._processor = cast(Processor, processor)
        self._encoder = cast(Encoder, encoder)
        _log.info(
            "model.loaded",
            model=_REPO,
            revision=_REVISION,
            dimensions=self.dimensions,
        )

    def embed(
        self, text: str, *, image: bytes | None = None, query: bool = False
    ) -> tuple[float, ...]:
        """Follow upstream formatting and MRL pooling without silent truncation."""
        import torch
        from PIL import Image

        if self._encoder is None or self._processor is None:
            raise ValueError("Qwen3-VL embeddings are not loaded")
        images: list[Image.Image] = []
        content: list[dict[str, object]] = []
        if image is not None:
            if query or len(image) > 25 * 1024 * 1024:
                raise ValueError("Invalid visual embedding request")
            try:
                with Image.open(io.BytesIO(image)) as decoded:
                    if (
                        decoded.width * decoded.height > 16_000_000
                        or decoded.format not in {"PNG", "JPEG", "WEBP"}
                    ):
                        raise ValueError(
                            "Image pixel budget or format rejected"
                        )
                    decoded.thumbnail((512, 512))
                    images.append(decoded.convert("RGB"))
            except (OSError, Image.DecompressionBombError):
                raise ValueError("Image decoding rejected") from None
            content.append({"type": "image"})
        content.append({"type": "text", "text": text})
        messages = [
            {
                "role": "system",
                "content": [{"type": "text", "text": _INSTRUCTION}],
            },
            {"role": "user", "content": content},
        ]
        try:
            formatted = self._processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
            inputs = self._processor(
                text=[formatted],
                images=images or None,
                padding=True,
                truncation=False,
                return_tensors="pt",
            )
            if inputs["input_ids"].shape[-1] > _MAX_LENGTH:
                raise ValueError("Qwen3-VL embedding context exceeded")
            inputs = {
                key: value.to(self._encoder.device)
                for key, value in inputs.items()
            }
            with torch.inference_mode():
                hidden = self._encoder(
                    **inputs, use_cache=False
                ).last_hidden_state
                mask = inputs["attention_mask"]
                last = mask.shape[1] - mask.flip(dims=[1]).argmax(dim=1) - 1
                pooled = hidden[
                    torch.arange(hidden.shape[0], device=hidden.device), last
                ]
                vector = pooled[0, : self.dimensions].float()
                if (
                    vector.numel() != self.dimensions
                    or not torch.isfinite(vector).all()
                    or vector.norm() <= 1e-12
                ):
                    raise ValueError("Invalid Qwen3-VL embedding output")
                vector = torch.nn.functional.normalize(vector, p=2, dim=-1)
                return tuple(vector.tolist())
        finally:
            for decoded in images:
                decoded.close()
