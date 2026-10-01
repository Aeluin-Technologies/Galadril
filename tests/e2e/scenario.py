"""Deterministic, multi-principal uploads derived from empirical source rows."""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from typing import Literal

from empirical_data import RetailRow, TrialRow

DataType = Literal["sales", "returns"]
ADMIN = "e2e-admin"
SALES_STEWARDS = ("e2e-sales-a", "e2e-sales-b", "e2e-sales-c", ADMIN)
RETURNS_STEWARDS = ("e2e-returns-a", "e2e-returns-b", ADMIN)
_RETAIL_COLUMNS = (
    "source_row",
    "customer_id",
    "invoice_no",
    "stock_code",
    "description",
    "quantity",
    "invoice_date",
    "unit_price",
    "country",
)


@dataclass(frozen=True, slots=True)
class UploadBatch:
    """One Gateway upload with an unambiguous owner and source-row set."""

    name: str
    actor: str
    data_type: DataType
    rows: tuple[RetailRow, ...]
    payload: bytes


def _encode_rows(rows: tuple[RetailRow, ...]) -> bytes:
    """Preserves source values in the Intake text schema's row-level payload."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("content", "encoding"))
    for row in rows:
        content = dict(
            zip(
                _RETAIL_COLUMNS,
                (
                    row.source_row,
                    row.customer_id,
                    row.invoice_no,
                    row.stock_code,
                    row.description,
                    row.quantity,
                    row.invoice_date,
                    row.unit_price,
                    row.country,
                ),
                strict=True,
            )
        )
        writer.writerow((json.dumps(content, separators=(",", ":")), "utf-8"))
    return output.getvalue().encode("utf-8")


def build_retail_uploads(
    rows: tuple[RetailRow, ...],
) -> tuple[UploadBatch, ...]:
    """Packs 24 customer sales files and 12 paired return files."""
    customers = sorted({row.customer_id for row in rows})
    if len(rows) != 240 or len(customers) != 24:
        raise ValueError("Expected the pinned 240-row, 24-customer fixture")
    uploads: list[UploadBatch] = []
    returns: list[RetailRow] = []
    for index, customer in enumerate(customers):
        customer_rows = tuple(
            row for row in rows if row.customer_id == customer
        )
        sales = tuple(row for row in customer_rows if not row.cancelled)
        refunds = tuple(row for row in customer_rows if row.cancelled)
        if len(sales) != 9 or len(refunds) != 1:
            raise ValueError(f"Unexpected transaction selection for {customer}")
        uploads.append(
            UploadBatch(
                name=f"sales-{index:02d}.csv",
                actor=SALES_STEWARDS[index % len(SALES_STEWARDS)],
                data_type="sales",
                rows=sales,
                payload=_encode_rows(sales),
            )
        )
        returns.extend(refunds)
    for index in range(12):
        batch_rows = tuple(returns[index * 2 : index * 2 + 2])
        uploads.append(
            UploadBatch(
                name=f"returns-{index:02d}.csv",
                actor=RETURNS_STEWARDS[index % len(RETURNS_STEWARDS)],
                data_type="returns",
                rows=batch_rows,
                payload=_encode_rows(batch_rows),
            )
        )
    return tuple(uploads)


def encode_trial_rows(rows: tuple[TrialRow, ...]) -> bytes:
    """Encodes only anonymized assignment and outcome as Intake CSV records."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("content", "encoding"))
    for row in rows:
        writer.writerow(
            (
                json.dumps(
                    {
                        "record_id": row.record_id,
                        "condition": row.condition,
                        "units_selected": row.units_selected,
                    },
                    separators=(",", ":"),
                ),
                "utf-8",
            )
        )
    return output.getvalue().encode("utf-8")


def build_access_oracle(
    uploads: tuple[UploadBatch, ...],
) -> dict[str, frozenset[str]]:
    """Defines independent object visibility for union and disjoint grants."""
    names = frozenset(upload.name for upload in uploads)
    sales = frozenset(
        upload.name for upload in uploads if upload.data_type == "sales"
    )
    returns = names - sales
    actors = (*SALES_STEWARDS, *RETURNS_STEWARDS)
    access = {
        actor: frozenset(
            upload.name for upload in uploads if upload.actor == actor
        )
        for actor in actors
    }
    access[ADMIN] = names
    access["e2e-sales-reader"] = sales
    access["e2e-returns-reader"] = returns
    access["e2e-analyst"] = frozenset(
        upload.name for index, upload in enumerate(uploads) if index % 2 == 0
    )
    access["e2e-exact-reader"] = frozenset({"sales-00.csv"})
    access["e2e-member"] = frozenset()
    return access
