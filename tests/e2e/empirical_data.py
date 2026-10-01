"""Typed, offline oracles for the attributed empirical E2E fixtures."""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class RetailRow:
    """One source-preserving transaction line with anonymized customer ID."""

    source_row: int
    customer_id: int
    invoice_no: str
    stock_code: str
    description: str
    quantity: int
    invoice_date: str
    unit_price: float
    country: str

    @property
    def cancelled(self) -> bool:
        """Uses the source dataset's documented cancellation convention."""
        return self.invoice_no.startswith("C")


@dataclass(frozen=True, slots=True)
class TrialRow:
    """One included randomized assignment and primary shopping outcome."""

    record_id: int
    condition: int
    units_selected: float


@dataclass(frozen=True, slots=True)
class TrialEstimate:
    """Independent unadjusted treatment-control contrast and normal interval."""

    difference: float
    standard_error: float
    lower: float
    upper: float


def load_retail_rows() -> tuple[RetailRow, ...]:
    """Loads the pinned retail extract without contacting the source host."""
    with (_FIXTURES / "retail.csv").open(
        encoding="utf-8", newline=""
    ) as source:
        reader = csv.DictReader(source)
        return tuple(
            RetailRow(
                source_row=int(row["source_row"]),
                customer_id=int(row["customer_id"]),
                invoice_no=row["invoice_no"],
                stock_code=row["stock_code"],
                description=row["description"],
                quantity=int(row["quantity"]),
                invoice_date=row["invoice_date"],
                unit_price=float(row["unit_price"]),
                country=row["country"],
            )
            for row in reader
        )


def load_trial_rows() -> tuple[TrialRow, ...]:
    """Loads only assignment and outcome, excluding source participant IDs."""
    with (_FIXTURES / "trial.csv").open(encoding="utf-8", newline="") as source:
        reader = csv.DictReader(source)
        return tuple(
            TrialRow(
                record_id=int(row["record_id"]),
                condition=int(row["condition"]),
                units_selected=float(row["units_selected"]),
            )
            for row in reader
        )


def trial_estimate(rows: tuple[TrialRow, ...]) -> TrialEstimate:
    """Computes a prespecified unadjusted contrast for randomized arms 1 and 6."""
    treated = [row.units_selected for row in rows if row.condition == 1]
    control = [row.units_selected for row in rows if row.condition == 6]
    if len(treated) < 2 or len(control) < 2:
        raise ValueError("Both randomized arms require at least two outcomes")
    treated_mean = math.fsum(treated) / len(treated)
    control_mean = math.fsum(control) / len(control)
    treated_var = math.fsum(
        (value - treated_mean) ** 2 for value in treated
    ) / (len(treated) - 1)
    control_var = math.fsum(
        (value - control_mean) ** 2 for value in control
    ) / (len(control) - 1)
    standard_error = math.sqrt(
        treated_var / len(treated) + control_var / len(control)
    )
    difference = treated_mean - control_mean
    return TrialEstimate(
        difference=difference,
        standard_error=standard_error,
        lower=difference - 1.96 * standard_error,
        upper=difference + 1.96 * standard_error,
    )
