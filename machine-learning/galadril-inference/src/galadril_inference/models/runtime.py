"""Lightweight ONNX Runtime configuration shared by inference models."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

_PROVIDER_PRIORITY: Final[tuple[str, ...]] = (
    "TensorrtExecutionProvider",
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
    "DmlExecutionProvider",
    "CoreMLExecutionProvider",
    "OpenVINOExecutionProvider",
    "CPUExecutionProvider",
)
_DEVICE_PROVIDERS: Final[Mapping[str, tuple[str, ...]]] = {
    "cpu": ("CPUExecutionProvider",),
    "gpu": _PROVIDER_PRIORITY[:-1],
    "cuda": ("TensorrtExecutionProvider", "CUDAExecutionProvider"),
    "tensorrt": ("TensorrtExecutionProvider", "CUDAExecutionProvider"),
    "rocm": ("ROCMExecutionProvider",),
    "directml": ("DmlExecutionProvider",),
    "dml": ("DmlExecutionProvider",),
    "coreml": ("CoreMLExecutionProvider",),
    "openvino": ("OpenVINOExecutionProvider",),
}


def resolve_providers(
    available: Sequence[str],
    device: str | None = None,
) -> tuple[str, ...]:
    """Resolve execution providers in deterministic accelerator-first order.

    Args:
        available: Providers exposed by the installed ONNX Runtime wheel.
        device: Requested device or ``None`` for ``GALADRIL_DEVICE``/automatic.

    Returns:
        Available providers in preferred order with CPU as a safe fallback.

    Raises:
        ValueError: If the requested device name is unsupported.
        RuntimeError: If ONNX Runtime exposes no usable execution provider.
    """
    requested = (device or os.getenv("GALADRIL_DEVICE", "auto")).strip().lower()
    if requested != "auto" and requested not in _DEVICE_PROVIDERS:
        choices = ", ".join(("auto", *_DEVICE_PROVIDERS))
        raise ValueError(
            f"Unsupported ONNX device '{requested}'. Expected one of: {choices}."
        )

    available_set = frozenset(available)
    preferred = (
        _PROVIDER_PRIORITY
        if requested == "auto"
        else _DEVICE_PROVIDERS[requested]
    )
    selected = [provider for provider in preferred if provider in available_set]

    # Accelerator requests intentionally fall back to CPU for portable deploys.
    if (
        "CPUExecutionProvider" in available_set
        and "CPUExecutionProvider" not in selected
    ):
        selected.append("CPUExecutionProvider")
    if not selected:
        raise RuntimeError(
            "The installed ONNX Runtime wheel exposes no provider compatible "
            f"with device '{requested}'. Available providers: {sorted(available_set)}."
        )
    return tuple(selected)


def create_session(
    model_path: str | Path,
    *,
    device: str | None = None,
    provider_options: Sequence[Mapping[str, str]] | None = None,
) -> Any:
    """Create an optimized ONNX inference session for the current environment.

    Thread counts can be pinned with ``GALADRIL_ONNX_INTRA_OP_THREADS`` and
    ``GALADRIL_ONNX_INTER_OP_THREADS`` to prevent runtime oversubscription.
    """
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise RuntimeError(
            "ONNX Runtime is not installed. Install the 'cpu' or 'gpu' extra."
        ) from exc

    options = ort.SessionOptions()
    options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    options.enable_cpu_mem_arena = True
    options.enable_mem_pattern = True
    options.intra_op_num_threads = _positive_env_int(
        "GALADRIL_ONNX_INTRA_OP_THREADS"
    )
    options.inter_op_num_threads = _positive_env_int(
        "GALADRIL_ONNX_INTER_OP_THREADS"
    )

    providers = resolve_providers(ort.get_available_providers(), device)
    kwargs: dict[str, Any] = {
        "sess_options": options,
        "providers": list(providers),
    }
    if provider_options is not None:
        if len(provider_options) != len(providers):
            raise ValueError(
                "provider_options must contain one mapping per selected provider."
            )
        kwargs["provider_options"] = list(provider_options)
    return ort.InferenceSession(str(model_path), **kwargs)


def _positive_env_int(name: str) -> int:
    """Read an optional positive integer, using zero for runtime defaults."""
    raw = os.getenv(name)
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{name} must be a positive integer, got '{raw}'."
        ) from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got '{raw}'.")
    return value
