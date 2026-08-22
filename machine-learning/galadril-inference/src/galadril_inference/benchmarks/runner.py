"""Low-overhead benchmark runner for model inference hot paths."""

from __future__ import annotations

import gc
import json
import math
import resource
import statistics
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

_NANOSECONDS_PER_MILLISECOND: Final = 1_000_000.0
_WORKLOAD_METADATA_KEYS: Final = (
    "audio_duration_seconds",
    "compute_type",
    "diarization",
    "device_request",
    "history_length",
    "horizon",
    "input",
    "input_kind",
    "masks",
    "prompt",
    "sample_rate",
)


@dataclass(frozen=True, slots=True)
class BenchmarkSummary:
    """Stable aggregate metrics for a set of measured predictions."""

    minimum_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    maximum_ms: float
    throughput_per_second: float
    real_time_factor: float | None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Serializable benchmark result including raw latency samples."""

    model_name: str
    model_version: str
    backend: str
    warmup_iterations: int
    measured_iterations: int
    samples_ms: tuple[float, ...]
    summary: BenchmarkSummary
    peak_rss_mb: float
    peak_rss_growth_mb: float
    metadata: Mapping[str, Any]

    def to_json(self) -> str:
        """Serialize the report with stable formatting for source comparison."""
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def write(self, path: str | Path) -> None:
        """Persist the report to a UTF-8 JSON file."""
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def read(cls, path: str | Path) -> BenchmarkReport:
        """Load and validate a previously generated report."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        summary = BenchmarkSummary(**payload.pop("summary"))
        payload["samples_ms"] = tuple(payload["samples_ms"])
        return cls(summary=summary, **payload)


@dataclass(frozen=True, slots=True)
class BenchmarkComparison:
    """Relative performance of a candidate report against a baseline."""

    model_name: str
    baseline_p50_ms: float
    candidate_p50_ms: float
    latency_improvement_pct: float
    throughput_improvement_pct: float
    speedup: float

    def to_json(self) -> str:
        """Serialize comparison metrics with stable formatting."""
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def run_benchmark(
    operation: Callable[[], object],
    *,
    model_name: str,
    model_version: str,
    backend: str,
    warmup_iterations: int = 3,
    measured_iterations: int = 20,
    workload_seconds: float | None = None,
    metadata: Mapping[str, Any] | None = None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
) -> BenchmarkReport:
    """Measure repeated steady-state inference with a reused request.

    Args:
        operation: Fully prepared prediction callable.
        model_name: Stable model identifier.
        model_version: Model implementation version.
        backend: Runtime implementation name.
        warmup_iterations: Untimed calls used to initialize runtime caches.
        measured_iterations: Number of latency samples to retain.
        workload_seconds: Media duration used to calculate real-time factor.
        metadata: Runtime, provider, input, and platform details.
        clock_ns: Monotonic nanosecond clock, injectable for deterministic tests.

    Returns:
        A serializable report containing raw and aggregate measurements.

    Raises:
        ValueError: If iteration counts or workload duration are invalid.
    """
    if warmup_iterations < 0:
        raise ValueError("warmup_iterations cannot be negative.")
    if measured_iterations < 1:
        raise ValueError("measured_iterations must be positive.")
    if workload_seconds is not None and workload_seconds <= 0.0:
        raise ValueError("workload_seconds must be positive when provided.")

    for _ in range(warmup_iterations):
        operation()

    rss_before = _peak_rss_mb()
    samples = [0.0] * measured_iterations
    gc_enabled = gc.isenabled()
    if gc_enabled:
        gc.disable()
    try:
        for index in range(measured_iterations):
            started = clock_ns()
            operation()
            finished = clock_ns()
            elapsed = (finished - started) / _NANOSECONDS_PER_MILLISECOND
            if elapsed < 0.0:
                raise RuntimeError("The benchmark clock moved backwards.")
            samples[index] = elapsed
    finally:
        if gc_enabled:
            gc.enable()

    rss_after = _peak_rss_mb()
    summary = _summarize(samples, workload_seconds)
    return BenchmarkReport(
        model_name=model_name,
        model_version=model_version,
        backend=backend,
        warmup_iterations=warmup_iterations,
        measured_iterations=measured_iterations,
        samples_ms=tuple(samples),
        summary=summary,
        peak_rss_mb=rss_after,
        peak_rss_growth_mb=max(0.0, rss_after - rss_before),
        metadata=dict(metadata or {}),
    )


def compare_reports(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
) -> BenchmarkComparison:
    """Compare equivalent reports and calculate latency/throughput deltas."""
    if baseline.model_name != candidate.model_name:
        raise ValueError(
            "Cannot compare different models: "
            f"'{baseline.model_name}' and '{candidate.model_name}'."
        )
    if baseline.warmup_iterations != candidate.warmup_iterations:
        raise ValueError("Compared reports must use the same warm-up count.")
    for key in _WORKLOAD_METADATA_KEYS:
        baseline_value = baseline.metadata.get(key)
        candidate_value = candidate.metadata.get(key)
        if baseline_value != candidate_value:
            raise ValueError(
                f"Compared reports have different '{key}' metadata: "
                f"{baseline_value!r} and {candidate_value!r}."
            )
    baseline_latency = baseline.summary.p50_ms
    candidate_latency = candidate.summary.p50_ms
    if baseline_latency <= 0.0 or candidate_latency <= 0.0:
        raise ValueError("Compared reports must have positive median latency.")

    latency_improvement = (
        (baseline_latency - candidate_latency) / baseline_latency * 100.0
    )
    baseline_throughput = baseline.summary.throughput_per_second
    candidate_throughput = candidate.summary.throughput_per_second
    throughput_improvement = (
        (candidate_throughput - baseline_throughput)
        / baseline_throughput
        * 100.0
    )
    return BenchmarkComparison(
        model_name=baseline.model_name,
        baseline_p50_ms=baseline_latency,
        candidate_p50_ms=candidate_latency,
        latency_improvement_pct=latency_improvement,
        throughput_improvement_pct=throughput_improvement,
        speedup=baseline_latency / candidate_latency,
    )


def _summarize(
    samples_ms: list[float], workload_seconds: float | None
) -> BenchmarkSummary:
    """Calculate aggregates without introducing NumPy into the runner."""
    ordered = sorted(samples_ms)
    mean_ms = statistics.fmean(ordered)
    p50_ms = _percentile(ordered, 0.50)
    real_time_factor = (
        p50_ms / (workload_seconds * 1000.0)
        if workload_seconds is not None
        else None
    )
    return BenchmarkSummary(
        minimum_ms=ordered[0],
        mean_ms=mean_ms,
        p50_ms=p50_ms,
        p95_ms=_percentile(ordered, 0.95),
        p99_ms=_percentile(ordered, 0.99),
        maximum_ms=ordered[-1],
        throughput_per_second=(1000.0 / mean_ms) if mean_ms > 0.0 else math.inf,
        real_time_factor=real_time_factor,
    )


def _percentile(ordered: list[float], quantile: float) -> float:
    """Interpolate a percentile from an already sorted non-empty sample set."""
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _peak_rss_mb() -> float:
    """Return process peak resident memory in MiB across macOS and Linux."""
    raw = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    bytes_used = raw if sys.platform == "darwin" else raw * 1024.0
    return bytes_used / (1024.0 * 1024.0)
