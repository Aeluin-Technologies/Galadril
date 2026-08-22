"""IBM-NASA Prithvi-EO-2.0-600M inference support.

The upstream checkpoint is a masked autoencoder trained on four temporal HLS
observations with bands B02, B03, B04, B05, B06, and B07. This module keeps the
model implementation self-contained so loading the official checkpoint does not
require TerraTorch, timm, einops, or executing code downloaded from the Hub.

Example:
    request = PredictionRequest(
        model_name="prithvi_eo_2_600m",
        features={
            "action": "embedding",
            "image": hls_timeseries,  # float32 array shaped (T, C, H, W)
            "layout": "TCHW",
            "normalized": False,
            "pooling": "mean",
        },
    )
    embedding = engine.predict(request).prediction["embedding"]
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from enum import StrEnum, unique
from pathlib import Path
from typing import Any, Final

import numpy as np
import structlog
import torch
import torch.nn.functional as functional
from numpy.typing import NDArray
from torch import Tensor, nn

from galadril_inference.common.exceptions import (
    ModelLoadError,
    SchemaValidationError,
)
from galadril_inference.common.types import (
    ModelMeta,
    PredictionRequest,
    PredictionResult,
)
from galadril_inference.models.base import BaseModel

logger = structlog.get_logger(__name__)

_MODEL_NAME: Final = "prithvi_eo_2_600m"
_MODEL_VERSION: Final = "2.0.0"
_REPO_ID: Final = "ibm-nasa-geospatial/Prithvi-EO-2.0-600M"
_CHECKPOINT_NAME: Final = "Prithvi_EO_V2_600M.pt"
_CONFIG_NAME: Final = "config.json"
_NO_DATA: Final = -9999.0
_NO_DATA_NORMALIZED: Final = 0.0001
_DEFAULT_MEAN: Final = (1087.0, 1342.0, 1433.0, 2734.0, 1958.0, 1363.0)
_DEFAULT_STD: Final = (2248.0, 2179.0, 2178.0, 1850.0, 1242.0, 1049.0)


@unique
class PrithviAction(StrEnum):
    """Native inference surfaces exposed by the Prithvi MAE."""

    RECONSTRUCTION = "reconstruction"
    EMBEDDING = "embedding"
    FEATURES = "features"
    RAW = "raw"


def _pair(value: int | Sequence[int]) -> tuple[int, int]:
    """Convert an integer or two-item sequence into a spatial pair."""
    if isinstance(value, int):
        return value, value
    if len(value) != 2:
        raise ValueError("Expected a two-item spatial size.")
    return int(value[0]), int(value[1])


def _triple(value: int | Sequence[int]) -> tuple[int, int, int]:
    """Convert a patch size into a temporal-spatial triple."""
    if isinstance(value, int):
        return 1, value, value
    if len(value) != 3:
        raise ValueError("Expected a three-item patch size.")
    return int(value[0]), int(value[1]), int(value[2])


def _sincos_1d(
    embed_dim: int, positions: NDArray[np.float64]
) -> NDArray[np.float64]:
    """Build the fixed one-dimensional sinusoidal position encoding."""
    if embed_dim % 2 != 0:
        raise ValueError("Position embedding dimension must be even.")
    omega = np.arange(embed_dim // 2, dtype=np.float64) / (embed_dim / 2.0)
    omega = 1.0 / (10000.0**omega)
    phase = np.einsum("m,d->md", positions.reshape(-1), omega)
    return np.concatenate((np.sin(phase), np.cos(phase)), axis=1)


def _sincos_3d(
    embed_dim: int,
    grid_size: Sequence[int],
    *,
    add_cls_token: bool,
) -> Tensor:
    """Build Prithvi's fixed 3D position encoding exactly as upstream."""
    if embed_dim % 16 != 0:
        raise ValueError("Prithvi embedding dimension must be divisible by 16.")
    time_size, height, width = (int(item) for item in grid_size)
    width_dim = embed_dim // 16 * 6
    height_dim = embed_dim // 16 * 6
    time_dim = embed_dim // 16 * 4
    width_embed = np.tile(
        _sincos_1d(width_dim, np.arange(width, dtype=np.float64)),
        (time_size * height, 1),
    )
    height_embed = np.tile(
        np.repeat(
            _sincos_1d(height_dim, np.arange(height, dtype=np.float64)),
            width,
            axis=0,
        ),
        (time_size, 1),
    )
    time_embed = np.repeat(
        _sincos_1d(time_dim, np.arange(time_size, dtype=np.float64)),
        height * width,
        axis=0,
    )
    position = np.concatenate((width_embed, height_embed, time_embed), axis=1)
    if add_cls_token:
        position = np.concatenate(
            (np.zeros((1, embed_dim), dtype=np.float64), position), axis=0
        )
    return torch.from_numpy(position).to(dtype=torch.float32).unsqueeze(0)


