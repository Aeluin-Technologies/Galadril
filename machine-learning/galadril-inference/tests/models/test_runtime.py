"""Tests for environment-aware ONNX Runtime configuration."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from galadril_inference.models.runtime import create_session, resolve_providers


def test_resolve_providers_prefers_accelerator_with_cpu_fallback() -> None:
    """Automatic selection should prefer the fastest available GPU provider."""
    available = [
        "CPUExecutionProvider",
        "ROCMExecutionProvider",
        "CUDAExecutionProvider",
    ]

    assert resolve_providers(available) == (
        "CUDAExecutionProvider",
        "ROCMExecutionProvider",
        "CPUExecutionProvider",
    )


def test_resolve_providers_honours_explicit_cpu() -> None:
    """Explicit CPU selection must never activate an accelerator."""
    available = ["CUDAExecutionProvider", "CPUExecutionProvider"]

    assert resolve_providers(available, "cpu") == ("CPUExecutionProvider",)


def test_resolve_providers_falls_back_when_gpu_is_unavailable() -> None:
    """GPU deployments should remain portable to CPU-only environments."""
    assert resolve_providers(["CPUExecutionProvider"], "gpu") == (
        "CPUExecutionProvider",
    )


def test_resolve_providers_rejects_unknown_device() -> None:
    """Invalid deployment configuration should fail before model loading."""
    with pytest.raises(ValueError, match="Unsupported ONNX device"):
        resolve_providers(["CPUExecutionProvider"], "quantum")


def test_create_session_applies_optimized_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session construction should configure deterministic optimized execution."""
    captured: dict[str, object] = {}
    fake_ort = types.ModuleType("onnxruntime")

    class FakeSessionOptions:
        graph_optimization_level: object | None = None
        execution_mode: object | None = None
        enable_cpu_mem_arena = False
        enable_mem_pattern = False
        intra_op_num_threads = -1
        inter_op_num_threads = -1

    class FakeInferenceSession:
        def __init__(self, path: str, **kwargs: object) -> None:
            captured["path"] = path
            captured.update(kwargs)

    fake_ort.SessionOptions = FakeSessionOptions
    fake_ort.InferenceSession = FakeInferenceSession
    fake_ort.GraphOptimizationLevel = types.SimpleNamespace(
        ORT_ENABLE_ALL="all"
    )
    fake_ort.ExecutionMode = types.SimpleNamespace(ORT_SEQUENTIAL="sequential")
    fake_ort.get_available_providers = lambda: [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_ort)
    monkeypatch.setenv("GALADRIL_ONNX_INTRA_OP_THREADS", "2")
    monkeypatch.setenv("GALADRIL_ONNX_INTER_OP_THREADS", "1")

    session = create_session(Path("model.onnx"))

    assert isinstance(session, FakeInferenceSession)
    assert captured["path"] == "model.onnx"
    assert captured["providers"] == [
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]
    options = captured["sess_options"]
    assert isinstance(options, FakeSessionOptions)
    assert options.graph_optimization_level == "all"
    assert options.execution_mode == "sequential"
    assert options.enable_cpu_mem_arena is True
    assert options.enable_mem_pattern is True
    assert options.intra_op_num_threads == 2
    assert options.inter_op_num_threads == 1
