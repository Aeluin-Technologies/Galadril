"""Tests for reproducible model benchmark aggregation and comparison."""

from __future__ import annotations

from pathlib import Path

import pytest
from galadril_inference.benchmarks import (
    BenchmarkReport,
    compare_reports,
    run_benchmark,
)


def test_run_benchmark_excludes_warmup_and_calculates_percentiles() -> None:
    """Only measured calls should contribute to retained latency metrics."""
    calls = 0
    clock_values = iter(
        (0, 1_000_000, 2_000_000, 4_000_000, 5_000_000, 8_000_000)
    )

    def operation() -> None:
        nonlocal calls
        calls += 1

    report = run_benchmark(
        operation,
        model_name="test_model",
        model_version="1.0.0",
        backend="onnxruntime",
        warmup_iterations=2,
        measured_iterations=3,
        workload_seconds=2.0,
        clock_ns=lambda: next(clock_values),
    )

    assert calls == 5
    assert report.samples_ms == (1.0, 2.0, 3.0)
    assert report.summary.mean_ms == 2.0
    assert report.summary.p50_ms == 2.0
    assert report.summary.p95_ms == pytest.approx(2.9)
    assert report.summary.throughput_per_second == 500.0
    assert report.summary.real_time_factor == 0.001


def test_report_round_trip_preserves_samples(tmp_path: Path) -> None:
    """Persisted reports should retain exact samples and comparison metadata."""
    clock_values = iter((0, 2_000_000))
    report = run_benchmark(
        lambda: None,
        model_name="test_model",
        model_version="1.0.0",
        backend="onnxruntime",
        warmup_iterations=0,
        measured_iterations=1,
        metadata={"providers": ["CPUExecutionProvider"]},
        clock_ns=lambda: next(clock_values),
    )
    output = tmp_path / "report.json"

    report.write(output)
    restored = BenchmarkReport.read(output)

    assert restored == report


def test_compare_reports_calculates_candidate_speedup() -> None:
    """A lower candidate median should produce positive improvement metrics."""
    baseline_clock = iter((0, 4_000_000, 5_000_000, 9_000_000))
    candidate_clock = iter((0, 2_000_000, 3_000_000, 5_000_000))
    baseline = run_benchmark(
        lambda: None,
        model_name="test_model",
        model_version="1.0.0",
        backend="pytorch",
        warmup_iterations=0,
        measured_iterations=2,
        clock_ns=lambda: next(baseline_clock),
    )
    candidate = run_benchmark(
        lambda: None,
        model_name="test_model",
        model_version="1.1.0",
        backend="onnxruntime",
        warmup_iterations=0,
        measured_iterations=2,
        clock_ns=lambda: next(candidate_clock),
    )

    comparison = compare_reports(baseline, candidate)

    assert comparison.speedup == 2.0
    assert comparison.latency_improvement_pct == 50.0
    assert comparison.throughput_improvement_pct == 100.0


def test_run_benchmark_rejects_invalid_iterations() -> None:
    """Invalid benchmark sizes should fail before invoking the model."""
    with pytest.raises(ValueError, match="measured_iterations"):
        run_benchmark(
            lambda: None,
            model_name="test_model",
            model_version="1.0.0",
            backend="onnxruntime",
            measured_iterations=0,
        )


def test_compare_reports_rejects_different_workloads() -> None:
    """Reports with different devices or inputs must not imply a valid speedup."""
    baseline_clock = iter((0, 2_000_000))
    candidate_clock = iter((0, 1_000_000))
    baseline = run_benchmark(
        lambda: None,
        model_name="test_model",
        model_version="1.0.0",
        backend="pytorch",
        warmup_iterations=0,
        measured_iterations=1,
        metadata={"device_request": "cpu"},
        clock_ns=lambda: next(baseline_clock),
    )
    candidate = run_benchmark(
        lambda: None,
        model_name="test_model",
        model_version="1.1.0",
        backend="onnxruntime",
        warmup_iterations=0,
        measured_iterations=1,
        metadata={"device_request": "cuda"},
        clock_ns=lambda: next(candidate_clock),
    )

    with pytest.raises(ValueError, match="device_request"):
        compare_reports(baseline, candidate)
