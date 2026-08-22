"""Command-line benchmarks for heavyweight Galadril model inference paths."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import math
import os
import platform
import sys
import time
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
from galadril_inference.benchmarks import (
    BenchmarkReport,
    compare_reports,
    run_benchmark,
)
from galadril_inference.common.types import PredictionRequest
from galadril_inference.models.base import BaseModel


def _parser() -> argparse.ArgumentParser:
    """Build the benchmark CLI parser."""
    parser = argparse.ArgumentParser(
        description="Benchmark and compare Galadril model inference runtimes."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="Run one prepared model benchmark.")
    run.add_argument(
        "--model",
        required=True,
        choices=("whisper", "siglip2", "owlv2", "grounded_sam", "timesfm"),
    )
    run.add_argument("--artifact-path", type=Path, required=True)
    run.add_argument(
        "--implementation-file",
        type=Path,
        help="Load the model class from a historical Python file for comparisons.",
    )
    run.add_argument("--input", help="Audio, image, or text input as required.")
    run.add_argument("--prompt", default="person. vehicle.")
    run.add_argument("--device", default="auto")
    run.add_argument("--compute-type", default="int8")
    run.add_argument("--warmup", type=int, default=3)
    run.add_argument("--iterations", type=int, default=20)
    run.add_argument("--output", type=Path)
    run.add_argument(
        "--label",
        default="unlabelled",
        help="Revision or experiment label stored in report metadata.",
    )
    run.add_argument("--download", action="store_true")
    run.add_argument(
        "--diarization",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include Whisper's ONNX diarization path (default: enabled).",
    )
    run.add_argument(
        "--masks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Include Grounded SAM mask decoding (default: enabled).",
    )
    run.add_argument("--history-length", type=int, default=512)
    run.add_argument("--horizon", type=int, default=24)

    compare = commands.add_parser(
        "compare", help="Compare candidate JSON against a baseline JSON."
    )
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    compare.add_argument("--output", type=Path)
    compare.add_argument("--require-latency-improvement-pct", type=float)
    return parser


def _run(args: argparse.Namespace) -> int:
    """Prepare a scenario, run it, and emit its JSON report."""
    model, request, workload_seconds, input_metadata = _prepare_scenario(args)
    meta = model.meta()
    started = time.perf_counter_ns()
    try:
        if args.download:
            _download(model, args)
        _load(model, args)
        _validate_runtime(model, args)
        load_time_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        metadata: dict[str, Any] = {
            "compute_type": args.compute_type,
            "device_request": args.device,
            "environment_device": os.getenv("GALADRIL_DEVICE"),
            "implementation_file": (
                args.implementation_file.name
                if args.implementation_file is not None
                else None
            ),
            "load_time_ms": load_time_ms,
            "label": args.label,
            "platform": platform.platform(),
            "python": platform.python_version(),
            "providers": _providers(model),
            "runtime_versions": _runtime_versions(),
            **input_metadata,
        }
        report = run_benchmark(
            lambda: model.predict(request),
            model_name=meta.name,
            model_version=meta.version,
            backend=meta.tags.get("backend", "unknown"),
            warmup_iterations=args.warmup,
            measured_iterations=args.iterations,
            workload_seconds=workload_seconds,
            metadata=metadata,
        )
    finally:
        model.cleanup()

    rendered = report.to_json()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        report.write(args.output)
    sys.stdout.write(rendered)
    return 0


def _compare(args: argparse.Namespace) -> int:
    """Compare two persisted reports and optionally enforce a speedup gate."""
    comparison = compare_reports(
        BenchmarkReport.read(args.baseline),
        BenchmarkReport.read(args.candidate),
    )
    rendered = comparison.to_json()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)

    required = args.require_latency_improvement_pct
    if required is not None and comparison.latency_improvement_pct < required:
        return 2
    return 0


def _prepare_scenario(
    args: argparse.Namespace,
) -> tuple[BaseModel, PredictionRequest, float | None, dict[str, Any]]:
    """Create a model and immutable request outside the timed region."""
    preparations: dict[
        str,
        Callable[
            [argparse.Namespace],
            tuple[BaseModel, PredictionRequest, float | None, dict[str, Any]],
        ],
    ] = {
        "whisper": _prepare_whisper,
        "siglip2": _prepare_siglip,
        "owlv2": _prepare_owl,
        "grounded_sam": _prepare_grounded_sam,
        "timesfm": _prepare_timesfm,
    }
    return preparations[args.model](args)


def _prepare_whisper(
    args: argparse.Namespace,
) -> tuple[BaseModel, PredictionRequest, float, dict[str, Any]]:
    """Decode audio once and prepare full Whisper/diarization inference."""
    if args.input is None:
        raise ValueError("Whisper requires --input pointing to an audio file.")
    import soundfile as sound_file
    from galadril_inference.models.audio.whisper import WhisperModel

    path = Path(args.input)
    waveform, sample_rate = sound_file.read(str(path), dtype="float32")
    duration = float(waveform.shape[0]) / float(sample_rate)
    request = PredictionRequest(
        model_name="whisper",
        features={
            "audio": {"waveform": waveform, "sample_rate": sample_rate},
            "task": "transcribe",
            "enable_diarization": args.diarization,
        },
    )
    return (
        _model_instance(args, WhisperModel, "WhisperModel"),
        request,
        duration,
        {
            "audio_duration_seconds": duration,
            "diarization": args.diarization,
            "input": path.name,
            "sample_rate": sample_rate,
        },
    )


def _prepare_siglip(
    args: argparse.Namespace,
) -> tuple[BaseModel, PredictionRequest, None, dict[str, Any]]:
    """Prepare text or image embedding based on the input path."""
    from galadril_inference.models.embedding.siglip import SigLIPModel

    if args.input is None:
        raise ValueError("SigLIP2 requires --input text or an image path.")
    path = Path(args.input)
    if path.is_file():
        image = _read_image(path)
        features: dict[str, Any] = {"action": "embed_image", "image": image}
        input_kind = "image"
    else:
        features = {"action": "embed_text", "text": args.input}
        input_kind = "text"
    request = PredictionRequest(model_name="siglip2", features=features)
    return (
        _model_instance(args, SigLIPModel, "SigLIPModel"),
        request,
        None,
        {"input_kind": input_kind},
    )


def _prepare_owl(
    args: argparse.Namespace,
) -> tuple[BaseModel, PredictionRequest, None, dict[str, Any]]:
    """Prepare an OwlV2 detection request."""
    from galadril_inference.models.image.owl import OwlV2Model

    image, path = _required_image(args.input, "OwlV2")
    request = PredictionRequest(
        model_name="owlv2",
        features={"image": image, "text": args.prompt, "threshold": 0.1},
    )
    return (
        _model_instance(args, OwlV2Model, "OwlV2Model"),
        request,
        None,
        {"input": path.name, "prompt": args.prompt},
    )


def _prepare_grounded_sam(
    args: argparse.Namespace,
) -> tuple[BaseModel, PredictionRequest, None, dict[str, Any]]:
    """Prepare detector-only or complete Grounded SAM inference."""
    from galadril_inference.models.image.grounded_sam import GroundedSamModel

    image, path = _required_image(args.input, "Grounded SAM")
    request = PredictionRequest(
        model_name="grounded_sam",
        features={
            "image": image,
            "text": args.prompt,
            "threshold": 0.2,
            "return_masks": args.masks,
        },
    )
    return (
        _model_instance(args, GroundedSamModel, "GroundedSamModel"),
        request,
        None,
        {"input": path.name, "masks": args.masks, "prompt": args.prompt},
    )


def _prepare_timesfm(
    args: argparse.Namespace,
) -> tuple[BaseModel, PredictionRequest, None, dict[str, Any]]:
    """Build a deterministic synthetic time-series forecast request."""
    from galadril_inference.models.temporal.time_series import TimesFMModel

    if args.history_length < 2:
        raise ValueError("--history-length must be at least 2.")
    history = [
        math.sin(index * 0.05) + 0.1 * math.cos(index * 0.013)
        for index in range(args.history_length)
    ]
    request = PredictionRequest(
        model_name="timesfm_forecast",
        features={"history": history, "horizon": args.horizon},
    )
    return (
        _model_instance(args, TimesFMModel, "TimesFMModel"),
        request,
        None,
        {"history_length": args.history_length, "horizon": args.horizon},
    )


def _download(model: BaseModel, args: argparse.Namespace) -> None:
    """Download scenario artifacts outside the measured region."""
    args.artifact_path.mkdir(parents=True, exist_ok=True)
    _invoke_supported(
        model.download,
        str(args.artifact_path),
        compute_type=args.compute_type,
    )


def _load(model: BaseModel, args: argparse.Namespace) -> None:
    """Load the selected runtime with consistent device/precision controls."""
    _invoke_supported(
        model.load,
        str(args.artifact_path),
        compute_type=args.compute_type,
        device=args.device,
    )


def _validate_runtime(model: BaseModel, args: argparse.Namespace) -> None:
    """Reject benchmark runs that silently skipped a requested inference stage."""
    if args.model != "whisper" or not args.diarization:
        return
    if getattr(model, "_embedding_inference", None) is None:
        raise RuntimeError(
            "Whisper diarization was requested but speaker embedding is inactive. "
            "Install its runtime dependencies or pass --no-diarization."
        )


def _invoke_supported(
    method: Callable[..., object], artifact_path: str, **options: object
) -> object:
    """Call current or historical hooks with only parameters they support."""
    supported = inspect.signature(method).parameters
    kwargs = {key: value for key, value in options.items() if key in supported}
    return method(artifact_path, **kwargs)


def _model_instance(
    args: argparse.Namespace,
    default_class: type[BaseModel],
    class_name: str,
) -> BaseModel:
    """Instantiate a built-in model or an exact historical source implementation."""
    path = args.implementation_file
    if path is None:
        return default_class()
    if not path.is_file():
        raise ValueError(f"Implementation file does not exist: {path}.")
    module_name = f"galadril_benchmark_{args.model}_{abs(hash(path))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot import implementation file: {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    historical_class = getattr(module, class_name, None)
    if not isinstance(historical_class, type) or not issubclass(
        historical_class, BaseModel
    ):
        raise ValueError(f"{path} does not define a valid {class_name}.")
    return historical_class()


def _providers(model: BaseModel) -> list[str]:
    """Collect execution providers from every ONNX stage without duplication."""
    providers: list[str] = []
    for name in (
        "_session",
        "_image_session",
        "_text_session",
        "_detector_session",
        "_vision_session",
        "_prompt_session",
        "_segmentation_session",
    ):
        session = getattr(model, name, None)
        if session is None or not hasattr(session, "get_providers"):
            continue
        for provider in session.get_providers():
            if provider not in providers:
                providers.append(provider)
    return providers


def _runtime_versions() -> dict[str, str]:
    """Record installed inference packages needed to reproduce a result."""
    versions: dict[str, str] = {}
    for package in (
        "faster-whisper",
        "onnxruntime",
        "onnxruntime-gpu",
        "torch",
        "transformers",
    ):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            continue
    return versions


def _required_image(
    value: str | None, model_name: str
) -> tuple[np.ndarray, Path]:
    """Validate and decode a required image argument."""
    if value is None:
        raise ValueError(f"{model_name} requires --input pointing to an image.")
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"Image does not exist: {path}.")
    return _read_image(path), path


def _read_image(path: Path) -> np.ndarray:
    """Decode an image into one contiguous RGB uint8 buffer."""
    from PIL import Image

    with Image.open(path) as image:
        return np.ascontiguousarray(
            np.asarray(image.convert("RGB"), dtype=np.uint8)
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested benchmark command."""
    args = _parser().parse_args(argv)
    try:
        return _run(args) if args.command == "run" else _compare(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
