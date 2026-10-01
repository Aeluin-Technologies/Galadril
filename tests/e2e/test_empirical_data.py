"""Offline contracts for the attributed empirical E2E data extracts."""

from __future__ import annotations

import hashlib
from collections import Counter
from math import isfinite
from pathlib import Path

import pytest
from empirical_data import load_retail_rows, load_trial_rows, trial_estimate


@pytest.mark.parametrize(
    ("name", "digest"),
    [
        (
            "retail.csv",
            "d750b1e3d256c78eacf7007d28fad3c14c9baa83500ba9c144dd3b8eea4de7df",
        ),
        (
            "trial.csv",
            "17df82e5083ae0d6faf3c6d9a18dbb189a5a9d02745c557ceb21fa9b9bbd9073",
        ),
    ],
)
def test_empirical_extract_matches_pinned_digest(
    name: str, digest: str
) -> None:
    """Detects accidental source or selection drift in hermetic Bazel runs."""
    path = Path(__file__).parent / "fixtures" / name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_retail_extract_preserves_identity_and_cancellation_evidence() -> None:
    """Guards the source-derived mapping before testing distributed resolution."""
    rows = load_retail_rows()
    assert len(rows) == 240
    counts = Counter(row.customer_id for row in rows)
    assert len(counts) == 24
    assert set(counts.values()) == {10}
    assert len({row.source_row for row in rows}) == len(rows)
    for customer_id in counts:
        customer_rows = [row for row in rows if row.customer_id == customer_id]
        assert sum(row.cancelled for row in customer_rows) == 1
        assert sum(not row.cancelled for row in customer_rows) == 9
        assert (
            len({row.invoice_no for row in customer_rows if not row.cancelled})
            >= 2
        )
    assert all(row.invoice_no and row.stock_code for row in rows)


def test_trial_extract_has_an_independent_uncertain_effect_oracle() -> None:
    """Rejects an invented significant effect or leaked participant fields."""
    rows = load_trial_rows()
    assert len(rows) == 205
    assert Counter(row.condition for row in rows) == {1: 101, 6: 104}
    assert len({row.record_id for row in rows}) == len(rows)
    estimate = trial_estimate(rows)
    assert estimate.difference == pytest.approx(1.998178789, abs=1e-6)
    assert estimate.lower < 0 < estimate.upper
    assert isfinite(estimate.standard_error)
    assert estimate.standard_error > 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
