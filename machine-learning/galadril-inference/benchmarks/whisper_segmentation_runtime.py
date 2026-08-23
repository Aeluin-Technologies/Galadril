"""Compare historical and optimized Whisper segmentation ONNX sessions."""

from __future__ import annotations

import argparse
import platform
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
from galadril_inference.benchmarks import run_benchmark
from galadril_inference.models.runtime import create_session

_LEGACY_PROVIDERS = (
    "CUDAExecutionProvider",
    "CoreMLExecutionProvider",
    "CPUExecutionProvider",
)


def _parser() -> argparse.ArgumentParser:
    """Build the focused segmentation benchmark parser."""
    parser = argparse.ArgumentParser(
        description="Benchmark Whisper's pyannote ONNX segmentation session."
    )
    parser.add_argument("--model-path", type=Path, required=True)
    parser.add_argument(
        "--runtime", choices=("legacy", "optimized"), required=True
    )
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--samples", type=int, default=160_000)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--output", type=Path)
    return parser


def _legacy_session(model_path: Path) -> Any:
    """Reproduce the historical Whisper session construction exactly."""
    return ort.InferenceSession(
        str(model_path),
        providers=list(_LEGACY_PROVIDERS),
    )


def _input_buffer(session: Any, samples: int) -> tuple[str, np.ndarray]:
    """Allocate one reusable input chunk matching the segmentation graph."""
    if samples < 1:
        raise ValueError("--samples must be positive.")
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise ValueError(f"Expected one graph input, found {len(inputs)}.")
    graph_input = inputs[0]
    if graph_input.type != "tensor(float)":
        raise ValueError(f"Unsupported graph input type: {graph_input.type}.")
    return graph_input.name, np.zeros((1, 1, samples), dtype=np.float32)


def main(argv: Sequence[str] | None = None) -> int:
    """Run steady-state inference and persist a comparable JSON report."""
    args = _parser().parse_args(argv)
    if not args.model_path.is_file():
        print(
            f"benchmark failed: model not found: {args.model_path}",
            file=sys.stderr,
        )
        return 1

    started = time.perf_counter_ns()
    try:
        session = (
            _legacy_session(args.model_path)
            if args.runtime == "legacy"
            else create_session(args.model_path, device=args.device)
        )
        load_time_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        input_name, waveform = _input_buffer(session, args.samples)
        report = run_benchmark(
            lambda: session.run(None, {input_name: waveform}),
            model_name="whisper_segmentation",
            model_version="pyannote-3.0-int8",
            backend=f"onnxruntime-{args.runtime}",
            warmup_iterations=args.warmup,
            measured_iterations=args.iterations,
            workload_seconds=args.samples / 16_000.0,
            metadata={
                "device_request": args.device,
                "input": "deterministic-zero-waveform",
                "platform": platform.platform(),
                "providers": session.get_providers(),
                "sample_rate": 16_000,
                "audio_duration_seconds": args.samples / 16_000.0,
                "load_time_ms": load_time_ms,
                "runtime": args.runtime,
            },
        )
    except (RuntimeError, ValueError, ort.OnnxRuntimeError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1

    rendered = report.to_json()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report.write(args.output)
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