def _interpolate_position(
    position: Tensor,
    grid_size: Sequence[int],
    patch_size: Sequence[int],
    sample_shape: Sequence[int],
) -> Tensor:
    """Interpolate fixed positions for variable time and spatial dimensions."""
    target_grid = tuple(
        int(size) // int(patch)
        for size, patch in zip(sample_shape, patch_size, strict=True)
    )
    source_grid = tuple(int(item) for item in grid_size)
    if target_grid == source_grid:
        return position
    source = position
    if target_grid[0] != source_grid[0]:
        source_grid = (target_grid[0], source_grid[1], source_grid[2])
        source = _sincos_3d(
            position.shape[-1], source_grid, add_cls_token=True
        ).to(device=position.device, dtype=position.dtype)
    class_position = source[:, :1]
    patches = source[:, 1:].reshape(*source_grid, position.shape[-1])
    patches = patches.permute(0, 3, 1, 2)
    patches = functional.interpolate(
        patches,
        size=target_grid[1:],
        mode="bicubic",
        align_corners=True,
    )
    patches = patches.permute(0, 2, 3, 1).reshape(1, -1, position.shape[-1])
    return torch.cat((class_position, patches), dim=1)


class _PatchEmbed(nn.Module):
    """Three-dimensional non-overlapping patch projection."""

    def __init__(
        self,
        input_size: tuple[int, int, int],
        patch_size: tuple[int, int, int],
        in_channels: int,
        embed_dim: int,
    ) -> None:
        super().__init__()
        self.input_size = input_size
        self.patch_size = patch_size
        self.grid_size = tuple(
            size // patch
            for size, patch in zip(input_size, patch_size, strict=True)
        )
        self.proj = nn.Conv3d(
            in_channels,
            embed_dim,
            kernel_size=patch_size,
            stride=patch_size,
        )
        self.norm = nn.Identity()

    def forward(self, value: Tensor) -> Tensor:
        """Project BCTHW input into BLD patch tokens."""
        return self.norm(self.proj(value).flatten(2).transpose(1, 2))


class _Attention(nn.Module):
    """State-dict-compatible timm multi-head self-attention."""

    def __init__(self, dim: int, num_heads: int) -> None:
        super().__init__()
        if dim % num_heads != 0:
            raise ValueError(
                "Embedding dimension must divide evenly into heads."
            )
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.qkv = nn.Linear(dim, dim * 3)
        self.q_norm = nn.Identity()
        self.k_norm = nn.Identity()
        self.attn_drop = nn.Dropout(0.0)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(0.0)

    def forward(self, value: Tensor) -> Tensor:
        """Apply scaled dot-product self-attention without materializing scores."""
        batch, length, dim = value.shape
        qkv = self.qkv(value).reshape(
            batch, length, 3, self.num_heads, self.head_dim
        )
        qkv = qkv.permute(2, 0, 3, 1, 4)
        query, key, values = qkv.unbind(0)
        query = self.q_norm(query)
        key = self.k_norm(key)
        attended = functional.scaled_dot_product_attention(
            query, key, values, dropout_p=0.0
        )
        attended = attended.transpose(1, 2).reshape(batch, length, dim)
        return self.proj_drop(self.proj(attended))


class _Mlp(nn.Module):
    """State-dict-compatible timm transformer MLP."""

    def __init__(self, dim: int, hidden_dim: int) -> None:
        super().__init__()
        self.fc1 = nn.Linear(dim, hidden_dim)
        self.act = nn.GELU()
        self.drop1 = nn.Dropout(0.0)
        self.norm = nn.Identity()
        self.fc2 = nn.Linear(hidden_dim, dim)
        self.drop2 = nn.Dropout(0.0)

    def forward(self, value: Tensor) -> Tensor:
        """Apply the feed-forward projection."""
        value = self.drop1(self.act(self.fc1(value)))
        return self.drop2(self.fc2(self.norm(value)))


class _Block(nn.Module):
    """State-dict-compatible pre-normalized ViT block."""

    def __init__(self, dim: int, num_heads: int, mlp_ratio: float) -> None:
        super().__init__()
        self.norm1 = nn.LayerNorm(dim)
        self.attn = _Attention(dim, num_heads)
        self.ls1 = nn.Identity()
        self.drop_path1 = nn.Identity()
        self.norm2 = nn.LayerNorm(dim)
        self.mlp = _Mlp(dim, int(dim * mlp_ratio))
        self.ls2 = nn.Identity()
        self.drop_path2 = nn.Identity()

    def forward(self, value: Tensor) -> Tensor:
        """Apply attention and feed-forward residual updates."""
        value = value + self.drop_path1(self.ls1(self.attn(self.norm1(value))))
        return value + self.drop_path2(self.ls2(self.mlp(self.norm2(value))))


