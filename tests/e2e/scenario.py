"""Deterministic, multi-principal uploads derived from empirical source rows."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from empirical_data import RetailRow, TrialRow

DataType = Literal["sales", "returns"]
ADMIN = "e2e-admin"
SALES_STEWARDS = ("e2e-sales-a", "e2e-sales-b", "e2e-sales-c", ADMIN)
RETURNS_STEWARDS = ("e2e-returns-a", "e2e-returns-b", ADMIN)
_SMOKE_CUSTOMERS = 8
_SMOKE_SALES_PER_CUSTOMER = 2
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


@dataclass(frozen=True, slots=True)
class RetailState:
    """One persisted row whose source and identity are checked independently."""

    entity_id: str
    event_id: str
    customer_id: int
    source_row: int


def assert_retail_identities(
    states: Sequence[RetailState], uploads: Sequence[UploadBatch]
) -> dict[str, set[str]]:
    """Checks the identity partition and returns each entity's events."""
    expected = {
        row.source_row: (row.customer_id, upload.actor)
        for upload in uploads
        for row in upload.rows
    }
    actual = {state.source_row: state.customer_id for state in states}
    assert len(states) == len(expected) == len(actual)
    assert actual == {row: customer for row, (customer, _) in expected.items()}
    assert len({state.event_id for state in states}) == len(states), (
        "Each retail observation must retain its own event"
    )

    entities: dict[tuple[int, str], str] = {}
    scopes: dict[str, tuple[int, str]] = {}
    events: dict[str, set[str]] = defaultdict(set)
    for state in states:
        # Uploader grants govern candidate reuse; reader grants only project evidence.
        scope = expected[state.source_row]
        assert entities.setdefault(scope, state.entity_id) == state.entity_id, (
            "LI-ESKG split a customer within one uploader's candidate scope"
        )
        assert scopes.setdefault(state.entity_id, scope) == scope, (
            "LI-ESKG merged different customers or uploader scopes"
        )
        events[state.entity_id].add(state.event_id)
    return events


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


def retail_smoke_rows(
    rows: tuple[RetailRow, ...], required_sources: set[tuple[int, int]]
) -> tuple[RetailRow, ...]:
    """Keeps cited source rows before filling the purchase and return cohorts."""
    indexed = {(row.customer_id, row.source_row): row for row in rows}
    missing = required_sources - indexed.keys()
    if missing:
        raise ValueError(f"Unknown retail retrieval sources: {sorted(missing)}")
    customers = {customer for customer, _ in required_sources}
    if len(customers) > _SMOKE_CUSTOMERS:
        raise ValueError("Retrieval sources exceed the retail customer budget")
    spare = sorted({row.customer_id for row in rows} - customers)
    customers.update(spare[: _SMOKE_CUSTOMERS - len(customers)])
    sales = Counter(
        customer
        for customer, source in required_sources
        if not indexed[customer, source].cancelled
    )
    if any(count > _SMOKE_SALES_PER_CUSTOMER for count in sales.values()):
        raise ValueError(
            "Retrieval sources exceed the per-customer sales budget"
        )
    selected = []
    for row in rows:
        if row.customer_id not in customers:
            continue
        if (
            row.cancelled
            or (row.customer_id, row.source_row) in required_sources
        ):
            selected.append(row)
        elif sales[row.customer_id] < _SMOKE_SALES_PER_CUSTOMER:
            sales[row.customer_id] += 1
            selected.append(row)
    return tuple(selected)


def build_retail_uploads(
    rows: tuple[RetailRow, ...], *, full: bool = True
) -> tuple[UploadBatch, ...]:
    """Packs customer sales files and paired return files."""
    customers = sorted({row.customer_id for row in rows})
    expected_customers, sales_per_customer = (
        (24, 9) if full else (_SMOKE_CUSTOMERS, _SMOKE_SALES_PER_CUSTOMER)
    )
    if len(customers) != expected_customers:
        raise ValueError("Unexpected retail cohort")
    uploads: list[UploadBatch] = []
    returns: list[RetailRow] = []
    for index, customer in enumerate(customers):
        customer_rows = tuple(
            row for row in rows if row.customer_id == customer
        )
        sales = tuple(row for row in customer_rows if not row.cancelled)
        refunds = tuple(row for row in customer_rows if row.cancelled)
        if len(sales) != sales_per_customer or len(refunds) != 1:
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
    for index in range(len(returns) // 2):
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
        name for index, name in enumerate(sorted(names)) if index % 2 == 0
    )
    access["e2e-exact-reader"] = frozenset({"sales-00.csv"})
    access["e2e-member"] = frozenset()
    return access
