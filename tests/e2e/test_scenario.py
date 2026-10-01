"""Contracts for distributing empirical rows across E2E principals."""

from __future__ import annotations

import csv
import io
import json
from collections import Counter

import pytest
from empirical_data import load_retail_rows, load_trial_rows
from scenario import (
    ADMIN,
    RETURNS_STEWARDS,
    SALES_STEWARDS,
    build_access_oracle,
    build_retail_uploads,
    encode_trial_rows,
)


def test_upload_plan_preserves_lineage_and_principals() -> None:
    """Prevents the integration case from reducing to one privileged uploader."""
    rows = load_retail_rows()
    uploads = build_retail_uploads(rows)
    assert len(uploads) == 36
    assert Counter(upload.data_type for upload in uploads) == {
        "sales": 24,
        "returns": 12,
    }
    assert len({upload.actor for upload in uploads}) == 6
    assert all(upload.rows and upload.payload for upload in uploads)
    assert len({upload.name for upload in uploads}) == 36
    assert Counter(
        row.source_row for upload in uploads for row in upload.rows
    ) == {row.source_row: 1 for row in rows}
    assert all(
        all(
            row.cancelled == (upload.data_type == "returns")
            for row in upload.rows
        )
        for upload in uploads
    )
    assert all(
        len(upload.rows) == 9
        for upload in uploads
        if upload.data_type == "sales"
    )
    assert all(
        len(upload.rows) == 2
        for upload in uploads
        if upload.data_type == "returns"
    )
    for upload in uploads:
        parsed = tuple(csv.DictReader(io.StringIO(upload.payload.decode())))
        assert len(parsed) == len(upload.rows)
        assert [
            json.loads(record["content"])["source_row"] for record in parsed
        ] == [row.source_row for row in upload.rows]
        assert all(record["encoding"] == "utf-8" for record in parsed)


def test_access_oracle_has_union_disjoint_and_exact_object_grants() -> None:
    """Keeps expected visibility independent from Gateway and SpiceDB output."""
    uploads = build_retail_uploads(load_retail_rows())
    access = build_access_oracle(uploads)
    names = {upload.name for upload in uploads}
    sales = {upload.name for upload in uploads if upload.data_type == "sales"}
    returns = names - sales
    assert access[ADMIN] == names
    assert access["e2e-sales-reader"] == sales
    assert access["e2e-returns-reader"] == returns
    assert access["e2e-sales-reader"].isdisjoint(access["e2e-returns-reader"])
    assert access["e2e-analyst"] & sales
    assert access["e2e-analyst"] & returns
    assert access["e2e-analyst"] < names
    assert len(access["e2e-exact-reader"]) == 1
    assert access["e2e-member"] == frozenset()
    for steward in (*SALES_STEWARDS[:-1], *RETURNS_STEWARDS[:-1]):
        owned = {upload.name for upload in uploads if upload.actor == steward}
        assert access[steward] == owned


def test_trial_payload_preserves_all_anonymous_randomized_rows() -> None:
    """Trial data entering Gateway remains the pinned analysis cohort."""
    rows = load_trial_rows()
    payload = encode_trial_rows(rows)
    parsed = tuple(csv.DictReader(io.StringIO(payload.decode())))
    assert len(parsed) == 205
    assert [json.loads(row["content"])["record_id"] for row in parsed] == [
        row.record_id for row in rows
    ]
    assert {json.loads(row["content"])["condition"] for row in parsed} == {
        1,
        6,
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "--import-mode=importlib"]))