class _PrithviEncoder(nn.Module):
    """Prithvi EO 2.0 Vision Transformer encoder."""

    def __init__(
        self,
        *,
        img_size: int | Sequence[int],
        patch_size: int | Sequence[int],
        num_frames: int,
        in_chans: int,
        embed_dim: int,
        depth: int,
        num_heads: int,
        mlp_ratio: float,
    ) -> None:
        super().__init__()
        spatial_size = _pair(img_size)
        temporal_patch = _triple(patch_size)
        self.in_chans = in_chans
        self.num_frames = num_frames
        self.embed_dim = embed_dim
        self.img_size = spatial_size
        self.patch_embed = _PatchEmbed(
            (num_frames, *spatial_size),
            temporal_patch,
            in_chans,
            embed_dim,
        )
        token_count = math.prod(self.patch_embed.grid_size)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed: Tensor
        self.register_buffer(
            "pos_embed",
            _sincos_3d(
                embed_dim, self.patch_embed.grid_size, add_cls_token=True
            ),
        )
        self.blocks = nn.ModuleList(
            _Block(embed_dim, num_heads, mlp_ratio) for _ in range(depth)
        )
        self.norm = nn.LayerNorm(embed_dim)
        if token_count < 1:
            raise ValueError("Patch size is larger than the configured input.")

    def _tokens(self, value: Tensor) -> Tensor:
        """Patchify input and add interpolated fixed positions."""
        tokens = self.patch_embed(value)
        position = _interpolate_position(
            self.pos_embed,
            self.patch_embed.grid_size,
            self.patch_embed.patch_size,
            value.shape[-3:],
        )
        tokens = tokens + position[:, 1:]
        class_token = (self.cls_token + position[:, :1]).expand(
            tokens.shape[0], -1, -1
        )
        return torch.cat((class_token, tokens), dim=1)

    def forward_selected(
        self, value: Tensor, layers: frozenset[int]
    ) -> dict[int, Tensor]:
        """Return only requested block outputs to bound feature memory."""
        value = self._tokens(value)
        selected: dict[int, Tensor] = {}
        last_index = len(self.blocks) - 1
        for index, block in enumerate(self.blocks):
            value = block(value)
            if index in layers:
                selected[index] = (
                    self.norm(value) if index == last_index else value
                )
        return selected

    def forward_masked(
        self, value: Tensor, mask_ratio: float
    ) -> tuple[Tensor, Tensor, Tensor]:
        """Encode a random visible-token subset and return restoration indices."""
        tokens = self.patch_embed(value)
        position = _interpolate_position(
            self.pos_embed,
            self.patch_embed.grid_size,
            self.patch_embed.patch_size,
            value.shape[-3:],
        )
        tokens = tokens + position[:, 1:]
        batch, length, dim = tokens.shape
        keep_count = int(length * (1.0 - mask_ratio))
        noise = torch.rand(batch, length, device=tokens.device)
        shuffle = torch.argsort(noise, dim=1)
        restore = torch.argsort(shuffle, dim=1)
        keep = shuffle[:, :keep_count]
        tokens = torch.gather(tokens, 1, keep.unsqueeze(-1).expand(-1, -1, dim))
        mask = torch.ones(batch, length, device=tokens.device)
        mask[:, :keep_count] = 0.0
        mask = torch.gather(mask, 1, restore)
        class_token = (self.cls_token + position[:, :1]).expand(batch, -1, -1)
        tokens = torch.cat((class_token, tokens), dim=1)
        for block in self.blocks:
            tokens = block(tokens)
        return self.norm(tokens), mask, restore


class _PrithviDecoder(nn.Module):
    """Masked autoencoder decoder used by the pretrained checkpoint."""

    def __init__(
        self,
        *,
        patch_size: int | Sequence[int],
        grid_size: Sequence[int],
        in_chans: int,
        encoder_embed_dim: int,
        decoder_embed_dim: int,
        depth: int,
        num_heads: int,
        mlp_ratio: float,
    ) -> None:
        super().__init__()
        self.patch_size = _triple(patch_size)
        self.grid_size = tuple(int(item) for item in grid_size)
        self.decoder_embed = nn.Linear(encoder_embed_dim, decoder_embed_dim)
        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))
        self.decoder_pos_embed: Tensor
        self.register_buffer(
            "decoder_pos_embed",
            _sincos_3d(decoder_embed_dim, self.grid_size, add_cls_token=True),
        )
        self.decoder_blocks = nn.ModuleList(
            _Block(decoder_embed_dim, num_heads, mlp_ratio)
            for _ in range(depth)
        )
        self.decoder_norm = nn.LayerNorm(decoder_embed_dim)
        self.decoder_pred = nn.Linear(
            decoder_embed_dim, math.prod(self.patch_size) * in_chans
        )

    def forward(
        self, hidden: Tensor, restore: Tensor, input_shape: Sequence[int]
    ) -> Tensor:
        """Restore masked tokens and predict normalized pixel patches."""
        hidden = self.decoder_embed(hidden)
        class_token = hidden[:, :1]
        mask_tokens = self.mask_token.expand(
            hidden.shape[0], restore.shape[1] + 1 - hidden.shape[1], -1
        )
        hidden = torch.cat((hidden[:, 1:], mask_tokens), dim=1)
        hidden = torch.gather(
            hidden,
            1,
            restore.unsqueeze(-1).expand(-1, -1, hidden.shape[-1]),
        )
        position = _interpolate_position(
            self.decoder_pos_embed,
            self.grid_size,
            self.patch_size,
            input_shape[-3:],
        )
        hidden = torch.cat(
            (class_token + position[:, :1], hidden + position[:, 1:]), dim=1
        )
        for block in self.decoder_blocks:
            hidden = block(hidden)
        return self.decoder_pred(self.decoder_norm(hidden))[:, 1:]


class _PrithviMAE(nn.Module):
    """Self-contained Prithvi-EO-2.0 masked autoencoder."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__()
        self.encoder = _PrithviEncoder(
            img_size=config["img_size"],
            patch_size=config["patch_size"],
            num_frames=int(config["num_frames"]),
            in_chans=int(config["in_chans"]),
            embed_dim=int(config["embed_dim"]),
            depth=int(config["depth"]),
            num_heads=int(config["num_heads"]),
            mlp_ratio=float(config["mlp_ratio"]),
        )
        self.decoder = _PrithviDecoder(
            patch_size=config["patch_size"],
            grid_size=self.encoder.patch_embed.grid_size,
            in_chans=int(config["in_chans"]),
            encoder_embed_dim=int(config["embed_dim"]),
            decoder_embed_dim=int(config["decoder_embed_dim"]),
            depth=int(config["decoder_depth"]),
            num_heads=int(config["decoder_num_heads"]),
            mlp_ratio=float(config["mlp_ratio"]),
        )

    def patchify(self, value: Tensor) -> Tensor:
        """Convert BCTHW pixels into patch-major vectors."""
        temporal, height, width = self.encoder.patch_embed.patch_size
        batch, channels, frames, image_height, image_width = value.shape
        value = value.reshape(
            batch,
            channels,
            frames // temporal,
            temporal,
            image_height // height,
            height,
            image_width // width,
            width,
        )
        value = value.permute(0, 2, 4, 6, 3, 5, 7, 1)
        return value.reshape(batch, -1, temporal * height * width * channels)

    def unpatchify(self, value: Tensor, image_shape: Sequence[int]) -> Tensor:
        """Convert patch-major predictions back into BCTHW pixels."""
        temporal, height, width = self.encoder.patch_embed.patch_size
        frames, image_height, image_width = (int(item) for item in image_shape)
        channels = self.encoder.in_chans
        batch = value.shape[0]
        value = value.reshape(
            batch,
            frames // temporal,
            image_height // height,
            image_width // width,
            temporal,
            height,
            width,
            channels,
        )
        value = value.permute(0, 7, 1, 4, 2, 5, 3, 6)
        return value.reshape(batch, channels, frames, image_height, image_width)

    def reconstruct(
        self, value: Tensor, mask_ratio: float
    ) -> tuple[Tensor, Tensor, Tensor, Tensor]:
        """Run masked reconstruction and return loss, patches, mask, and image."""
        latent, mask, restore = self.encoder.forward_masked(value, mask_ratio)
        prediction = self.decoder(latent, restore, value.shape)
        target = self.patchify(value)
        patch_loss = (prediction - target).square().mean(dim=-1)
        loss = (patch_loss * mask).sum() / mask.sum()
        prediction_image = self.unpatchify(prediction, value.shape[-3:])
        mask_image = self.unpatchify(
            mask.unsqueeze(-1).expand(-1, -1, prediction.shape[-1]),
            value.shape[-3:],
        )
        reconstructed = torch.where(mask_image.bool(), prediction_image, value)
        return loss, prediction, mask, reconstructed


class PrithviEOModel(BaseModel):
    """IBM-NASA Prithvi-EO-2.0-600M reconstruction and feature model."""

    def __init__(self) -> None:
        self._model: _PrithviMAE | None = None
        self._device = torch.device("cpu")
        self._dtype = torch.float32
        self._mean = torch.tensor(_DEFAULT_MEAN).view(1, 6, 1, 1, 1)
        self._std = torch.tensor(_DEFAULT_STD).view(1, 6, 1, 1, 1)
        self._bands: tuple[str, ...] = (
            "B02",
            "B03",
            "B04",
            "B05",
            "B06",
            "B07",
        )
        self._depth = 32
        self._embed_dim = 1280
        self._patch_size = (1, 14, 14)
        self._tile_size = 224

    def meta(self) -> ModelMeta:
        """Return immutable model identity and provenance."""
        return ModelMeta(
            name=_MODEL_NAME,
            version=_MODEL_VERSION,
            description=(
                "IBM-NASA Prithvi-EO-2.0-600M masked autoencoder for "
                "multi-temporal HLS reconstruction and feature extraction."
            ),
            tags={
                "domain": "earth-observation",
                "backend": "native-pytorch",
                "framework": "pytorch",
                "upstream": _REPO_ID,
            },
        )

    def load(
        self,
        artifact_path: str,
        *,
        device: str = "auto",
        dtype: str = "auto",
    ) -> None:
        """Load the official config and checkpoint without duplicate weight copies."""
        root = Path(artifact_path)
        config_path = root / _CONFIG_NAME
        checkpoint_path = root / _CHECKPOINT_NAME
        if not config_path.is_file() or not checkpoint_path.is_file():
            raise ModelLoadError(
                _MODEL_NAME,
                f"Expected {_CONFIG_NAME} and {_CHECKPOINT_NAME} in {root}.",
            )
        try:
            config_document = json.loads(
                config_path.read_text(encoding="utf-8")
            )
            config = config_document["pretrained_cfg"]
            self._validate_upstream_config(config)
            self._device = self._resolve_device(device)
            self._dtype = self._resolve_dtype(dtype, self._device)
            model = _PrithviMAE(config)
            state = torch.load(
                checkpoint_path,
                map_location="cpu",
                weights_only=True,
                mmap=True,
            )
            if not isinstance(state, Mapping):
                raise TypeError(
                    "Checkpoint does not contain a state dictionary."
                )
            raw_state = state.get("state_dict", state)
            if not isinstance(raw_state, Mapping):
                raise TypeError("Checkpoint state_dict is invalid.")
            cleaned = {
                self._strip_weight_prefix(str(key)): tensor
                for key, tensor in raw_state.items()
                if "pos_embed" not in str(key)
            }
            incompatible = model.load_state_dict(
                cleaned, strict=False, assign=True
            )
            allowed_missing = {"encoder.pos_embed", "decoder.decoder_pos_embed"}
            unexpected_missing = (
                set(incompatible.missing_keys) - allowed_missing
            )
            if unexpected_missing or incompatible.unexpected_keys:
                raise ValueError(
                    "Checkpoint architecture mismatch: "
                    f"missing={sorted(unexpected_missing)}, "
                    f"unexpected={sorted(incompatible.unexpected_keys)}"
                )
            self._model = model.to(
                device=self._device, dtype=self._dtype
            ).eval()
            self._mean = torch.tensor(config["mean"], dtype=torch.float32).view(
                1, 6, 1, 1, 1
            )
            self._std = torch.tensor(config["std"], dtype=torch.float32).view(
                1, 6, 1, 1, 1
            )
            self._bands = tuple(str(band) for band in config["bands"])
            self._depth = int(config["depth"])
            self._embed_dim = int(config["embed_dim"])
            self._patch_size = _triple(config["patch_size"])
            self._tile_size = int(config["img_size"])
            logger.info(
                "model_loaded",
                model_name=_MODEL_NAME,
                device=str(self._device),
                dtype=str(self._dtype),
            )
        except ModelLoadError:
            raise
        except Exception as exc:
            self._model = None
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def download(self, target_path: str) -> None:
        """Download only the authoritative model files required at runtime."""
        try:
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise ModelLoadError(
                _MODEL_NAME, "huggingface_hub is not installed."
            ) from exc
        try:
            Path(target_path).mkdir(parents=True, exist_ok=True)
            snapshot_download(
                repo_id=_REPO_ID,
                local_dir=target_path,
                allow_patterns=[_CONFIG_NAME, _CHECKPOINT_NAME],
            )
        except Exception as exc:
            raise ModelLoadError(_MODEL_NAME, str(exc)) from exc

    def predict(self, request: PredictionRequest) -> PredictionResult:
        """Validate and dispatch a reconstruction or representation request."""
        model = self._ensure_loaded()
        action = self._extract_action(request)
        value = self._prepare_input(request)
        try:
            with torch.inference_mode():
                if action in (PrithviAction.RECONSTRUCTION, PrithviAction.RAW):
                    prediction = self._predict_reconstruction(
                        model,
                        value,
                        request.features,
                        raw=action is PrithviAction.RAW,
                    )
                else:
                    prediction = self._predict_features(
                        model, value, request.features, action
                    )
            return PredictionResult(
                model_name=_MODEL_NAME,
                model_version=_MODEL_VERSION,
                prediction=prediction,
            )
        except SchemaValidationError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Prithvi inference failed: {exc}") from exc

    def cleanup(self) -> None:
        """Release model weights and accelerator caches."""
        self._model = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        if hasattr(torch, "mps") and torch.backends.mps.is_available():
            torch.mps.empty_cache()
        logger.info("model_cleaned_up", model_name=_MODEL_NAME)

    def input_schema(self) -> dict[str, Any]:
        """Return the request schema for every supported native operation."""
        return {
            "type": "object",
            "required": ["action", "image"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [action.value for action in PrithviAction],
                },
                "image": {
                    "type": "ndarray",
                    "description": "HLS reflectance or normalized imagery.",
                },
                "layout": {
                    "type": "string",
                    "enum": [
                        "CHW",
                        "HWC",
                        "CTHW",
                        "TCHW",
                        "THWC",
                        "BCTHW",
                        "BTCHW",
                        "BTHWC",
                    ],
                    "default": "auto",
                },
                "normalized": {"type": "boolean", "default": False},
                "mask_ratio": {
                    "type": "number",
                    "exclusiveMinimum": 0.0,
                    "exclusiveMaximum": 1.0,
                    "default": 0.75,
                },
                "pooling": {
                    "type": "string",
                    "enum": ["cls", "mean"],
                    "default": "mean",
                },
                "normalize_embedding": {"type": "boolean", "default": True},
                "output_layers": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "default": [-1],
                },
                "feature_format": {
                    "type": "string",
                    "enum": ["tokens", "maps"],
                    "default": "tokens",
                },
                "tile_size": {
                    "type": ["integer", "null"],
                    "default": 224,
                },
                "batch_size": {"type": "integer", "minimum": 1, "default": 1},
                "return_normalized": {"type": "boolean", "default": False},
            },
        }

    def output_schema(self) -> dict[str, Any]:
        """Return the union schema for reconstruction and feature outputs."""
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "embedding": {"type": "ndarray"},
                "embedding_dim": {"type": "integer"},
                "features": {"type": "object"},
                "reconstruction": {"type": "ndarray"},
                "prediction": {"type": "ndarray"},
                "mask": {"type": "ndarray"},
                "loss": {"type": "number"},
                "bands": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "layout": {"type": "string"},
            },
        }

    def _prepare_input(self, request: PredictionRequest) -> Tensor:
        """Canonicalize supported layouts and normalize HLS reflectance values."""
        image = request.features.get("image")
        if not isinstance(image, (np.ndarray, Tensor)):
            raise SchemaValidationError(
                _MODEL_NAME,
                ["Feature 'image' must be a numpy array or tensor."],
            )
        value = torch.as_tensor(image)
        layout = request.features.get("layout", "auto")
        value = self._to_bcthw(value, str(layout))
        if value.shape[1] != len(self._bands):
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Expected {len(self._bands)} channels in {self._bands} order; "
                    f"received {value.shape[1]}."
                ],
            )
        if not value.is_floating_point():
            value = value.to(dtype=torch.float32)
        else:
            value = value.to(dtype=torch.float32, copy=False)
        if not bool(request.features.get("normalized", False)):
            mean = self._mean
            std = self._std
            no_data = value == _NO_DATA
            value = (value - mean) / std
            value.masked_fill_(no_data, _NO_DATA_NORMALIZED)
        if not torch.isfinite(value).all():
            raise SchemaValidationError(
                _MODEL_NAME, ["Image contains NaN or infinite values."]
            )
        return value.to(device=self._device, dtype=self._dtype)

    def _predict_reconstruction(
        self,
        model: _PrithviMAE,
        value: Tensor,
        features: Mapping[str, Any],
        *,
        raw: bool,
    ) -> dict[str, Any]:
        """Reconstruct masked patches, using bounded-memory spatial tiling."""
        mask_ratio = float(features.get("mask_ratio", 0.75))
        if not 0.0 < mask_ratio < 1.0:
            raise SchemaValidationError(
                _MODEL_NAME, ["'mask_ratio' must be strictly between 0 and 1."]
            )
        windows, geometry = self._make_windows(
            value, features.get("tile_size", 224)
        )
        batch_size = self._positive_batch_size(features)
        reconstructed_chunks: list[Tensor] = []
        prediction_chunks: list[Tensor] = []
        mask_chunks: list[Tensor] = []
        weighted_loss = torch.zeros((), device=self._device)
        sample_count = 0
        for chunk in windows.split(batch_size):
            loss, prediction, mask, reconstructed = model.reconstruct(
                chunk, mask_ratio
            )
            weighted_loss += loss * chunk.shape[0]
            sample_count += chunk.shape[0]
            reconstructed_chunks.append(reconstructed.cpu())
            mask_image = model.unpatchify(
                mask.unsqueeze(-1).expand(-1, -1, prediction.shape[-1]),
                chunk.shape[-3:],
            )
            mask_chunks.append(mask_image.cpu())
            if raw:
                prediction_chunks.append(prediction.cpu())
        reconstructed = self._stitch_windows(
            torch.cat(reconstructed_chunks), geometry
        )
        mask_image = self._stitch_windows(torch.cat(mask_chunks), geometry)
        return_normalized = bool(features.get("return_normalized", False))
        if not return_normalized:
            reconstructed = self._denormalize(reconstructed)
        output: dict[str, Any] = {
            "action": PrithviAction.RAW.value
            if raw
            else PrithviAction.RECONSTRUCTION.value,
            "loss": float((weighted_loss / sample_count).cpu()),
            "reconstruction": reconstructed.numpy(),
            "mask": mask_image[:, :1].numpy().astype(np.bool_, copy=False),
            "bands": list(self._bands),
            "layout": "BCTHW",
            "normalized": return_normalized,
        }
        if raw:
            output["prediction"] = torch.cat(prediction_chunks).numpy()
            output["prediction_layout"] = "NLP"
        return output

    def _predict_features(
        self,
        model: _PrithviMAE,
        value: Tensor,
        features: Mapping[str, Any],
        action: PrithviAction,
    ) -> dict[str, Any]:
        """Extract pooled embeddings, tokens, or spatial feature maps."""
        layers = self._extract_layers(features)
        windows, geometry = self._make_windows(
            value, features.get("tile_size", 224)
        )
        batch_size = self._positive_batch_size(features)
        collected: dict[int, list[Tensor]] = {layer: [] for layer in layers}
        for chunk in windows.split(batch_size):
            selected = model.encoder.forward_selected(chunk, frozenset(layers))
            for layer in layers:
                collected[layer].append(selected[layer].cpu())
        if action is PrithviAction.EMBEDDING:
            last = torch.cat(collected[layers[-1]])
            pooling = str(features.get("pooling", "mean"))
            if pooling == "cls":
                embeddings = last[:, 0]
            elif pooling == "mean":
                embeddings = last[:, 1:].mean(dim=1)
            else:
                raise SchemaValidationError(
                    _MODEL_NAME, ["'pooling' must be 'cls' or 'mean'."]
                )
            embeddings = embeddings.reshape(
                geometry["batch"], geometry["rows"] * geometry["cols"], -1
            ).mean(dim=1)
            if bool(features.get("normalize_embedding", True)):
                embeddings = functional.normalize(embeddings, dim=-1)
            return {
                "action": action.value,
                "embedding": embeddings.numpy(),
                "embedding_dim": embeddings.shape[-1],
                "pooling": pooling,
                "normalized": bool(features.get("normalize_embedding", True)),
            }
        feature_format = str(features.get("feature_format", "tokens"))
        output_features: dict[str, NDArray[np.generic]] = {}
        for layer in layers:
            tokens = torch.cat(collected[layer])
            if feature_format == "tokens":
                output_features[str(layer)] = tokens.numpy()
            elif feature_format == "maps":
                maps = self._tokens_to_maps(tokens, geometry)
                output_features[str(layer)] = maps.numpy()
            else:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["'feature_format' must be 'tokens' or 'maps'."],
                )
        return {
            "action": action.value,
            "features": output_features,
            "layers": list(layers),
            "format": feature_format,
            "embedding_dim": self._embed_dim,
        }

    def _make_windows(
        self, value: Tensor, requested_tile: object
    ) -> tuple[Tensor, dict[str, int]]:
        """Create non-overlapping tiles while retaining reversible geometry."""
        patch_height, patch_width = self._patch_size[1:]
        height, width = value.shape[-2:]
        if requested_tile is None:
            tile_height = math.ceil(height / patch_height) * patch_height
            tile_width = math.ceil(width / patch_width) * patch_width
        else:
            if not isinstance(requested_tile, int) or requested_tile < 1:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    ["'tile_size' must be a positive integer or null."],
                )
            if (
                requested_tile % patch_height != 0
                or requested_tile % patch_width != 0
            ):
                raise SchemaValidationError(
                    _MODEL_NAME,
                    [
                        f"'tile_size' must be divisible by {patch_height} and {patch_width}."
                    ],
                )
            tile_height = requested_tile
            tile_width = requested_tile
        padded_height = math.ceil(height / tile_height) * tile_height
        padded_width = math.ceil(width / tile_width) * tile_width
        spatial_pad = (0, padded_width - width, 0, padded_height - height)
        if spatial_pad[1] or spatial_pad[3]:
            mode = (
                "reflect"
                if spatial_pad[1] < width
                and spatial_pad[3] < height
                and height > 1
                and width > 1
                else "replicate"
            )
            value = functional.pad(value, (*spatial_pad, 0, 0), mode=mode)
        rows = padded_height // tile_height
        cols = padded_width // tile_width
        windows = value.unfold(-2, tile_height, tile_height).unfold(
            -1, tile_width, tile_width
        )
        windows = windows.permute(0, 3, 4, 1, 2, 5, 6).reshape(
            -1,
            value.shape[1],
            value.shape[2],
            tile_height,
            tile_width,
        )
        return windows, {
            "batch": value.shape[0],
            "frames": value.shape[2],
            "height": height,
            "width": width,
            "padded_height": padded_height,
            "padded_width": padded_width,
            "tile_height": tile_height,
            "tile_width": tile_width,
            "rows": rows,
            "cols": cols,
        }

    @staticmethod
    def _stitch_windows(windows: Tensor, geometry: Mapping[str, int]) -> Tensor:
        """Reassemble BCTHW tensors from non-overlapping spatial windows."""
        value = windows.reshape(
            geometry["batch"],
            geometry["rows"],
            geometry["cols"],
            windows.shape[1],
            windows.shape[2],
            geometry["tile_height"],
            geometry["tile_width"],
        )
        value = value.permute(0, 3, 4, 1, 5, 2, 6).reshape(
            geometry["batch"],
            windows.shape[1],
            windows.shape[2],
            geometry["padded_height"],
            geometry["padded_width"],
        )
        return value[..., : geometry["height"], : geometry["width"]]

    def _tokens_to_maps(
        self, tokens: Tensor, geometry: Mapping[str, int]
    ) -> Tensor:
        """Convert per-tile patch tokens into stitched BDT'h'w' feature maps."""
        temporal_patch, patch_height, patch_width = self._patch_size
        tile_frames = geometry["frames"] // temporal_patch
        tile_height = geometry["tile_height"] // patch_height
        tile_width = geometry["tile_width"] // patch_width
        maps = tokens[:, 1:].reshape(
            -1, tile_frames, tile_height, tile_width, self._embed_dim
        )
        maps = maps.permute(0, 4, 1, 2, 3)
        maps = maps.reshape(
            geometry["batch"],
            geometry["rows"],
            geometry["cols"],
            self._embed_dim,
            tile_frames,
            tile_height,
            tile_width,
        )
        maps = maps.permute(0, 3, 4, 1, 5, 2, 6).reshape(
            geometry["batch"],
            self._embed_dim,
            tile_frames,
            geometry["padded_height"] // patch_height,
            geometry["padded_width"] // patch_width,
        )
        output_height = math.ceil(geometry["height"] / patch_height)
        output_width = math.ceil(geometry["width"] / patch_width)
        return maps[..., :output_height, :output_width]

    def _denormalize(self, value: Tensor) -> Tensor:
        """Return reconstructed pixels in original HLS reflectance units."""
        mean = self._mean.to(dtype=value.dtype)
        std = self._std.to(dtype=value.dtype)
        return value * std + mean

    def _ensure_loaded(self) -> _PrithviMAE:
        """Return the loaded model or raise a domain-specific error."""
        if self._model is None:
            raise ModelLoadError(_MODEL_NAME, "Model is not loaded.")
        return self._model

    def _extract_layers(self, features: Mapping[str, Any]) -> tuple[int, ...]:
        """Normalize positive and negative block indices without duplicates."""
        raw_layers = features.get("output_layers", [-1])
        if not isinstance(raw_layers, Sequence) or isinstance(
            raw_layers, (str, bytes)
        ):
            raise SchemaValidationError(
                _MODEL_NAME, ["'output_layers' must be an integer array."]
            )
        layers: list[int] = []
        for raw_layer in raw_layers:
            if not isinstance(raw_layer, int):
                raise SchemaValidationError(
                    _MODEL_NAME, ["Every output layer must be an integer."]
                )
            layer = raw_layer + self._depth if raw_layer < 0 else raw_layer
            if not 0 <= layer < self._depth:
                raise SchemaValidationError(
                    _MODEL_NAME,
                    [
                        f"Output layer {raw_layer} is outside [{-self._depth}, {self._depth - 1}]."
                    ],
                )
            if layer not in layers:
                layers.append(layer)
        if not layers:
            raise SchemaValidationError(
                _MODEL_NAME, ["At least one output layer is required."]
            )
        return tuple(sorted(layers))

    @staticmethod
    def _positive_batch_size(features: Mapping[str, Any]) -> int:
        """Validate the accelerator micro-batch size."""
        batch_size = features.get("batch_size", 1)
        if not isinstance(batch_size, int) or batch_size < 1:
            raise SchemaValidationError(
                _MODEL_NAME, ["'batch_size' must be a positive integer."]
            )
        return batch_size

    @staticmethod
    def _extract_action(request: PredictionRequest) -> PrithviAction:
        """Parse the requested native inference action."""
        raw_action = request.features.get("action")
        try:
            return PrithviAction(raw_action)
        except (TypeError, ValueError) as exc:
            raise SchemaValidationError(
                _MODEL_NAME,
                [
                    f"Invalid action '{raw_action}'. Expected one of "
                    f"{[action.value for action in PrithviAction]}."
                ],
            ) from exc

    @staticmethod
    def _to_bcthw(value: Tensor, layout: str) -> Tensor:
        """Convert every documented input layout into canonical BCTHW form."""
        if layout == "auto":
            layout = PrithviEOModel._infer_layout(value)
        layouts: dict[str, tuple[int, ...]] = {
            "CHW": (0, 1, 2),
            "HWC": (2, 0, 1),
            "CTHW": (0, 1, 2, 3),
            "TCHW": (1, 0, 2, 3),
            "THWC": (3, 0, 1, 2),
            "BCTHW": (0, 1, 2, 3, 4),
            "BTCHW": (0, 2, 1, 3, 4),
            "BTHWC": (0, 4, 1, 2, 3),
        }
        permutation = layouts.get(layout)
        if permutation is None or len(permutation) != value.ndim:
            raise SchemaValidationError(
                _MODEL_NAME,
                [f"Layout '{layout}' is incompatible with rank {value.ndim}."],
            )
        value = value.permute(permutation)
        if value.ndim == 3:
            value = value.unsqueeze(0).unsqueeze(2)
        elif value.ndim == 4:
            value = value.unsqueeze(0)
        return value

    @staticmethod
    def _infer_layout(value: Tensor) -> str:
        """Infer only unambiguous common channel-first or channel-last layouts."""
        if value.ndim == 3:
            if value.shape[0] == 6:
                return "CHW"
            if value.shape[-1] == 6:
                return "HWC"
        elif value.ndim == 4:
            if value.shape[0] == 6:
                return "CTHW"
            if value.shape[1] == 6:
                return "TCHW"
            if value.shape[-1] == 6:
                return "THWC"
        elif value.ndim == 5:
            if value.shape[1] == 6:
                return "BCTHW"
            if value.shape[2] == 6:
                return "BTCHW"
            if value.shape[-1] == 6:
                return "BTHWC"
        raise SchemaValidationError(
            _MODEL_NAME,
            [
                "Cannot infer image layout; provide the explicit 'layout' feature."
            ],
        )

    @staticmethod
    def _resolve_device(requested: str) -> torch.device:
        """Select a supported accelerator deterministically."""
        if requested == "auto":
            if torch.cuda.is_available():
                return torch.device("cuda")
            if torch.backends.mps.is_available():
                return torch.device("mps")
            return torch.device("cpu")
        device = torch.device(requested)
        if device.type == "cuda" and not torch.cuda.is_available():
            raise ValueError("CUDA was requested but is unavailable.")
        if device.type == "mps" and not torch.backends.mps.is_available():
            raise ValueError("MPS was requested but is unavailable.")
        return device

    @staticmethod
    def _resolve_dtype(requested: str, device: torch.device) -> torch.dtype:
        """Choose an inference dtype supported by the selected device."""
        if requested == "auto":
            return (
                torch.float16
                if device.type in {"cuda", "mps"}
                else torch.float32
            )
        dtypes = {
            "float32": torch.float32,
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
        }
        dtype = dtypes.get(requested)
        if dtype is None:
            raise ValueError(
                "dtype must be auto, float32, float16, or bfloat16."
            )
        if device.type == "cpu" and dtype is torch.float16:
            raise ValueError("float16 inference is not supported on CPU.")
        return dtype

    @staticmethod
    def _strip_weight_prefix(key: str) -> str:
        """Remove common trainer wrappers from checkpoint parameter names."""
        for prefix in ("module.", "model."):
            if key.startswith(prefix):
                return key[len(prefix) :]
        return key

    @staticmethod
    def _validate_upstream_config(config: Mapping[str, Any]) -> None:
        """Reject artifacts for a different Prithvi variant early."""
        expected = {
            "in_chans": 6,
            "embed_dim": 1280,
            "depth": 32,
            "num_heads": 16,
        }
        mismatches = [
            f"{key}={config.get(key)!r}, expected {value!r}"
            for key, value in expected.items()
            if config.get(key) != value
        ]
        if tuple(config.get("coords_encoding", ())) != ():
            mismatches.append(
                "coords_encoding must be empty for the non-TL 600M model"
            )
        if mismatches:
            raise ValueError(
                "Unsupported Prithvi config: " + "; ".join(mismatches)
            )
